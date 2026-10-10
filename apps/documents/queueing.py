"""Queueing extraction and analysis with time limits sized to the file.

Every task ran under one fixed limit (CELERY_TASK_SOFT_TIME_LIMIT, 25
minutes). A scanned 179-page contract needs about 15 minutes of OCR alone and
far longer to analyse clause by clause, so a large file would have been cut
off part way, every time. The limit now grows with the pages to process; the
fixed setting stays the floor.
"""
from __future__ import annotations

from django.conf import settings


def _limits(units: int, per_unit: float, base: int) -> tuple[int, int]:
    floor_soft = int(getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 25 * 60))
    soft = max(floor_soft, int(base + units * per_unit))
    soft = min(soft, int(settings.TASK_TIME_LIMIT_CEILING))
    return soft, soft + 5 * 60


def extraction_limits(page_count: int) -> tuple[int, int]:
    # The figure check reads up to FIGURE_CHECK_MAX_PAGES pages with vision
    # (about 20-30 s each) plus a few closer readings.
    figure_check = 0
    if getattr(settings, "FIGURE_CHECK_ENABLED", False) and settings.KEY_FACTS_CONFIRMATION == "auto":
        figure_check = (settings.FIGURE_CHECK_MAX_PAGES + settings.FIGURE_CHECK_TIEBREAK_CALLS) * 60
    return _limits(page_count or 0, settings.EXTRACTION_SECONDS_PER_PAGE, base=10 * 60 + figure_check)


def analysis_limits(page_count: int) -> tuple[int, int]:
    return _limits(page_count or 0, settings.ANALYSIS_SECONDS_PER_PAGE, base=10 * 60)


def pdf_page_count(path) -> int:
    try:
        import pymupdf

        with pymupdf.open(str(path)) as doc:
            return doc.page_count
    except Exception:
        return 0


def queue_extraction(source_file, *, then_analyse: bool = True):
    from .storage import absolute_path
    from .tasks import extract_source_file

    pages = source_file.page_count or pdf_page_count(absolute_path(source_file.stored_path))
    soft, hard = extraction_limits(pages)
    return extract_source_file.apply_async(
        args=[str(source_file.pk)], kwargs={"then_analyse": then_analyse},
        soft_time_limit=soft, time_limit=hard)


def queue_analysis(contract, document=None, *, actor_id=None, idempotency_key: str = ""):
    """Queue an analysis of the document's analysed pages."""
    from apps.analysis.tasks import analyse_contract

    pages = 0
    if document is not None:
        pages = document.pages.filter(include_in_analysis=True).count() or document.page_count
    soft, hard = analysis_limits(pages)
    kwargs = {"document_id": str(document.pk) if document else None}
    if actor_id:
        kwargs["actor_id"] = actor_id
    if idempotency_key:
        kwargs["idempotency_key"] = idempotency_key
    return analyse_contract.apply_async(args=[str(contract.pk)], kwargs=kwargs,
                                        soft_time_limit=soft, time_limit=hard)
