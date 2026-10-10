"""How far a contract has got through processing, as a percentage.

Measured, not animated: the percentage comes from what the pipeline has
actually done -- pages read out of the file's pages while extracting, clauses
assessed out of the clauses found while analysing. Each stage owns a band of
the bar, so the bar only ever moves forward:

    waiting in the queue      3
    reading the file          5 - 40   (pages read / pages)
    finding the clauses      42 - 44
    analysing the clauses    45 - 95   (clauses assessed / clauses)
    done                    100

A job nobody has picked up is said to be waiting, and after a minute that
it has not started -- instead of a bar that sits at 3% looking busy.
"""
from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import ContractStatus

#: Seconds a contract may wait in the queue before the page says nothing has
#: picked it up (no worker running, or a queue that is backed up).
STALLED_AFTER = 60

STEPS = ("queued", "reading", "structuring", "analysing")


def _step_labels() -> dict[str, str]:
    return {
        "queued": _("في قائمة الانتظار"),
        "reading": _("قراءة الملف"),
        "structuring": _("تحديد البنود"),
        "analysing": _("تحليل البنود"),
    }


def progress_for(contract) -> dict:
    """The state the progress card shows, and polls for."""
    from apps.analysis.models import AnalysisRun, AnalysisStatus

    status = contract.status
    labels = _step_labels()
    state = {
        "percent": 0, "step": "queued", "label": labels["queued"], "detail": "",
        "done": False, "failed": False, "stalled": False, "next": "",
        "steps": [{"key": k, "label": labels[k]} for k in STEPS],
        # What the analysis has found so far, while it is still running.
        "found": None, "live": "",
    }

    if status == ContractStatus.NEEDS_SOURCE_REVIEW:
        # Read, and waiting for a person to confirm the key figures and the
        # pages to analyse: the card sends the reader there, not to an
        # analysis that has not run.
        state.update(percent=40, done=True, step="reading", label=_("بانتظار تأكيد البيانات الأساسية"),
                     detail=_("قُرئ الملف. أكّد القيم الأساسية والصفحات ثم يبدأ التحليل."),
                     next=reverse("documents:confirm", args=[contract.pk]))
        return state

    if status not in ContractStatus.in_pipeline():
        # Not the step labels' default: the card showed "in the queue" at
        # 100% in the moment before it moved on to the analysis.
        state.update(percent=100, done=True, step="analysing", label=_("اكتمل التحليل"))
        if status in (ContractStatus.EXTRACTION_FAILED, ContractStatus.ANALYSIS_FAILED):
            state.update(failed=True, label=_("تعذّرت المعالجة"),
                         detail=contract.get_status_display())
        run = contract.current_analysis
        state["next"] = (reverse("analysis:detail", args=[run.pk]) if run is not None
                         else reverse("contracts:detail", args=[contract.pk]) + "?view=record")
        return state

    if status in (ContractStatus.UPLOADED, ContractStatus.RETRYING):
        state.update(percent=3, detail=_("ينتظر بدء المعالجة"))
        waited = timezone.now() - (contract.updated_at or contract.created_at)
        if waited > timedelta(seconds=STALLED_AFTER):
            state.update(stalled=True, detail=_("لم تبدأ المعالجة بعد. خدمة المعالجة في الخلفية لا تعمل أو مشغولة؛ سيبدأ التحليل تلقائياً عند تشغيلها."))
        return state

    if status in (ContractStatus.EXTRACTING, ContractStatus.OCR_VERIFICATION):
        document = contract.documents.order_by("-created_at").first()
        total = (document.page_count if document else 0) or 0
        done = (document.pages.filter(blocks__isnull=False).distinct().count() if document else 0)
        done = min(done, total)
        percent = 5 + round(35 * done / total) if total else 6
        detail = (_("الصفحة %(done)s من %(total)s") % {"done": done, "total": total}) if total else ""
        state.update(percent=percent, step="reading", label=labels["reading"], detail=detail)
        return state

    if status == ContractStatus.STRUCTURING:
        state.update(percent=42, step="structuring", label=labels["structuring"])
        return state

    # AI_ANALYSIS
    run = (AnalysisRun.objects.filter(contract=contract)
           .filter(status__in=[AnalysisStatus.QUEUED, AnalysisStatus.RUNNING])
           .order_by("-version").first())
    if run is None or not run.progress_total:
        state.update(percent=44, step="structuring", label=labels["structuring"])
        return state
    done = min(run.progress_done, run.progress_total)
    state.update(
        percent=45 + round(50 * done / run.progress_total),
        step="analysing", label=labels["analysing"],
        detail=_("البند %(done)s من %(total)s") % {"done": done, "total": run.progress_total},
        found=found_so_far(run), live=reverse("analysis:detail", args=[run.pk]),
    )
    return state


#: Findings named on the card while the run goes on; the rest are a click away.
FOUND_SHOWN = 4


def found_so_far(run) -> dict:
    """Counts by risk, and the most serious findings yet, in the reader's
    language. Clauses are assessed most serious standard first (see
    apps.analysis.services._most_serious_first), so these arrive early."""
    from apps.analysis.models import Finding
    from .models import RiskLevel, risk_badge_label

    findings = Finding.objects.filter(analysis=run).select_related("category")
    counts = {level: 0 for level in (RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW)}
    for level in findings.values_list("risk_level", flat=True):
        if level in counts:
            counts[level] += 1
    top = (findings.filter(risk_level__in=[RiskLevel.HIGH, RiskLevel.MEDIUM])
           .order_by("-risk_score", "-created_at")[:FOUND_SHOWN])
    return {
        "total": findings.count(),
        "counts": [{"level": str(level), "label": str(level.label), "count": n}
                   for level, n in counts.items()],
        "top": [{"title": f.display_title, "clause": f.clause_number,
                 "level": f.risk_level, "label": risk_badge_label(f.risk_level)}
                for f in top],
    }
