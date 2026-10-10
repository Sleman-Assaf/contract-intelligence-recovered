"""Background analysis (§2, §43)."""
from __future__ import annotations

import logging

from celery import shared_task

from apps.contracts.models import Contract
from apps.documents.models import Document

from .models import AnalysisRun, AnalysisStatus
from .services import run_analysis, start_analysis

logger = logging.getLogger(__name__)


@shared_task(bind=True, acks_late=True)
def analyse_contract(self, contract_id: str, *, document_id: str | None = None,
                     actor_id: int | None = None, idempotency_key: str = ""):
    """Create a run and execute it."""
    contract = Contract.objects.select_related("contract_type", "entity").get(pk=contract_id)

    # Idempotency: an accidental double-submit must not produce two analyses.
    if idempotency_key:
        existing = AnalysisRun.objects.filter(
            contract=contract, idempotency_key=idempotency_key
        ).exclude(status=AnalysisStatus.FAILED).first()
        if existing:
            return {"status": "duplicate_ignored", "run": str(existing.pk)}

    document = Document.objects.filter(pk=document_id).first() if document_id else None

    actor = None
    if actor_id:
        from apps.accounts.models import User

        actor = User.objects.filter(pk=actor_id).first()

    run = start_analysis(
        contract, document=document, actor=actor, idempotency_key=idempotency_key
    )
    run.task_id = self.request.id or ""
    run.save(update_fields=["task_id", "updated_at"])

    try:
        outcome = run_analysis(run)
    except Exception as exc:                       # already recorded by the service
        logger.exception("analyse_contract failed for %s", contract_id)
        return {"status": "failed", "run": str(run.pk), "error": str(exc)[:500]}

    # The chat's copy of the file: its clauses as searchable passages (RAG),
    # beside the original upload. Only a run that read the contract; a
    # failure here leaves the analysis standing and the file out of the chat.
    from . import rag

    if run.status != AnalysisStatus.FAILED:
        rag.build_quietly(run)

    # The findings are readable now, in AI_OUTPUT_LANGUAGE. The other
    # language and the clause reading aid follow in their own task, so the
    # reader is not kept waiting for translations.
    if outcome.findings_created:
        from django.db import transaction

        transaction.on_commit(lambda: translate_findings.delay(str(run.pk)))

    return {
        # The run's own verdict, not a blanket "succeeded" - a degraded run
        # that could not read half the contract must say so to its caller too.
        "status": run.status,
        "run": str(run.pk),
        "version": run.version,
        "findings": outcome.findings_created,
        "conflicts": outcome.conflicts,
        "missing_clauses": outcome.missing_clauses,
        "ai_failures": outcome.ai_failures,
        "unclassified_clauses": outcome.unclassified_clauses,
    }


@shared_task
def reanalyse_contract(contract_id: str, actor_id: int | None = None):
    """Explicit re-analysis. Creates a new version; never overwrites (§32)."""
    return analyse_contract(contract_id, actor_id=actor_id)


@shared_task(acks_late=True)
def translate_findings(run_id: str):
    """Fill in the languages each finding of a run is missing. One failed
    translation leaves that finding in the language it was written in -- the
    pages fall back to it -- and never touches the analysis itself."""
    from apps.ai.services import ai_service

    from .models import Finding

    run = AnalysisRun.objects.filter(pk=run_id).first()
    if not run:
        return {"status": "missing", "run": run_id}
    added = failed = 0
    for finding in Finding.objects.filter(analysis=run):          # highest risk first
        try:
            added += bool(ai_service.translate_finding(finding=finding, analysis_run=run))
        except Exception:                          # noqa: BLE001 - one finding, not the run
            failed += 1
            logger.warning("translation failed for finding %s", finding.pk, exc_info=True)
    return {"status": "done", "run": run_id, "translated": added, "failed": failed}
