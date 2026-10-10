"""Analysis orchestration — the pipeline of §56.

    canonical document
        -> clause / structure detection
        -> clause classification
        -> AJi standards resolution        (deterministic)
        -> structured value extraction     (deterministic)
        -> deterministic rules engine      (authoritative for facts)
        -> preliminary findings
        -> AI semantic comparison
        -> AI verification pass
        -> evidence validation
        -> system confidence
        -> final analysis

The ordering is the point. Rules run *before* the model and their verdict is
recorded separately, so a disagreement surfaces as a conflict for a human
rather than being resolved silently in the model's favour (§18, §55).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ai.prompts import NO_CATEGORY, PROMPT_VERSION
from apps.ai.providers.base import AIProviderError
from apps.ai.services import ai_service
from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.contracts.models import ContractStatus, RiskLevel
from apps.documents.models import Block, VerificationStatus
from apps.standards.models import ClauseCategory
from apps.standards.resolution import ResolutionResult, resolve_for_contract

from . import rules, scoring
from .clause_detection import DetectedClause, build_context, detect_clauses
from .models import (
    AnalysisRun,
    AnalysisStatus,
    Clause,
    ComplianceResult,
    Finding,
    MissingClause,
    VerificationOutcome,
)

logger = logging.getLogger(__name__)

ANALYSIS_CODE_VERSION = "1.0.0"

#: Findings below this system confidence are surfaced as needing a human read
#: rather than stated as fact (§24, §54).
LOW_CONFIDENCE_THRESHOLD = 0.6
#: How sure the model must be that a clause no rule can check is compliant
#: before the clause is left out of the findings.
AI_ONLY_COMPLIANT_CONFIDENCE = 0.7


@dataclass
class AnalysisOutcome:
    run: AnalysisRun
    clauses_detected: int = 0
    clauses_assessed: int = 0
    findings_created: int = 0
    missing_clauses: int = 0
    conflicts: int = 0
    ai_failures: int = 0
    #: Clauses that exist in the contract but could not be categorised. These
    #: make the missing-clause set difference unsafe: an unclassified clause
    #: looks exactly like an absent one.
    unclassified_clauses: int = 0
    #: Clauses the classifier said set no judgeable term (severability,
    #: signatures...). Classified, so they do not weaken absence; not assessed.
    no_term_clauses: int = 0
    notes: list[str] = field(default_factory=list)


# ── entry point ──────────────────────────────────────────────────────────

def start_analysis(contract, *, document=None, actor=None, idempotency_key: str = "") -> AnalysisRun:
    """Create the run record. The heavy work happens in the Celery task.

    Re-analysis never overwrites: each run gets the next version number (§32).
    """
    document = document or contract.documents.order_by("-created_at").first()
    previous = contract.analysis_runs.order_by("-version").first()

    run = AnalysisRun.objects.create(
        contract=contract,
        document=document,
        version=(previous.version + 1) if previous else 1,
        status=AnalysisStatus.QUEUED,
        idempotency_key=idempotency_key,
        retry_of=previous if previous and previous.status == AnalysisStatus.FAILED else None,
        prompt_template_version=PROMPT_VERSION,
        analysis_code_version=ANALYSIS_CODE_VERSION,
    )

    contract.status = ContractStatus.AI_ANALYSIS
    contract.save(update_fields=["status", "updated_at"])

    audit.record(
        AuditAction.ANALYSIS_STARTED,
        actor=actor,
        entity_type="AnalysisRun",
        entity_id=run.pk,
        contract_id=contract.pk,
        details={"version": run.version},
    )
    return run


# ── the pipeline ─────────────────────────────────────────────────────────

def run_analysis(run: AnalysisRun) -> AnalysisOutcome:
    """Execute one analysis run to completion."""
    contract = run.contract
    outcome = AnalysisOutcome(run=run)

    run.status = AnalysisStatus.RUNNING
    run.started_at = timezone.now()
    run.ai_provider = _provider_name()
    run.save(update_fields=["status", "started_at", "ai_provider", "updated_at"])

    try:
        if run.document is None:
            raise ValueError("Analysis requires an extracted document.")

        # 1 ── structure ---------------------------------------------------
        detected = detect_clauses(run.document)
        outcome.clauses_detected = len(detected)
        if not detected:
            outcome.notes.append("no_clauses_detected")

        stored = _persist_clauses(run, detected)
        run.progress_total, run.progress_done = len(stored), 0
        run.save(update_fields=["progress_total", "progress_done", "updated_at"])

        # 2 ── standards (deterministic, before any model call) ------------
        resolution = resolve_for_contract(contract)
        run.standards_snapshot = resolution.snapshot
        run.save(update_fields=["standards_snapshot", "updated_at"])

        # Not `detected` - that name already holds the detected clauses.
        detected_language = getattr(run.document, "detected_language", "")
        if detected_language and detected_language != contract.language:
            # Not an error - the uploader may simply have left the default.
            # Recorded rather than silently overridden, so a reviewer can see
            # which language the analysis was actually conducted in.
            outcome.notes.append(
                f"language_declared:{contract.language}"
                f"_detected:{detected_language}"
            )

        if resolution.uses_demo_fixtures:
            outcome.notes.append("uses_demo_fixture_standards")
        if resolution.unresolved_categories:
            outcome.notes.append(
                f"no_standard_for:{','.join(resolution.unresolved_categories)}"
            )

        audit.record(
            AuditAction.STANDARD_SELECTED,
            entity_type="AnalysisRun",
            entity_id=run.pk,
            contract_id=contract.pk,
            details={
                "resolved": len(resolution.resolved),
                "unresolved": resolution.unresolved_categories,
                "demo_fixtures": resolution.uses_demo_fixtures,
            },
        )

        # 3 ── classify, compare, verify ------------------------------------
        categories = _categories_by_code()
        batched = _classify_in_batches(run, stored, categories)
        for clause_model, detected_clause in _most_serious_first(stored, batched, resolution):
            assessed = _assess_clause(
                run, clause_model, detected_clause, detected, resolution, categories, outcome,
                classified=batched.get(clause_model.pk),
            )
            if assessed:
                outcome.clauses_assessed += 1
            # One clause further: the contract page's progress bar reads this.
            run.progress_done += 1
            run.save(update_fields=["progress_done", "updated_at"])

        # 4 ── missing clauses: set difference, never a model question ------
        outcome.missing_clauses = _record_missing_clauses(
            run, resolution, absence_certain=outcome.unclassified_clauses == 0
        )
        if outcome.no_term_clauses:
            outcome.notes.append(f"not_assessed_no_term:{outcome.no_term_clauses}")
        if outcome.unclassified_clauses:
            outcome.notes.append(
                f"absence_unverified:{outcome.unclassified_clauses}_unclassified_clauses"
            )

        # 5 ── roll up ------------------------------------------------------
        _finalise(run, outcome)

    except Exception as exc:                       # noqa: BLE001 - recorded, then re-raised
        logger.exception("Analysis run %s failed", run.pk)
        run.status = AnalysisStatus.FAILED
        run.error_message = str(exc)[:2000]
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error_message", "finished_at", "updated_at"])

        contract.status = ContractStatus.ANALYSIS_FAILED
        contract.save(update_fields=["status", "updated_at"])

        from apps.notifications import services as notify

        notify.analysis_failed(run, str(exc)[:500])

        audit.record(
            AuditAction.ANALYSIS_FAILED,
            entity_type="AnalysisRun",
            entity_id=run.pk,
            contract_id=contract.pk,
            details={"error": str(exc)[:500]},
        )
        raise

    return outcome


# ── steps ────────────────────────────────────────────────────────────────

def _provider_name() -> str:
    from apps.ai.registry import get_ai_provider

    try:
        return get_ai_provider().name
    except Exception:                               # pragma: no cover
        return ""


def _categories_by_code() -> dict[str, ClauseCategory]:
    return {c.code: c for c in ClauseCategory.objects.filter(is_active=True)}


@transaction.atomic
def _persist_clauses(run: AnalysisRun, detected: list[DetectedClause]):
    """Store detected clauses with their links back to source blocks."""
    pairs = []
    for item in detected:
        clause = Clause.objects.create(
            document=run.document,
            contract=run.contract,
            clause_number=item.clause_number,
            heading=item.heading[:512],
            text=item.text,
            page_number=item.page_number,
            ordinal=item.ordinal,
            referenced_clause_numbers=item.referenced_clauses,
            is_inferred=not item.is_traceable,
        )
        if item.block_ids:
            clause.source_blocks.set(Block.objects.filter(pk__in=item.block_ids))
        pairs.append((clause, item))
    return pairs


def _most_serious_first(stored, batched, resolution):
    """Assess the clauses whose standard carries the most impact first, so
    the findings that matter most are the first the reader sees while the
    rest are still being assessed. Reading order within the same impact; a
    clause not yet classified goes last and is classified when reached."""
    def weight(pair):
        parsed = batched.get(pair[0].pk)
        resolved = resolution.resolved.get(parsed.primary) if parsed else None
        return -(resolved.version.impact or 0) if resolved else 0
    return sorted(stored, key=weight)


#: How sure the classifier must be that a clause sets no judgeable term
#: before the clause goes unassessed. Below it, the best real category is used.
NO_CATEGORY_MIN_CONFIDENCE = 0.8


def _primary_of(parsed, categories) -> str:
    """The category to act on. "none" is trusted only when the classifier is
    sure; otherwise the clause is assessed under its best real alternative,
    because skipping a clause that did carry a term is the costlier mistake."""
    if parsed.primary != NO_CATEGORY or parsed.confidence >= NO_CATEGORY_MIN_CONFIDENCE:
        return parsed.primary
    return next((code for code in parsed.alternatives if code in categories), NO_CATEGORY)


def _classify_in_batches(run, stored, categories) -> dict:
    """{clause pk: classification} for the clauses a batched call settled.

    Consecutive clauses are grouped up to AI_CLASSIFY_BATCH_SIZE and
    AI_CLASSIFY_BATCH_MAX_CHARS. A clause too long to share a call, or one the
    batch did not answer cleanly, is simply absent here and is classified on
    its own in _assess_clause, exactly as before batching existed.
    """
    size = getattr(settings, "AI_CLASSIFY_BATCH_SIZE", 1)
    max_chars = getattr(settings, "AI_CLASSIFY_BATCH_MAX_CHARS", 3500)
    if size <= 1 or not categories:
        return {}

    batches: list[list] = []
    current: list = []
    used = 0
    for clause, _detected in stored:
        length = len(clause.text)
        if length > max_chars:
            continue
        if current and (len(current) >= size or used + length > max_chars):
            batches.append(current)
            current, used = [], 0
        current.append(clause)
        used += length
    if current:
        batches.append(current)

    found: dict = {}
    keys = sorted(categories)
    for batch in batches:
        if len(batch) == 1:
            continue                                   # one clause: the normal call
        numbered = [(index, clause.clause_number, clause.text) for index, clause in enumerate(batch, 1)]
        answers = ai_service.classify_clauses(
            clauses=numbered, categories=keys, contract_id=run.contract.pk, analysis_run=run,
        )
        for index, clause in enumerate(batch, 1):
            if index in answers:
                found[clause.pk] = answers[index]
    return found


def _assess_clause(
    run, clause, detected_clause, all_detected, resolution, categories, outcome,
    classified=None,
) -> bool:
    """Classify, apply the rule, then ask the model — in that order."""
    contract = run.contract
    # The language the model writes its prose in. This is the reader's
    # language, not the document's: an English contract read by an Arabic
    # desk still needs Arabic findings. Driving this from the document is
    # what produced English titles on an Arabic screen.
    #
    # It applies to interpretation only -- titles, explanations, impact,
    # recommended actions. Quoted clause text is copied verbatim from the
    # source and is never translated, because a translated clause is a
    # different clause.
    language = settings.AI_OUTPUT_LANGUAGE

    # -- classification -------------------------------------------------
    category = None
    classification_confidence = None
    if categories:
        try:
            if classified is not None:
                parsed = classified                    # settled in a batch
            else:
                parsed = ai_service.classify_clause(
                    clause_text=clause.text[:8000],
                    categories=sorted(categories),
                    language=language,
                    contract_id=contract.pk,
                    analysis_run=run,
                ).parsed
            primary = _primary_of(parsed, categories)
            category = categories.get(primary)
            if primary == NO_CATEGORY:
                outcome.no_term_clauses += 1
            classification_confidence = parsed.confidence
            clause.primary_category = category
            clause.alternative_categories = parsed.alternatives
            clause.classification_confidence = parsed.confidence
            clause.save(update_fields=[
                "primary_category", "alternative_categories",
                "classification_confidence", "updated_at",
            ])
        except AIProviderError as exc:
            outcome.ai_failures += 1
            outcome.unclassified_clauses += 1
            logger.warning("Classification failed for clause %s: %s", clause.pk, exc)
            return False

    if category is None:
        return False

    resolved = resolution.resolved.get(category.code)
    if resolved is None:
        # No approved standard covers this category — a real outcome (§54).
        return False

    version = resolved.version

    # -- deterministic rule FIRST ---------------------------------------
    rule_outcome = rules.evaluate(version.rule_definition, clause.text, contract=contract)

    # -- then the model, given the standard the system already chose -----
    context = build_context(detected_clause, all_detected)
    ai_result = ComplianceResult.UNKNOWN
    assessment = None
    try:
        call = ai_service.assess_clause(
            clause_text=clause.text[:12000],
            standard_position=version.preferred_position,
            acceptable_range=version.acceptable_range,
            red_flags=version.red_flags,
            context_clauses=context,
            contract_context={
                "country": contract.get_country_display(),
                "contract_type": contract.contract_type.name,
                "perspective": contract.analysis_perspective,
                "counterparty_type": contract.counterparty_type,
                "currency": contract.currency,
            },
            language=language,
            contract_id=contract.pk,
            analysis_run=run,
        )
        assessment = call.parsed
        ai_result = assessment.ai_result.value
    except AIProviderError as exc:
        outcome.ai_failures += 1
        logger.warning("Assessment failed for clause %s: %s", clause.pk, exc)

    # Nothing to report: both readings say the clause is fine -- or no rule can
    # check it and the model, confidently, finds it fine. Kept as a finding,
    # that second case reached the page as a medium risk titled "no deviation"
    # (an AI-only reading is routed to review, and review lifts low to
    # medium). An AI-only breach, partial or unsure reading still goes to a
    # human.
    ai_confidence = assessment.confidence if assessment is not None else None
    if ai_result == ComplianceResult.COMPLIANT and (
        rule_outcome.result == ComplianceResult.COMPLIANT
        or (rule_outcome.result == ComplianceResult.NOT_APPLICABLE
            and ai_confidence is not None and ai_confidence >= AI_ONLY_COMPLIANT_CONFIDENCE)
    ):
        return True

    verification = rules.reconcile(rule_outcome.result, ai_result)

    # -- independent verification of anything contested ------------------
    verifier_agrees = None
    verifier_notes = ""
    if assessment is not None and verification in {
        VerificationOutcome.SYSTEM_AI_CONFLICT, VerificationOutcome.VERIFIED
    }:
        try:
            verify_call = ai_service.verify_finding(
                clause_text=clause.text[:12000],
                standard_position=version.preferred_position,
                system_result=rule_outcome.result,
                previous_assessment=assessment.model_dump(mode="json"),
                language=language,
                contract_id=contract.pk,
                analysis_run=run,
            )
            verdict = verify_call.parsed
            verifier_agrees = verdict.verdict.value == "agree"
            verifier_notes = verdict.evidence
            if verdict.verdict.value in {"needs_human_review", "ambiguous"}:
                verification = VerificationOutcome.NEEDS_HUMAN_REVIEW
            elif verdict.verdict.value == "disagree":
                verification = VerificationOutcome.SYSTEM_AI_CONFLICT
        except AIProviderError as exc:
            outcome.ai_failures += 1
            logger.warning("Verification failed for clause %s: %s", clause.pk, exc)

    # -- evidence: original wording comes from stored blocks, not the model
    original_wording, source_blocks, source_confidence = _evidence_for(clause)
    # A finding resting on a critical value nobody could confirm is a source
    # conflict, whether it was recorded the old way (SOURCE_CONFLICT) or by the
    # three-tier flow (NEEDS_HUMAN_REVIEW on a critical block).
    if any(
        b.verification_status == VerificationStatus.SOURCE_CONFLICT or b.unresolved_critical
        for b in source_blocks
    ):
        verification = VerificationOutcome.SOURCE_CONFLICT

    # -- scoring ---------------------------------------------------------
    risk = scoring.score_finding(
        standard_impact=version.impact,
        system_result=rule_outcome.result,
        ai_result=ai_result,
        verification=verification,
        ai_confidence=assessment.confidence if assessment else None,
    )
    confidence, breakdown = scoring.system_confidence(
        source_confidence=source_confidence,
        standard_confidence=classification_confidence,
        rule_confidence=rule_outcome.confidence if rule_outcome.is_conclusive else 0.0,
        ai_confidence=assessment.confidence if assessment else None,
        analyzer_verifier_agree=verifier_agrees,
    )
    if confidence < LOW_CONFIDENCE_THRESHOLD and verification == VerificationOutcome.VERIFIED:
        verification = VerificationOutcome.NEEDS_HUMAN_REVIEW

    finding = Finding.objects.create(
        analysis=run,
        contract=contract,
        clause=clause,
        risk_level=risk.level,
        impact=risk.impact,
        probability=risk.probability,
        risk_score=risk.score,
        category=category,
        title=(assessment.deviation[:255] if assessment and assessment.deviation
               else f"{category.name}: deviation from "
                    f"{'the FIDIC baseline' if version.is_baseline else 'AJi standard'}"),
        title_is_generated=not (assessment and assessment.deviation),
        clause_number=clause.clause_number,
        page_number=clause.page_number,
        original_wording=original_wording,
        standard_version=version,
        standard_position=version.preferred_position,
        deviation=assessment.deviation if assessment else "",
        explanation=assessment.explanation if assessment else "",
        business_impact=assessment.business_impact if assessment else "",
        recommended_action=assessment.recommended_action if assessment else "",
        fallback_wording=(
            version.approved_fallback_wording
            or _in_clause_language(assessment.suggested_wording if assessment else "", clause.text)
        ),
        responsible_department=(
            version.responsible_department or category.default_department
        ),
        # The same finding for every reader. `title` is stored per language
        # too, because it is derived from `deviation`. The model writes one
        # language; the others are filled by translate_findings afterwards.
        prose=_prose_of(assessment),
        # A reading aid beside the clause, never a replacement for it. The
        # verbatim `original_wording` above is what the redline exports.
        wording_translations=(
            dict(assessment.clause_translation)
            if assessment and assessment.clause_translation else {}
        ),
        system_result=rule_outcome.result,
        system_rule_detail={**rule_outcome.detail, "scoring": risk.rationale},
        ai_result=ai_result,
        ai_reasoning=assessment.explanation if assessment else "",
        verification_status=verification,
        verifier_notes=verifier_notes,
        source_confidence=source_confidence,
        standard_confidence=classification_confidence,
        rule_confidence=rule_outcome.confidence if rule_outcome.is_conclusive else 0.0,
        ai_confidence=assessment.confidence if assessment else None,
        system_confidence=confidence,
    )
    if source_blocks:
        finding.source_blocks.set(source_blocks)

    outcome.findings_created += 1
    if verification == VerificationOutcome.SYSTEM_AI_CONFLICT:
        outcome.conflicts += 1
    return True


def _evidence_for(clause) -> tuple[str, list[Block], float | None]:
    """Original wording, taken from stored source blocks (§22, §53)."""
    blocks = list(clause.source_blocks.all())
    if not blocks:
        return "", [], None

    # The verified text where verification chose one; the original otherwise.
    wording = "\n".join(b.usable_text for b in blocks).strip()
    confidences = [b.ocr_confidence for b in blocks if b.ocr_confidence is not None]
    # Native text carries no confidence value; treat it as fully reliable.
    confidence = (sum(confidences) / len(confidences)) if confidences else 100.0
    return wording[:20000], blocks, confidence


def _record_missing_clauses(
    run: AnalysisRun, resolution: ResolutionResult, *, absence_certain: bool = True
) -> int:
    """Expected categories with no clause in the contract.

    A set difference over *classified* clauses — deterministic and free, and
    far better than asking a model "what is missing?", which is the question
    they fail hardest (§23).

    It is only a statement about the contract when every clause was classified.
    If classification failed anywhere, an unread clause is indistinguishable
    from an absent one, so each record is marked `absence_confirmed=False` and
    says so in its reason. Reporting "no insurance clause" for a contract whose
    insurance clause we simply failed to read would be a damaging false alarm.
    """
    clauses = list(run.contract.clauses.filter(document=run.document)
                   .values_list("clause_number", "primary_category__code", "alternative_categories"))
    present = {primary for _, primary, _ in clauses if primary}
    # A topic that is only a secondary topic of some clause is not a clause
    # of its own, but it is not plainly absent either: the Ekaider staff
    # clause carries the confidentiality duty, "General provisions" carries
    # the civil-code liability. Saying "missing" there is a false alarm.
    mentioned: dict[str, list[str]] = {}
    for number, _, alternatives in clauses:
        for code in alternatives or []:
            if isinstance(code, str):
                mentioned.setdefault(code, [])
                if (number or "") not in mentioned[code]:
                    mentioned[code].append(number or "")
    expected = set(resolution.resolved)
    created = 0

    for code in sorted(expected - present):
        resolved = resolution.resolved[code]
        version = resolved.version
        passing = [n for n in mentioned.get(code, []) if n][:6] or (["—"] if code in mentioned else [])
        certain = absence_certain and not passing
        risk = scoring.score_finding(
            standard_impact=version.impact,
            system_result=(
                ComplianceResult.NON_COMPLIANT if certain
                else ComplianceResult.UNKNOWN
            ),
            ai_result=ComplianceResult.UNKNOWN,
            verification=VerificationOutcome.AMBIGUOUS,
        )
        caveat = "" if absence_certain else (
            " NOTE: some clauses in this contract could not be classified, so "
            "this clause may be present but unread. Confirm against the source "
            "document before acting on this."
        )
        if passing:
            caveat = (f" No clause of its own, but clause(s) {', '.join(passing)} also touch on "
                      "this topic. Check whether they cover what the standard expects.") + caveat
        MissingClause.objects.create(
            analysis=run,
            contract=run.contract,
            standard_version=version,
            category=resolved.standard.category,
            title=resolved.standard.title,
            reason_expected=(
                f"{'FIDIC baseline' if version.is_baseline else 'AJi standard'} "
                f"{resolved.standard.category.code} "
                f"({resolved.scope}) v{version.version} expects this clause."
                + caveat
            ),
            absence_confirmed=certain,
            mentioned_in=passing,
            # Absence is not automatically high risk — the standard's own
            # impact rating decides (§23).
            risk_if_absent=risk.level,
            recommended_action=version.approved_fallback_wording or version.preferred_position,
        )
        created += 1
    return created


def _finalise(run: AnalysisRun, outcome: AnalysisOutcome) -> None:
    """Roll findings up to contract level and hand over to a human."""
    from .tidy import tidy_findings

    # One point once, and no risk without evidence (tidy.py).
    tidied = tidy_findings(run)
    outcome.notes.append(f"tidied: {tidied['merged']} merged, {tidied['undetermined']} undetermined")
    findings = list(run.findings.all())
    run.high_count = sum(1 for f in findings if f.risk_level == "high")
    run.medium_count = sum(1 for f in findings if f.risk_level == "medium")
    run.low_count = sum(1 for f in findings if f.risk_level == "low")
    run.unresolved_count = sum(1 for f in findings if f.needs_human)

    if run.high_count:
        overall = RiskLevel.HIGH
    elif run.medium_count:
        overall = RiskLevel.MEDIUM
    elif run.low_count:
        overall = RiskLevel.LOW
    else:
        # No findings is not the same as "low risk" when nothing could be
        # assessed — say unknown rather than imply a clean bill of health.
        overall = RiskLevel.LOW if outcome.clauses_assessed else RiskLevel.UNKNOWN
    if outcome.unclassified_clauses or outcome.ai_failures:
        # Not everything was looked at, so the roll-up is a floor, not a
        # verdict: an unread clause could be worse than anything found.
        #
        # HIGH survives - it is a real red flag found in the text, and hiding
        # it behind "unknown" would be its own kind of dishonesty. Anything
        # below HIGH becomes UNKNOWN, because "medium" on a run that read one
        # clause in eleven reads as a considered judgement, and it is not.
        if overall != RiskLevel.HIGH:
            overall = RiskLevel.UNKNOWN

    # A run that could not complete its work must not present itself as a
    # finished analysis. "Succeeded" on this screen tells a reviewer the
    # contract has been examined; if half the clauses were never read, that is
    # the single most misleading thing the system could say (§54).
    gaps: list[str] = []
    if outcome.unclassified_clauses:
        gaps.append(
            f"{outcome.unclassified_clauses} clause(s) could not be categorised"
        )
    other_failures = outcome.ai_failures - outcome.unclassified_clauses
    if other_failures > 0:
        gaps.append(f"{other_failures} AI call(s) failed")

    confidences = [f.system_confidence for f in findings if f.system_confidence is not None]
    run.system_confidence = (sum(confidences) / len(confidences)) if confidences else None
    run.confidence_breakdown = {
        "findings": len(findings),
        "ai_failures": outcome.ai_failures,
        "unclassified_clauses": outcome.unclassified_clauses,
        "clauses_detected": outcome.clauses_detected,
        "clauses_assessed": outcome.clauses_assessed,
        "gaps": gaps,
        "notes": outcome.notes,
    }
    run.overall_risk = overall
    if gaps:
        run.next_action = (
            "This analysis is incomplete: " + "; ".join(gaps) + ". "
            "Re-run it once the AI service is available. Do not treat the "
            "findings below as a full review of the contract."
        )
    elif run.unresolved_count:
        run.next_action = (
            "Review the source conflicts and system/AI disagreements before proceeding."
        )
    else:
        run.next_action = "Proceed to contract review."
    run.status = AnalysisStatus.DEGRADED if gaps else AnalysisStatus.SUCCEEDED
    run.finished_at = timezone.now()
    # The model that actually answered, from this run's request log: the
    # field was never filled, so every report said "model: —".
    from django.db.models import Count

    from apps.ai.models import AIRequestLog

    used = (AIRequestLog.objects.filter(analysis_run=run).exclude(model="")
            .values("model").annotate(n=Count("id")).order_by("-n").first())
    if used and not run.ai_text_model:
        run.ai_text_model = used["model"][:128]
    run.save()

    contract = run.contract
    contract.current_analysis = run
    contract.risk_level = overall
    contract.status = (
        ContractStatus.NEEDS_HUMAN_REVIEW if (run.unresolved_count or gaps)
        else ContractStatus.PENDING_CONTRACT_REVIEW
    )
    contract.save(update_fields=[
        "current_analysis", "risk_level", "status", "updated_at",
    ])

    from apps.notifications import services as notify

    notify.analysis_completed(run)
    if run.unresolved_count:
        notify.review_requested(
            contract,
            role="contract_reviewer",
            kind="analysis.completed",
            title=f"Needs review — {contract.project_name}",
        )

    audit.record(
        AuditAction.ANALYSIS_COMPLETED,
        entity_type="AnalysisRun",
        entity_id=run.pk,
        contract_id=contract.pk,
        new_state=contract.status,
        details={
            "version": run.version,
            "high": run.high_count,
            "medium": run.medium_count,
            "low": run.low_count,
            "unresolved": run.unresolved_count,
            "conflicts": outcome.conflicts,
            "missing_clauses": outcome.missing_clauses,
            "ai_failures": outcome.ai_failures,
        },
    )


def _in_clause_language(wording: str, clause_text: str) -> str:
    """Suggested replacement wording, only if it is in the clause's language.

    It may be inserted into the contract, so an English clause needs English
    wording; the model sometimes wrote it in the finding's language instead.
    Dropped rather than translated -- a translated clause is a different
    clause, and nobody at AJi drafted it.
    """
    from apps.documents.language import detect_language

    wording = wording or ""
    clause, suggested = detect_language(clause_text or ""), detect_language(wording)
    if clause in ("ar", "en") and suggested in ("ar", "en") and clause != suggested:
        return ""
    return wording


def _prose_of(assessment) -> dict:
    """The finding's prose keyed by language. The assessment is written in
    AI_OUTPUT_LANGUAGE; a provider that still returns `prose` for several
    languages keeps them all."""
    if not assessment:
        return {}
    blocks = dict(assessment.prose) if assessment.prose else {}
    prose = {
        code: {
            "title": (block.title or block.deviation or "")[:255],
            "deviation": block.deviation,
            "explanation": block.explanation,
            "business_impact": block.business_impact,
            "recommended_action": block.recommended_action,
        }
        for code, block in blocks.items()
    }
    if assessment.deviation and settings.AI_OUTPUT_LANGUAGE not in prose:
        prose[settings.AI_OUTPUT_LANGUAGE] = {
            "title": assessment.deviation[:255],
            "deviation": assessment.deviation,
            "explanation": assessment.explanation,
            "business_impact": assessment.business_impact,
            "recommended_action": assessment.recommended_action,
        }
    return prose
