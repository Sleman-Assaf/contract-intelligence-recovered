"""Chat attachments: read once, stored, answered from (§ chat files).

A file attached to an assistant chat used to go to the AI server again on
every question: the PDF's pages as text, a scan's pages as pictures, a photo
as itself. A follow-up about the same file paid the full cost again, and a
scan was never OCR'd at all.

Now a file is processed once, when it is attached:

    native PDF text          -> used as it is (no OCR, no vision)
    scanned page / photo     -> Tesseract -> quality gate -> vision verification
                                of the regions that need it (verification.py)

and what comes out is stored: every region with all of its readings
(ExtractedRegion), and the final text split into clauses (ExtractedChunk). A
question is then answered from the few chunks it needs. Vision is used again
only for a visual question (a signature, a stamp, the layout) or when the
answer rests on a value nobody could confirm.

Keyed by owner and SHA-256: the same bytes attached again by the same person
-- any name, any of their chats -- reuse the stored extraction. Another
person's extraction of the same bytes is never reused.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from . import extractors
from .critical_values import contains_critical_value
from .language import detect_language
from .models import (
    ExtractedChunk,
    ExtractedFile,
    ExtractedRegion,
    ExtractionMethod,
    VerificationStatus,
)
from .verification import Region, VisionBudget, priority, reconcile, vision_reader, word_confidences

logger = logging.getLogger(__name__)

MAX_MB = 10
PDF_MAX_PAGES = 30
IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg")}

#: A clause longer than this is split into passages, so one huge preamble
#: does not have to travel whole for a question about one line of it.
CHUNK_MAX_CHARS = 3000
#: Text sent for a question about something specific.
ASK_MAX_CHARS = 6000
#: Text sent for "translate it" / "continue", in order -- a translation is
#: as long as its source, so it goes in parts the reply has room for.
FULL_MAX_CHARS = 15_000
#: A file up to this size is sent whole for any question that does not name a
#: page or a clause. Only its start (6,000 characters) used to go, so an
#: 11-page contract was read as its first three pages: "how many pages?",
#: "did you read it all?" and -- asked in Arabic of an English contract, where
#: no word matches -- "what is the liability cap?" were all answered from
#: pages 1 to 3. About 12-15 pages; well inside the model's window.
WHOLE_FILE_MAX_CHARS = 32_000
#: Pictures sent with a question: page renders for a visual question, or
#: crops of unresolved values. Each costs image tokens on the AI server.
MAX_IMAGES = 3

UNVERIFIED_OPEN = "⟦UNVERIFIED⟧"
UNVERIFIED_CLOSE = "⟦/UNVERIFIED⟧"

_UNRESOLVED = (VerificationStatus.NEEDS_HUMAN_REVIEW, VerificationStatus.SOURCE_CONFLICT)


# ── accepting a file ─────────────────────────────────────────────────────

def sniff(data: bytes) -> tuple[str, str, str]:
    """(kind, extension, mime) judged by the bytes, never the name."""
    if data.startswith(b"%PDF"):
        return ExtractedFile.Kind.PDF, "pdf", "application/pdf"
    for magic, (ext, mime) in IMAGE_TYPES.items():
        if data.startswith(magic):
            return ExtractedFile.Kind.IMAGE, ext, mime
    raise ValidationError(_("الملف المرفق ليس صورة PNG أو JPEG ولا ملف PDF."))


def pdf_page_count(data: bytes) -> int:
    """Pages of a PDF, refusing what cannot be read (ValidationError)."""
    import pymupdf

    try:
        pdf = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValidationError(_("تعذّر قراءة ملف PDF.")) from exc
    try:
        if pdf.needs_pass:
            raise ValidationError(_("ملف PDF محمي بكلمة مرور ولا يمكن قراءته."))
        count = pdf.page_count
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(_("تعذّر قراءة ملف PDF.")) from exc
    finally:
        pdf.close()
    if count < 1:
        raise ValidationError(_("تعذّر قراءة ملف PDF."))
    if count > PDF_MAX_PAGES:
        raise ValidationError(
            _("ملف PDF يحتوي على أكثر من %(n)s صفحة؛ ارفعه كعقد بدلاً من إرفاقه.")
            % {"n": PDF_MAX_PAGES})
    return count


def absolute(file: ExtractedFile) -> Path | None:
    root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    target = (root / file.stored_path).resolve()
    return target if target.is_relative_to(root) else None


def _stale(file: ExtractedFile) -> bool:
    started = file.processing_started_at or file.created_at
    return started < timezone.now() - timedelta(minutes=settings.EXTRACTION_STALE_MINUTES)


def ingest(user, data: bytes, filename: str) -> ExtractedFile:
    """Accept an attachment and make sure it has been read, once.

    The same person attaching the same bytes again gets the stored
    extraction back: no OCR, no vision. A FAILED extraction, or one stuck
    PROCESSING past EXTRACTION_STALE_MINUTES, is read again.
    Raises ValidationError with a message for the reader.
    """
    from apps.documents.storage import safe_filename

    if len(data) > MAX_MB * 1024 * 1024:
        raise ValidationError(_("الملف أكبر من الحد المسموح (%(mb)s ميغابايت).") % {"mb": MAX_MB})
    kind, ext, _mime = sniff(data)
    pages = pdf_page_count(data) if kind == ExtractedFile.Kind.PDF else 1
    digest = hashlib.sha256(data).hexdigest()
    name = safe_filename(filename or f"attachment.{ext}")[:200]

    # One file per person and content, so a re-upload never adds a copy.
    relative = f"advisor/{user.pk}/{digest}.{ext}"
    existing = ExtractedFile.objects.filter(owner=user, sha256=digest).first()
    if existing is None:
        try:
            with transaction.atomic():
                existing = ExtractedFile.objects.create(
                    owner=user, sha256=digest, kind=kind, original_filename=name,
                    stored_path=relative, size_bytes=len(data), page_count=pages,
                    status=ExtractedFile.Status.PROCESSING,
                    processing_started_at=timezone.now(),
                )
            created = True
        except IntegrityError:          # the same file, attached twice at once
            existing = ExtractedFile.objects.get(owner=user, sha256=digest)
            created = False
    else:
        created = False

    target = absolute(existing)
    if target is not None and not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    if not created:
        if existing.status in (ExtractedFile.Status.READY, ExtractedFile.Status.NEEDS_REVIEW):
            logger.info("Chat file %s reused for %s (sha256 %s)", existing.pk, user.pk, digest[:12])
            return existing
        if existing.status == ExtractedFile.Status.PROCESSING and not _stale(existing):
            return existing
    schedule(existing)
    existing.refresh_from_db()
    return existing


def schedule(file: ExtractedFile) -> None:
    """Process now, or on a worker when EXTRACTION_ASYNC says one runs.

    Off by default: with no worker the file would sit PROCESSING forever, so
    it is processed in the request -- the upload takes as long as reading it.
    """
    ExtractedFile.objects.filter(pk=file.pk).update(
        status=ExtractedFile.Status.PROCESSING, processing_started_at=timezone.now(),
        processing_finished_at=None, error="")
    if getattr(settings, "EXTRACTION_ASYNC", False):
        try:
            from .tasks import process_chat_file

            process_chat_file.delay(str(file.pk))
            return
        except Exception as exc:      # broker down: do it here instead
            logger.warning("Could not queue chat file %s (%s); processing inline", file.pk, exc)
    process(file.pk)


# ── reading it ───────────────────────────────────────────────────────────

def process(file_id) -> ExtractedFile:
    """Extract, verify, store regions and chunks. Never raises: a failure
    is recorded on the file (FAILED), and the chat then falls back to
    sending the file itself."""
    file = ExtractedFile.objects.get(pk=file_id)
    file.regions.all().delete()
    file.chunks.all().delete()
    budget = VisionBudget()
    try:
        path = absolute(file)
        if path is None or not path.is_file():
            raise extractors.ExtractionError("The stored file is missing.")
        if file.kind == ExtractedFile.Kind.PDF:
            _process_pdf(file, path, budget)
        else:
            _process_image(file, path.read_bytes(), budget)
        _propagate_disputes(file)
        _store_text_and_chunks(file)
        unresolved = file.regions.filter(status__in=_UNRESOLVED).exists()
        file.status = ExtractedFile.Status.NEEDS_REVIEW if unresolved else ExtractedFile.Status.READY
        file.error = ""
    except Exception as exc:
        logger.warning("Chat file %s could not be read: %s", file.pk, exc)
        file.status = ExtractedFile.Status.FAILED
        file.error = str(exc)[:2000]
    file.vision_calls = budget.used
    file.processing_finished_at = timezone.now()
    file.save()
    return file


def _ocr_engine():
    from .ocr import get_ocr_engine
    from .ocr.base import OCRError

    engine = get_ocr_engine()
    ok, detail = engine.is_available()
    if not ok:
        raise OCRError(detail)
    return engine


def _process_pdf(file: ExtractedFile, path: Path, budget: VisionBudget) -> None:
    parsed = extractors.extract(path, content_type="application/pdf")
    file.page_count = parsed.page_count
    for page in parsed.pages:
        if page.has_native_text:
            # Exact: no OCR, no vision.
            file.native_text = True
            for block in page.blocks:
                if not (block.text or "").strip():
                    continue
                box = block.bbox.as_tuple() if block.bbox else (None,) * 4
                ExtractedRegion.objects.create(
                    file=file, page_number=page.page_number, block_index=block.reading_order,
                    bbox_x0=box[0], bbox_y0=box[1], bbox_x1=box[2], bbox_y1=box[3],
                    bbox_units="pt", extraction_method=ExtractionMethod.NATIVE_PDF,
                    source_text=block.text, final_text=block.text,
                    contains_critical_value=contains_critical_value(block.text),
                    status=VerificationStatus.ACCEPTED,
                )
            continue
        image = extractors.rasterise_page(path, page.page_number, dpi=extractors.OCR_RENDER_DPI)

        def crop(bbox, tight, _n=page.page_number):
            opts = {"dpi": 300, "padding": 2.0} if tight else {"dpi": 220, "padding": 6.0}
            return extractors.crop_region(path, _n, bbox, **opts)

        _ocr_and_verify(file, page.page_number, image, crop, budget, units="pt")


def _process_image(file: ExtractedFile, data: bytes, budget: VisionBudget) -> None:
    file.page_count = 1

    def crop(bbox, tight):
        return extractors.crop_image(data, bbox, padding=2.0 if tight else 6.0,
                                     upscale=2.0 if tight else 1.0)

    _ocr_and_verify(file, 1, data, crop, budget, units="px")


def _ocr_and_verify(file, page_number, image, crop, budget, *, units) -> None:
    """Tesseract, the quality gate, then vision on the regions that need it."""
    from .quality import GateDecision, evaluate_page

    result = _ocr_engine().recognise_page(
        image, page_number=page_number, languages=settings.OCR_LANGUAGES)
    gate = evaluate_page(result)
    whole_page = gate.decision in (GateDecision.VISION_VERIFY, GateDecision.VISION_VERIFY_AND_RETRY)
    page_status = {
        GateDecision.ACCEPT: VerificationStatus.ACCEPTED,
        GateDecision.ACCEPT_WITH_VALIDATION: VerificationStatus.AUTO_VALIDATED,
    }.get(gate.decision, VerificationStatus.UNVERIFIED)

    regions = []
    for index, block in enumerate(result.blocks):
        if not (block.text or "").strip():
            continue
        box = block.bbox.as_tuple()
        if units == "pt":
            box = extractors.pixels_to_points(box, dpi=extractors.OCR_RENDER_DPI)
        region = ExtractedRegion.objects.create(
            file=file, page_number=page_number, block_index=block.reading_order,
            bbox_x0=box[0], bbox_y0=box[1], bbox_x1=box[2], bbox_y1=box[3], bbox_units=units,
            extraction_method=ExtractionMethod.OCR, source_text=block.text,
            ocr_confidence=block.confidence, min_word_confidence=block.min_word_confidence,
            word_confidences=word_confidences(block),
            contains_critical_value=contains_critical_value(block.text),
            status=page_status, final_text=block.text,
        )
        if whole_page or index in gate.blocks_needing_vision:
            regions.append(region)

    # Critical values first, least confident first: past the limit, what is
    # left is the least important (§13).
    regions.sort(key=lambda r: priority(r.contains_critical_value, r.min_word_confidence,
                                        r.ocr_confidence))
    for region in regions:
        verify_region(region, crop, budget)


def verify_region(region: ExtractedRegion, crop, budget: VisionBudget | None = None) -> None:
    """Run the three-tier check on one stored region and keep every reading."""
    subject = Region(
        ocr_text=region.source_text, page_number=region.page_number, bbox=region.bbox,
        ocr_confidence=region.ocr_confidence, min_word_confidence=region.min_word_confidence,
        word_confidences=region.word_confidences or [],
        language=detect_language(region.source_text) or "ar",
    )
    read = vision_reader(language=subject.language, page_number=subject.page_number,
                         bbox=subject.bbox)
    resolution = reconcile(subject, lambda tight: crop(subject.bbox, tight), read, budget)

    region.status = resolution.status
    region.final_text = resolution.final_text
    region.mismatch_reason = resolution.mismatch_reason
    region.similarity = resolution.similarity
    for record in resolution.passes:
        if record.number == 1:
            region.vision_pass1_text, region.vision_pass1_confidence = record.text, record.confidence
        else:
            region.vision_pass2_text, region.vision_pass2_confidence = record.text, record.confidence
        if record.provider:
            region.provider, region.model = record.provider, record.model
    if resolution.passes:
        region.verified_at = timezone.now()
        region.extraction_method = (ExtractionMethod.OCR_VERIFIED_BY_VISION
                                    if not resolution.unresolved else ExtractionMethod.OCR)
    region.save()


#: OCR regions no vision reading checked -- the ones a dispute elsewhere in
#: the file can call into question.
_UNCHECKED = (VerificationStatus.ACCEPTED, VerificationStatus.AUTO_VALIDATED,
              VerificationStatus.UNVERIFIED)


def _propagate_disputes(file: ExtractedFile) -> None:
    """Hold for review every unchecked OCR reading of a value that vision
    disputed elsewhere in the file (verification.disputed_values)."""
    from .verification import disputed_values, shares_a_disputed_value

    disputed = disputed_values(
        (r.source_text, r.vision_pass1_text, r.page_number)
        for r in file.regions.filter(status__in=_UNRESOLVED))
    if not disputed:
        return
    for region in file.regions.filter(status__in=_UNCHECKED, extraction_method=ExtractionMethod.OCR):
        why = shares_a_disputed_value(region.source_text, disputed)
        if why:
            region.status = VerificationStatus.NEEDS_HUMAN_REVIEW
            region.final_text = ""
            region.mismatch_reason = why
            region.save(update_fields=["status", "final_text", "mismatch_reason", "updated_at"])


# ── the stored text and its chunks ───────────────────────────────────────

def _marked(region: ExtractedRegion) -> str:
    """A region's text as a question is answered from it.

    An unresolved region carries both readings -- OCR's and the first vision
    reading -- inside the markers, so neither is taken as confirmed and the
    model can still see what the region probably says. On a bad scan OCR's
    reading alone can be noise.
    """
    text = region.usable_text.strip()
    if region.status not in _UNRESOLVED:
        return text
    vision = (region.vision_pass1_text or "").strip()
    if vision and vision != region.source_text.strip():
        return (f"{UNVERIFIED_OPEN}OCR read: {region.source_text.strip()} || "
                f"a vision reading gave: {vision}{UNVERIFIED_CLOSE}")
    return f"{UNVERIFIED_OPEN}{text}{UNVERIFIED_CLOSE}"


def _structure_text(region: ExtractedRegion) -> str:
    """The text clause grouping reads. For an unresolved region the vision
    reading, when there is one, shows the clause numbers more faithfully than
    OCR noise -- used to find the structure only, never as the region's text."""
    if region.status in _UNRESOLVED and (region.vision_pass1_text or "").strip():
        return region.vision_pass1_text
    return region.usable_text


def _store_text_and_chunks(file: ExtractedFile) -> None:
    from apps.analysis.clause_detection import TextUnit, group_into_clauses

    regions = list(file.regions.order_by("page_number", "block_index"))
    by_id = {r.pk: r for r in regions}

    pages: dict[int, list[str]] = {}
    for r in regions:
        pages.setdefault(r.page_number, []).append(_marked(r))
    file.text = "\n\n".join(f"[Page {n}]\n" + "\n".join(parts) for n, parts in sorted(pages.items()))

    clauses = group_into_clauses(TextUnit(r.pk, _structure_text(r), r.page_number) for r in regions)
    ordinal = 0
    for clause in clauses:
        members = [by_id[i] for i in clause.block_ids if i in by_id]
        if not members:
            continue
        # Passages of at most CHUNK_MAX_CHARS, cut between regions.
        groups, current, size = [], [], 0
        for r in members:
            length = len(r.usable_text)
            if current and size + length > CHUNK_MAX_CHARS:
                groups.append(current)
                current, size = [], 0
            current.append(r)
            size += length
        if current:
            groups.append(current)
        for part, group in enumerate(groups):
            ExtractedChunk.objects.create(
                file=file, ordinal=ordinal,
                clause_number=clause.clause_number,
                heading=(clause.heading if part == 0 else f"{clause.heading} (cont.)".strip())[:512],
                page_start=min(r.page_number for r in group),
                page_end=max(r.page_number for r in group),
                text="\n".join(_marked(r) for r in group),
                unresolved_region_ids=[r.pk for r in group if r.status in _UNRESOLVED],
            )
            ordinal += 1


# ── answering from it ────────────────────────────────────────────────────

_WHOLE = re.compile(
    r"translat|summar|overview|whole|entire|all of it|full text|every (?:page|clause)"
    r"|read (?:it|them|all|everything|the (?:whole|rest))|all (?:the )?pages"
    r"|ترجم|لخص|لخّص|ملخص|تلخيص|كامل|بالكامل|كل البنود|جميع البنود|النص الكامل"
    r"|اقرأ|اقرا|إقرأ|قراءة كل|كل الصفحات|جميع الصفحات|كلها|كلهم",
    re.IGNORECASE)
#: A translation goes in parts whatever the size: the reply is as long as
#: the source (see FULL_MAX_CHARS and "continue").
_TRANSLATE = re.compile(r"translat|ترجم", re.IGNORECASE)
_PAGE_REF = re.compile(r"(?:\bpages?|\bp\.|صفحة|الصفحة|صفحه|ص\.)\s*([0-9٠-٩]{1,3})", re.IGNORECASE)
_CLAUSE_REF = re.compile(
    r"(?:clause|article|section|art\.|البند|بند|المادة|مادة|الفقرة|فقرة)\s*"
    r"([0-9٠-٩]{1,2}(?:[.٫][0-9٠-٩]{1,2}){0,3})", re.IGNORECASE)
_BARE_NUMBER = re.compile(r"(?<![\w.])([0-9٠-٩]{1,2}(?:[.٫][0-9٠-٩]{1,2}){1,3})(?![\w.%٪])")
#: Questions only the picture can answer.
#: Words are matched whole: "بصورة" and "بشكل" ("in a manner") or "رسمي"
#: ("official") are ordinary prose, and a contract that mentions "drawings" as
#: a deliverable is not a question about a picture.
VISUAL = re.compile(
    r"\b(?:signatures?|signed|sign(?:ed)? by|stamps?|stamped|seals?|sealed|logos?|handwrit\w*"
    r"|hand-writ\w*|layout|(?:the|this|in the) (?:drawing|diagram)|looks? like|appearance"
    r"|colou?rs?|pictures?|images?|photos?|table (?:look|design|appearance))\b"
    r"|(?<!\w)(?:توقيع|التوقيع|توقيعات|التوقيعات|موقع من|موقّع|مُوقّع|وقّع|ختم|الختم|مختوم|شعار|الشعار"
    r"|خط اليد|بخط اليد|مكتوب باليد|التنسيق|الرسم|شكل الجدول|لون|اللون|الصورة|صورة)(?!\w)",
    re.IGNORECASE)


def _ascii(text: str) -> str:
    from .critical_values import normalise_digits

    return normalise_digits(text).replace("٫", ".")


@dataclass
class Context:
    """What one question is given from the file."""

    excerpts: list[str] = field(default_factory=list)
    images: list = field(default_factory=list)
    chunks_sent: list[int] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    mode: str = ""
    #: True when the file itself (not its text) had to go: extraction failed.
    fallback: bool = False


def _within(chunks, budget):
    chosen, used = [], 0
    for chunk in chunks:
        if chosen and used + len(chunk.text) > budget:
            break
        chosen.append(chunk)
        used += len(chunk.text)
    return chosen


#: Words that say nothing about where to look, however rare they are in a
#: short file.
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "what", "which", "who", "whom", "how", "why",
    "when", "where", "does", "did", "are", "was", "were", "has", "have", "can", "could", "should",
    "would", "will", "from", "into", "about", "there", "their", "its", "any", "all", "please",
    "tell", "explain", "show", "give", "file", "document", "contract", "clause", "article",
    "page", "section", "summarise", "summarize", "summary", "translate", "translation",
    "ما", "ماذا", "هل", "كيف", "لماذا", "متى", "أين", "هذا", "هذه", "ذلك", "التي", "الذي",
    "في", "من", "على", "إلى", "الى", "عن", "مع", "كل", "الملف", "العقد", "البند", "المادة",
    "الصفحة", "اشرح", "لخص", "ترجم", "بين", "أو", "او",
}


def _telling_words(question, chunks):
    words = {w for w in re.findall(r"[^\W_]+(?:\.\d+)*", _ascii(question).lower())
             if (len(w) >= 3 or any(ch.isdigit() for ch in w)) and w not in _STOPWORDS}
    spread = {w: sum(1 for c in chunks if w in _ascii(c.text).lower()) for w in words}
    return {w for w in words if 0 < spread[w] <= max(1, len(chunks) // 2)}


def select_chunks(file: ExtractedFile, question: str, *, last_sent: int | None = None,
                  continuing: bool = False) -> tuple[list[ExtractedChunk], str]:
    """The chunks a question needs, in document order, and why."""
    chunks = list(file.chunks.order_by("ordinal"))
    if not chunks:
        return [], "empty"
    if continuing:
        rest = [c for c in chunks if last_sent is None or c.ordinal > last_sent]
        return _within(rest, FULL_MAX_CHARS), "continue"

    plain = _ascii(question)
    pages = {int(n) for n in _PAGE_REF.findall(plain)}
    if pages:
        hit = [c for c in chunks if any(c.page_start <= p <= c.page_end for p in pages)]
        if hit:
            return _within(hit, FULL_MAX_CHARS), "page"

    numbers = set(_CLAUSE_REF.findall(plain)) | set(_BARE_NUMBER.findall(plain))
    if numbers:
        hit = [c for c in chunks if c.clause_number and any(
            c.clause_number == n or c.clause_number.startswith(n + ".") for n in numbers)]
        if hit:
            return _within(hit, FULL_MAX_CHARS), "clause"

    if _TRANSLATE.search(question):
        return _within(chunks, FULL_MAX_CHARS), "in_order"

    # Small enough to read in full: every question gets all of it.
    if sum(len(c.text) for c in chunks) <= WHOLE_FILE_MAX_CHARS:
        return chunks, "whole"

    if _WHOLE.search(question):
        return _within(chunks, FULL_MAX_CHARS), "in_order"

    telling = _telling_words(question, chunks)
    if telling:
        def score(c):
            text = _ascii(c.text).lower() + " " + _ascii(c.heading).lower()
            return sum(text.count(w) for w in telling)

        ranked = [c for c in sorted(chunks, key=lambda c: (-score(c), c.ordinal)) if score(c)]
        if ranked:
            chosen = _within(ranked, ASK_MAX_CHARS)
            return sorted(chosen, key=lambda c: c.ordinal), "relevant"
    return _within(chunks, ASK_MAX_CHARS), "start"


def _excerpt(chunk: ExtractedChunk) -> str:
    where = f"p.{chunk.page_start}" + (f"-{chunk.page_end}" if chunk.page_end != chunk.page_start else "")
    label = f"Clause {chunk.clause_number} {where}" if chunk.clause_number else f"Passage {where}"
    return f"[{label}] {chunk.text}"


UNVERIFIED_NOTE = (
    "[VERIFICATION NOTE] Text between ⟦UNVERIFIED⟧ and ⟦/UNVERIFIED⟧ was read by OCR "
    "but could not be confirmed by the verification step, so it may be wrong. "
    "{crops}Never state such a value as certain: say it is unverified and needs "
    "human review, and do not guess a corrected value."
)


def _page_image(path: Path, page_number: int, *, dpi: int = 110):
    from apps.ai.providers.base import ImageRegion

    return ImageRegion(image_bytes=extractors.rasterise_page(path, page_number, dpi=dpi),
                       mime_type="image/png", page_number=page_number)


def _file_image(file: ExtractedFile, path: Path):
    from apps.ai.providers.base import ImageRegion

    mime = "image/png" if path.suffix == ".png" else "image/jpeg"
    return ImageRegion(image_bytes=path.read_bytes(), mime_type=mime, page_number=1)


def _region_crop(file: ExtractedFile, path: Path, region: ExtractedRegion):
    from apps.ai.providers.base import ImageRegion

    if region.bbox is None:
        return None
    try:
        if file.kind == ExtractedFile.Kind.PDF:
            data = extractors.crop_region(path, region.page_number, region.bbox, dpi=220, padding=6.0)
        else:
            data = extractors.crop_image(path.read_bytes(), region.bbox, padding=6.0)
    except extractors.ExtractionError:
        return None
    return ImageRegion(image_bytes=data, mime_type="image/png", page_number=region.page_number,
                       bbox=region.bbox)


MAX_UNRESOLVED_CROPS = 2


#: What kind of value a question asks for, by its words.
_ASKS_FOR = [
    ("party", re.compile(r"\bpart(?:y|ies)\b|\bcompan(?:y|ies)\b|\bclient\b|\bconsultant\b|\bsigned by\b"
                         r"|الأطراف|الطرفين|الطرف|الشركة|الشركات|العميل|الاستشاري", re.I)),
    ("contract_number", re.compile(r"contract (?:no|number)|agreement (?:no|number)|reference"
                                   r"|\bref\b|رقم العقد|رقم الاتفاقية|المرجع", re.I)),
    ("money", re.compile(r"amount|price|fee|cost|sum|value|pay|how much|مبلغ|المبلغ|السعر|قيمة|أتعاب"
                         r"|تكلفة|كم", re.I)),
    ("percentage", re.compile(r"percent|%|rate|نسبة|٪", re.I)),
    ("date", re.compile(r"\bdate\b|\bwhen\b|deadline|تاريخ|متى|موعد", re.I)),
    ("duration", re.compile(r"\bdays?\b|\bweeks?\b|\bmonths?\b|period|duration|how long|within"
                            r"|مدة|فترة|أيام|يوم|شهر", re.I)),
    ("guarantee", re.compile(r"guarantee|bond|security|retention|ضمان|كفالة|تأمين", re.I)),
]
#: Kinds a question may ask for without naming them: it names the clause, and
#: the clause's own values are what it is about. Party names and reference
#: numbers are left out -- they sit in the headers and footers that fall
#: inside whichever clause spans the page break.
_BODY_KINDS = {"money", "percentage", "date", "duration", "guarantee"}


def _relevant_unresolved(region_ids, question, chunks):
    """Unresolved regions holding a critical value that the question is
    about: it names their page, asks for the kind of value they hold, names
    their clause (for the clause's own amounts, rates and periods), or shares
    a telling word with either of their readings."""
    from .critical_values import find_critical_values

    plain = _ascii(question)
    pages = {int(n) for n in _PAGE_REF.findall(plain)}
    numbers = set(_CLAUSE_REF.findall(plain)) | set(_BARE_NUMBER.findall(plain))
    asked = {kind for kind, pattern in _ASKS_FOR if pattern.search(question)}
    clause_of = {i: c.clause_number for c in chunks for i in c.unresolved_region_ids}
    telling = _telling_words(question, chunks) or {
        w for w in re.findall(r"[^\W_]+", plain.lower()) if len(w) >= 4 and w not in _STOPWORDS}
    out = []
    for region in (ExtractedRegion.objects.filter(pk__in=region_ids)
                   .order_by("page_number", "block_index")):
        readings = f"{region.source_text}\n{region.vision_pass1_text}"
        kinds = {m.kind.value for m in find_critical_values(readings)} - {"clause_reference"}
        if not kinds:
            continue
        text = _ascii(readings).lower()
        clause = clause_of.get(region.pk, "")
        names_clause = bool(clause) and any(clause == n or clause.startswith(n + ".") for n in numbers)
        if (region.page_number in pages
                or kinds & asked
                or (names_clause and kinds & _BODY_KINDS)
                or any(w in text for w in telling)):
            out.append(region)
    return out


def unverified_values(file: ExtractedFile, answer: str) -> list[str]:
    """Values an answer states that no verified text of the file supports.

    The prompt tells the model not to present an unverified value as certain;
    this is the check that does not depend on the model listening. Clause
    numbers are left out: an answer echoes the clause it was asked about.
    """
    from .critical_values import CriticalKind, critical_signature, find_critical_values

    if not answer or not file.regions.filter(status__in=_UNRESOLVED).exists():
        return []
    verified = set()
    for region in file.regions.exclude(status__in=_UNRESOLVED):
        verified |= critical_signature(region.usable_text)
    out = []
    for match in find_critical_values(answer):
        if match.kind is CriticalKind.CLAUSE_REFERENCE:
            continue
        if not critical_signature(match.raw) <= verified and match.raw not in out:
            out.append(match.raw)
    return out


def context_for(file: ExtractedFile, question: str, *, last_sent: int | None = None,
                continuing: bool = False) -> Context:
    """Excerpts (and only the pictures a question needs) for one question.

    Normal follow-ups get stored text only: no OCR, no vision, no image.
    """
    path = absolute(file)
    ctx = Context()

    if file.status == ExtractedFile.Status.FAILED or not file.chunks.exists():
        # Nothing could be read out of it: the picture itself is the only
        # source. Kept small -- the first pages, never the whole file.
        ctx.fallback, ctx.mode = True, "fallback"
        if path is not None and path.is_file():
            if file.kind == ExtractedFile.Kind.PDF:
                for n in range(1, min(file.page_count or 1, MAX_IMAGES) + 1):
                    try:
                        ctx.images.append(_page_image(path, n))
                        ctx.pages.append(n)
                    except extractors.ExtractionError:
                        break
            else:
                ctx.images.append(_file_image(file, path))
                ctx.pages = [1]
        return ctx

    chunks, ctx.mode = select_chunks(file, question, last_sent=last_sent, continuing=continuing)
    ctx.excerpts = [_excerpt(c) for c in chunks]
    ctx.chunks_sent = [c.ordinal for c in chunks]
    ctx.pages = sorted({p for c in chunks for p in range(c.page_start, c.page_end + 1)})
    if continuing and not chunks:
        ctx.excerpts = ["[END OF DOCUMENT] Every part of the attached file has already "
                        "been provided in earlier turns of this conversation."]
        return ctx
    if ctx.mode == "whole":
        # Said outright, or the model takes clause-labelled passages for a
        # selection and declines "summarise it page by page" as impossible.
        # Last, so the excerpt numbers the citations use do not move.
        ctx.excerpts.append(
            f"[COMPLETE DOCUMENT] The excerpts above are the ENTIRE attached file -- all "
            f"{file.page_count or len(ctx.pages)} page(s), in order, each labelled with its page "
            f"(p.N). Nothing was left out: you can summarise it as a whole or page by page, "
            f"and answer about any part of it.")

    # A visual question: the picture of the pages it is about.
    if VISUAL.search(question) and path is not None and path.is_file():
        if file.kind == ExtractedFile.Kind.IMAGE:
            ctx.images.append(_file_image(file, path))
        else:
            wanted = ctx.pages if ctx.mode in ("page", "clause", "relevant") else []
            if not wanted:
                # "Is it signed?" -- signatures and stamps sit at the end.
                wanted = [file.page_count or 1]
            for n in wanted[:2]:
                try:
                    ctx.images.append(_page_image(path, n))
                except extractors.ExtractionError:
                    pass

    # The answer may rest on a value nobody could confirm: say so, and let the
    # model look at exactly the regions the question is about -- critical
    # ones only, at most MAX_UNRESOLVED_CROPS. A bad scan has unresolved text
    # everywhere; sending pictures of all of it on every question is the load
    # this module exists to avoid.
    unresolved_ids = [i for c in chunks for i in c.unresolved_region_ids]
    if unresolved_ids:
        crops = 0
        if path is not None and path.is_file():
            for region in _relevant_unresolved(unresolved_ids, question, chunks):
                if len(ctx.images) >= MAX_IMAGES or crops >= MAX_UNRESOLVED_CROPS:
                    break
                crop = _region_crop(file, path, region)
                if crop is not None:
                    ctx.images.append(crop)
                    crops += 1
        ctx.excerpts.append(UNVERIFIED_NOTE.format(
            crops=("Cropped images of those regions are attached: if a value is clearly "
                   "legible in its image you may quote it, marked as read from the image. ")
            if crops else ""))
    ctx.images = ctx.images[:MAX_IMAGES]
    return ctx


# ── removal ──────────────────────────────────────────────────────────────

def delete_if_unused(file_id) -> bool:
    """Delete a stored extraction and its file once no turn refers to it."""
    from apps.ai.models import AdvisorMessage

    if not file_id or AdvisorMessage.objects.filter(extracted_file_id=file_id).exists():
        return False
    file = ExtractedFile.objects.filter(pk=file_id).first()
    if file is None:
        return False
    target = absolute(file)
    file.delete()
    try:
        if target is not None and target.is_file():
            target.unlink()
    except OSError:
        pass
    return True
