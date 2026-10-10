"""Extraction pipeline orchestration (§5–§12).

    native text  →  OCR only where needed  →  vision only where needed

Each stage writes evidence into the canonical document. Nothing here calls a
language model to produce text: vision is used to *verify* or *repair* a
specific region, and its reading is stored alongside the original rather than
replacing it (§9, §53).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.audit.models import AuditAction

from . import extractors
from .critical_values import contains_critical_value
from .language import combine, detect_language
from .models import (
    Block,
    Document,
    ExtractionMethod,
    Page,
    SourceFile,
    VerificationStatus,
    VisionVerification,
)
from .ocr import get_ocr_engine
from .ocr.base import OCRError, OCRPageResult
from .quality import GateDecision, evaluate_page
from .storage import absolute_path
from .verification import (
    Region,
    VisionBudget,
    priority,
    reconcile,
    vision_reader,
    word_confidences,
)

logger = logging.getLogger(__name__)


def known_parties(contract) -> tuple[str, ...]:
    """The contract's party names from its metadata (§ critical values).

    The client, the AJi entity and every named party, in both languages. A
    reading that changes one of these is a critical mismatch.
    """
    if contract is None:
        return ()
    names = [getattr(contract, "client_name", "")]
    entity = getattr(contract, "entity", None)
    if entity is not None:
        names += [getattr(entity, "name", ""), getattr(entity, "name_ar", "")]
    try:
        for party in contract.parties.all():
            names += [party.name, party.name_ar]
    except Exception:          # unsaved contract, or no parties relation
        pass
    return tuple(dict.fromkeys(n.strip() for n in names if n and len(n.strip()) >= 3))


@dataclass
class ExtractionOutcome:
    document: Document
    pages_native: int = 0
    pages_ocr: int = 0
    pages_failed: int = 0
    blocks_created: int = 0
    regions_sent_to_vision: int = 0
    conflicts: int = 0
    needs_human_review: bool = False


# ── stage 1: native ──────────────────────────────────────────────────────

@transaction.atomic
def extract_document(source_file: SourceFile) -> Document:
    """Create the canonical document from native text where it exists."""
    path = absolute_path(source_file.stored_path)

    document, _ = Document.objects.get_or_create(
        source_file=source_file,
        defaults={"contract": source_file.contract},
    )
    document.extraction_started_at = timezone.now()
    document.save(update_fields=["extraction_started_at", "updated_at"])

    try:
        parsed = extractors.extract(path, content_type=source_file.content_type)
    except extractors.EncryptedDocumentError as exc:
        document.extraction_error = str(exc)
        document.verification_status = VerificationStatus.FAILED
        document.extraction_finished_at = timezone.now()
        document.save(update_fields=[
            "extraction_error", "verification_status", "extraction_finished_at", "updated_at",
        ])
        source_file.is_encrypted = True
        source_file.save(update_fields=["is_encrypted", "updated_at"])
        raise
    except extractors.ExtractionError as exc:
        document.extraction_error = str(exc)
        document.verification_status = VerificationStatus.FAILED
        document.extraction_finished_at = timezone.now()
        document.save(update_fields=[
            "extraction_error", "verification_status", "extraction_finished_at", "updated_at",
        ])
        raise

    is_pdf = str(path).lower().endswith(".pdf")
    method = ExtractionMethod.NATIVE_PDF if is_pdf else ExtractionMethod.DOCX

    document.page_count = parsed.page_count
    document.native_text_coverage = parsed.native_text_coverage
    document.requires_ocr = parsed.requires_ocr
    document.save(update_fields=[
        "page_count", "native_text_coverage", "requires_ocr", "updated_at",
    ])

    source_file.page_count = parsed.page_count
    source_file.save(update_fields=["page_count", "updated_at"])

    # Rebuild cleanly on a retry rather than accumulating duplicates.
    document.pages.all().delete()

    parties = known_parties(source_file.contract)
    for parsed_page in parsed.pages:
        page = Page.objects.create(
            document=document,
            page_number=parsed_page.page_number,
            width=parsed_page.width,
            height=parsed_page.height,
            rotation=parsed_page.rotation,
            has_native_text=parsed_page.has_native_text,
            extraction_method=method if parsed_page.has_native_text else ExtractionMethod.OCR,
            verification_status=(
                VerificationStatus.ACCEPTED if parsed_page.has_native_text
                else VerificationStatus.UNVERIFIED
            ),
        )
        if not parsed_page.has_native_text:
            continue      # left for the OCR stage

        for block in parsed_page.blocks:
            bbox = block.bbox
            Block.objects.create(
                page=page,
                document=document,
                block_index=block.reading_order,
                original_source_text=block.text,
                bbox_x0=bbox.x0 if bbox else None,
                bbox_y0=bbox.y0 if bbox else None,
                bbox_x1=bbox.x1 if bbox else None,
                bbox_y1=bbox.y1 if bbox else None,
                extraction_method=method,
                # Native text is exact; there is no confidence to report and
                # pretending otherwise would be inventing a measurement.
                ocr_confidence=None,
                verification_status=VerificationStatus.ACCEPTED,
                contains_critical_value=contains_critical_value(block.text, parties),
                language=detect_language(block.text),
            )

        # The page's language, rolled up from the text actually on it.
        page.language = combine(
            list(page.blocks.values_list("language", flat=True))
        )
        page.save(update_fields=["language", "updated_at"])

    # Nothing on the native-text path used to set a language at all, so the
    # analysis prompts fell back to Arabic for every document, including
    # English ones.
    document.detected_language = combine(
        list(document.pages.values_list("language", flat=True))
    )
    document.save(update_fields=["detected_language", "updated_at"])

    audit.record(
        AuditAction.ANALYSIS_STARTED,
        entity_type="Document",
        entity_id=document.id,
        contract_id=document.contract_id,
        details={
            "stage": "native_extraction",
            "pages": parsed.page_count,
            "native_coverage": round(parsed.native_text_coverage, 3),
            "requires_ocr": parsed.requires_ocr,
            "detected_language": document.detected_language or "undetermined",
        },
    )
    return document


# ── stage 2: OCR the pages that need it ──────────────────────────────────

def ocr_page(document: Document, page: Page, *, languages: str | None = None) -> OCRPageResult:
    """Prepare, read and store one scanned page (apps/documents/ocr/pipeline.py).

    The page is cleaned of stamp ink, turned upright and straightened before
    it is read; lines the layout skipped are read again, and the figures of
    key Arabic lines get a second reading. Each block keeps exactly what the
    engine read as its evidence; repaired text and the record of every
    repair are stored beside it.
    """
    from .ocr.pipeline import read_page

    engine = get_ocr_engine()
    available, detail = engine.is_available()
    if not available:
        raise OCRError(detail)

    path = absolute_path(document.source_file.stored_path)
    parties = known_parties(document.contract)
    reading = read_page(path, page.page_number, engine=engine, languages=languages,
                        known_terms=parties)
    result = reading.result

    page.blocks.all().delete()
    for block in result.blocks:
        x0, y0, x1, y1 = block.bbox.as_tuple()       # already PDF points
        repaired = reading.corrected.get(block.reading_order)
        text_for_checks = repaired.text if repaired else block.text
        Block.objects.create(
            page=page,
            document=document,
            block_index=block.reading_order,
            original_source_text=block.text,
            corrected_text=repaired.text if repaired else "",
            corrections=[c.as_dict() for c in repaired.corrections] if repaired else [],
            bbox_x0=x0,
            bbox_y0=y0,
            bbox_x1=x1,
            bbox_y1=y1,
            extraction_method=ExtractionMethod.OCR,
            ocr_confidence=block.confidence,
            min_word_confidence=block.min_word_confidence,
            word_confidences=word_confidences(block),
            language=detect_language(text_for_checks),
            verification_status=VerificationStatus.UNVERIFIED,
            contains_critical_value=(contains_critical_value(text_for_checks, parties)
                                     or bool(repaired)),
        )

    page.extraction_method = ExtractionMethod.OCR
    page.ocr_confidence = result.mean_confidence
    page.ocr_rotation = reading.transform.rotate
    page.quality_signals = {**(page.quality_signals or {}), "preparation": reading.signals}
    # `result.language` is the engine's *input* ("ara+eng"), not a detection.
    page.language = combine(list(page.blocks.values_list("language", flat=True)))
    page.save(update_fields=[
        "extraction_method", "ocr_confidence", "ocr_rotation", "quality_signals",
        "language", "updated_at",
    ])
    return result


# ── stage 3: the quality gate ────────────────────────────────────────────

def apply_quality_gate(page: Page, result: OCRPageResult) -> GateDecision:
    """Record the gate's evidence and set the page's verification status."""
    gate = evaluate_page(result)

    # Kept beside what the page preparation recorded, not over it.
    preparation = (page.quality_signals or {}).get("preparation")
    page.quality_signals = gate.signals.as_dict()
    if preparation:
        page.quality_signals["preparation"] = preparation
    page.verification_status = {
        GateDecision.ACCEPT: VerificationStatus.ACCEPTED,
        GateDecision.ACCEPT_WITH_VALIDATION: VerificationStatus.AUTO_VALIDATED,
        GateDecision.VISION_VERIFY: VerificationStatus.UNVERIFIED,
        GateDecision.VISION_VERIFY_AND_RETRY: VerificationStatus.UNVERIFIED,
    }[gate.decision]
    page.save(update_fields=["quality_signals", "verification_status", "updated_at"])

    # Flag the specific blocks the gate singled out, so vision work is targeted
    # rather than re-reading the whole page.
    if gate.blocks_needing_vision:
        Block.objects.filter(
            page=page, block_index__in=gate.blocks_needing_vision
        ).update(verification_status=VerificationStatus.UNVERIFIED)

    return gate.decision


# ── stage 4: vision verification of specific regions ─────────────────────

#: The first vision look: the OCR box with a little margin, at 220 dpi.
VISION_CROP = {"dpi": 220, "padding": 6.0}
#: The second, closer look at a disputed critical value: tighter and sharper.
VISION_CROP_TIGHT = {"dpi": 300, "padding": 2.0}

_VERIFIED_STATES = {
    VerificationStatus.VERIFIED,
    VerificationStatus.AUTO_RESOLVED,
    VerificationStatus.VISION_REVERIFIED,
}


def _block_region(block: Block, parties=()) -> Region:
    # The repaired OCR reading ("AJi Water" for "Adi Water", "10%" for
    # "1096"), not the raw one: a reading vision agrees with becomes the
    # block's final text, and the raw text there undid every repair. The raw
    # text itself is still kept untouched on the block.
    return Region(
        ocr_text=block.corrected_text or block.original_source_text,
        page_number=block.page.page_number,
        bbox=block.bbox,
        ocr_confidence=block.ocr_confidence,
        min_word_confidence=block.min_word_confidence,
        word_confidences=block.word_confidences or [],
        language=block.language or "ar",
        known_parties=tuple(parties),
        is_native=block.extraction_method in (ExtractionMethod.NATIVE_PDF, ExtractionMethod.DOCX),
    )


def verify_block_with_vision(
    block: Block, *, reason: str, budget: VisionBudget | None = None, parties=None,
) -> VisionVerification:
    """Verify one OCR region with independent vision readings (§9, §10).

    The decision is `verification.reconcile`; this stores its evidence. The
    original OCR text is never edited. One VisionVerification row is written
    per vision pass (pass 0 when no pass could be made), and the block keeps
    the final text, the status and why.
    """
    document = block.document
    path = absolute_path(document.source_file.stored_path)
    if parties is None:
        parties = known_parties(document.contract)
    region = _block_region(block, parties)

    def crop(tight: bool) -> bytes:
        opts = VISION_CROP_TIGHT if tight else VISION_CROP
        # Turned the way OCR turned the page: a sideways table is read upright.
        return extractors.crop_region(path, region.page_number, region.bbox,
                                      rotate=block.page.ocr_rotation, **opts)

    read = vision_reader(language=region.language, page_number=region.page_number,
                         bbox=region.bbox, contract_id=document.contract_id)
    resolution = reconcile(region, crop, read, budget, reason=reason)

    block.verification_status = resolution.status
    block.final_text = resolution.final_text
    block.mismatch_reason = resolution.mismatch_reason
    if resolution.passes:
        block.vision_text = resolution.passes[0].text
        confidences = [p.confidence for p in resolution.passes if p.confidence is not None]
        block.vision_confidence = confidences[-1] if confidences else None
    if resolution.status in _VERIFIED_STATES:
        block.extraction_method = ExtractionMethod.OCR_VERIFIED_BY_VISION
    block.save(update_fields=[
        "verification_status", "final_text", "mismatch_reason", "vision_text",
        "vision_confidence", "extraction_method", "updated_at",
    ])

    rows = []
    bbox = list(region.bbox) if region.bbox else None
    for record in resolution.passes or [None]:
        if record is None:
            outcome = VisionVerification.Outcome.INCONCLUSIVE
        elif not record.ok:
            outcome = VisionVerification.Outcome.ERROR
        elif record.number == 1:
            outcome = (VisionVerification.Outcome.AGREE
                       if resolution.status in (VerificationStatus.VERIFIED,
                                                VerificationStatus.AUTO_RESOLVED)
                       else VisionVerification.Outcome.DISAGREE)
        else:
            outcome = (VisionVerification.Outcome.AGREE
                       if resolution.status == VerificationStatus.VISION_REVERIFIED
                       else VisionVerification.Outcome.INCONCLUSIVE)
        rows.append(VisionVerification.objects.create(
            block=block,
            reason=reason if record is None or record.number == 1 else "critical_recheck",
            ocr_text=block.original_source_text,
            vision_text=record.text if record else "",
            outcome=outcome,
            provider=record.provider if record else "",
            model=record.model if record else "",
            notes=((record.error or record.notes) if record else resolution.mismatch_reason)[:1000],
            pass_number=record.number if record else 0,
            vision_confidence=record.confidence if record else None,
            status=resolution.status,
            final_text=resolution.final_text,
            mismatch_reason=resolution.mismatch_reason,
            similarity=record.similarity if record else None,
            page_number=region.page_number,
            bbox=bbox,
        ))

    details = {
        "page": region.page_number,
        "reason": reason,
        "status": resolution.status,
        "passes": len(resolution.passes),
        "critical_value": block.contains_critical_value,
        "mismatch": resolution.mismatch_reason,
    }
    if resolution.unresolved and block.contains_critical_value:
        audit.record(AuditAction.SOURCE_CONFLICT, entity_type="Block", entity_id=block.pk,
                     contract_id=document.contract_id, details=details)
    else:
        audit.record(AuditAction.VISION_VERIFICATION, entity_type="Block", entity_id=block.pk,
                     contract_id=document.contract_id, details=details)
    return rows[-1]


# ── the whole pipeline ───────────────────────────────────────────────────

def _reuse_same_contract_extraction(source_file: SourceFile) -> Document | None:
    """The same bytes already extracted for this contract (§ SHA-256 reuse).

    Scoped to the same contract: a re-upload of an identical file -- another
    round under a new name -- copies the stored pages, blocks and their
    verification evidence instead of running OCR and vision again. Never
    across contracts, never across owners.
    """
    earlier = (
        Document.objects.filter(
            contract=source_file.contract,
            source_file__sha256=source_file.sha256,
            extraction_finished_at__isnull=False,
        )
        .exclude(source_file=source_file)
        .exclude(verification_status=VerificationStatus.FAILED)
        .order_by("-extraction_finished_at")
        .first()
    )
    if earlier is None or not source_file.sha256:
        return None

    with transaction.atomic():
        document, _ = Document.objects.get_or_create(
            source_file=source_file, defaults={"contract": source_file.contract})
        document.pages.all().delete()
        for field in ("detected_language", "page_count", "native_text_coverage",
                      "requires_ocr", "overall_confidence", "verification_status"):
            setattr(document, field, getattr(earlier, field))
        document.extraction_started_at = timezone.now()
        document.extraction_finished_at = timezone.now()
        document.extraction_error = ""
        document.save()
        for old_page in earlier.pages.all():
            old_blocks = list(old_page.blocks.all())
            old_page.pk = None
            old_page.document = document
            old_page.save()
            for block in old_blocks:
                block.pk = None
                block.page = old_page
                block.document = document
                block.save()
        source_file.page_count = earlier.page_count
        source_file.save(update_fields=["page_count", "updated_at"])
    audit.record(
        AuditAction.ANALYSIS_STARTED, entity_type="Document", entity_id=document.id,
        contract_id=document.contract_id,
        details={"stage": "extraction_reused", "from_document": str(earlier.pk),
                 "sha256": source_file.sha256},
    )
    return document


def _propagate_disputes(document: Document, parties=()) -> int:
    """Hold for review every unchecked OCR block that repeats a value vision
    disputed elsewhere in the document (verification.disputed_values).
    Returns how many blocks with a critical value were held."""
    from .verification import disputed_values, shares_a_disputed_value

    disputed = disputed_values(
        ((b.original_source_text, b.vision_text, b.page.page_number)
         for b in document.blocks.filter(verification_status=VerificationStatus.NEEDS_HUMAN_REVIEW)
         .select_related("page")),
        parties)
    if not disputed:
        return 0
    held = 0
    unchecked = document.blocks.filter(
        extraction_method=ExtractionMethod.OCR,
        verification_status__in=[VerificationStatus.UNVERIFIED, VerificationStatus.ACCEPTED,
                                 VerificationStatus.AUTO_VALIDATED])
    for block in unchecked:
        why = shares_a_disputed_value(block.original_source_text, disputed, parties)
        if why:
            block.verification_status = VerificationStatus.NEEDS_HUMAN_REVIEW
            block.final_text = ""
            block.mismatch_reason = why
            block.save(update_fields=["verification_status", "final_text", "mismatch_reason", "updated_at"])
            held += block.contains_critical_value
    return held


def run_extraction(source_file: SourceFile) -> ExtractionOutcome:
    """Native → OCR where needed → vision where needed."""
    reused = _reuse_same_contract_extraction(source_file)
    if reused is not None:
        outcome = ExtractionOutcome(document=reused, blocks_created=reused.blocks.count())
        outcome.pages_native = reused.pages.filter(has_native_text=True).count()
        outcome.pages_ocr = reused.pages.count() - outcome.pages_native
        outcome.conflicts = sum(b.unresolved_critical for b in reused.blocks.all())
        outcome.needs_human_review = reused.verification_status == VerificationStatus.NEEDS_HUMAN_REVIEW
        return outcome

    from django.conf import settings

    document = extract_document(source_file)
    outcome = ExtractionOutcome(document=document)
    parties = known_parties(source_file.contract)
    # A long scanned file gets a vision allowance in proportion to its
    # scanned pages; the fixed per-file limit stays the floor.
    scanned = document.pages.filter(has_native_text=False).count()
    budget = VisionBudget(per_file=max(settings.VISION_MAX_CALLS_PER_FILE,
                                       int(scanned * settings.VISION_CALLS_PER_SCANNED_PAGE)))
    use_vision = settings.VISION_VERIFICATION_ENABLED

    for page in document.pages.all():
        if page.has_native_text:
            # Native text is exact: no OCR, no vision.
            outcome.pages_native += 1
            continue

        try:
            result = ocr_page(document, page)
        except OCRError as exc:
            outcome.pages_failed += 1
            page.verification_status = VerificationStatus.FAILED
            page.quality_signals = {"error": str(exc)[:500]}
            page.save(update_fields=["verification_status", "quality_signals", "updated_at"])
            logger.warning("OCR failed on page %s: %s", page.page_number, exc)
            continue

        outcome.pages_ocr += 1
        decision = apply_quality_gate(page, result)
        if not use_vision:
            # Vision verification switched off: uncertain readings stay
            # unverified and are put to the reviewer as key facts instead.
            continue

        if decision in {GateDecision.VISION_VERIFY, GateDecision.VISION_VERIFY_AND_RETRY}:
            targets = list(page.blocks.all())
            reason = "bad_scan" if decision == GateDecision.VISION_VERIFY_AND_RETRY else "low_confidence"
        else:
            # Even on an accepted page, critical values below the stricter bar
            # are verified individually (§9) -- the blocks the gate flagged,
            # not every block that happens to hold a number: a confidently
            # read "0.5%" needs no second reader.
            flagged = set(evaluate_page(result).blocks_needing_vision)
            targets = [b for b in page.blocks.filter(verification_status=VerificationStatus.UNVERIFIED)
                       if b.block_index in flagged or (
                           b.contains_critical_value and b.min_word_confidence is None)]
            reason = "critical_value"

        # Critical values first, least confident first: when the vision limit
        # is reached, what is left over is the least important (§13).
        targets.sort(key=lambda b: priority(b.contains_critical_value,
                                            b.min_word_confidence, b.ocr_confidence))
        for block in targets:
            block_reason = "critical_value" if block.contains_critical_value else reason
            verify_block_with_vision(block, reason=block_reason, budget=budget, parties=parties)
            block.refresh_from_db(fields=["verification_status"])
            outcome.regions_sent_to_vision += 1
            if block.unresolved_critical:
                outcome.conflicts += 1

    outcome.conflicts += _propagate_disputes(document, parties)
    outcome.blocks_created = document.blocks.count()

    conflicted = document.blocks.filter(
        verification_status__in=[
            VerificationStatus.SOURCE_CONFLICT,
            VerificationStatus.NEEDS_HUMAN_REVIEW,
        ]
    ).exists()
    outcome.needs_human_review = conflicted or outcome.pages_failed > 0

    document.overall_confidence = _overall_confidence(document)
    document.verification_status = (
        VerificationStatus.NEEDS_HUMAN_REVIEW if outcome.needs_human_review
        else VerificationStatus.VERIFIED
    )
    document.extraction_finished_at = timezone.now()
    document.save(update_fields=[
        "overall_confidence", "verification_status", "extraction_finished_at", "updated_at",
    ])
    return outcome


def _overall_confidence(document: Document) -> float:
    """Source-extraction quality for the document as a whole.

    Native pages count as fully reliable; OCR pages contribute their measured
    confidence. A document with no text at all scores zero rather than being
    treated as perfect.
    """
    pages = list(document.pages.all())
    if not pages:
        return 0.0
    scores = [100.0 if p.has_native_text else (p.ocr_confidence or 0.0) for p in pages]
    return sum(scores) / len(scores)
