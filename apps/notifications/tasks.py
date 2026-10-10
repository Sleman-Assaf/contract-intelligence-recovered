"""Scheduled lifecycle monitoring (§41).

Thresholds come from `EXPIRY_WARNING_DAYS`, so extending the warning window is
a configuration change rather than a deployment.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.contracts.models import Contract, ContractStatus

from . import services

logger = logging.getLogger(__name__)

#: Contracts in these states are no longer worth chasing about expiry.
_INACTIVE = {
    ContractStatus.REJECTED,
    ContractStatus.ARCHIVED,
}


@shared_task
def check_expiring_contracts() -> dict:
    """Warn about contracts approaching expiry, once per threshold.

    De-duplication lives on the Notification record, so running this hourly or
    nightly produces the same number of messages.
    """
    today = timezone.localdate()
    thresholds = sorted(settings.EXPIRY_WARNING_DAYS)
    sent = 0

    for days in thresholds:
        target = today + timezone.timedelta(days=days)
        contracts = (
            Contract.objects.filter(expiry_date=target)
            .exclude(status__in=_INACTIVE)
            .select_related("reviewer", "uploaded_by")
        )
        for contract in contracts:
            services.expiry_approaching(contract, days)
            sent += 1

    return {"thresholds": thresholds, "notified": sent}


@shared_task
def check_expired_contracts() -> dict:
    """Flag contracts whose expiry date has passed."""
    today = timezone.localdate()
    contracts = (
        Contract.objects.filter(expiry_date__lt=today)
        .exclude(status__in=_INACTIVE)
        .select_related("reviewer", "uploaded_by")
    )
    count = 0
    for contract in contracts:
        services.contract_expired(contract)
        count += 1
    return {"notified": count}


@shared_task
def retry_stalled_analyses(max_age_minutes: int = 60) -> dict:
    """Recover jobs whose worker died mid-run.

    Without this a contract can sit in `ai_analysis` forever — the failure mode
    the browser-driven version had by design.
    """
    from apps.analysis.models import AnalysisRun, AnalysisStatus

    cutoff = timezone.now() - timezone.timedelta(minutes=max_age_minutes)
    stalled = AnalysisRun.objects.filter(
        status=AnalysisStatus.RUNNING, started_at__lt=cutoff
    ).select_related("contract")

    recovered = 0
    for run in stalled:
        run.status = AnalysisStatus.FAILED
        run.error_message = (
            f"No progress for over {max_age_minutes} minutes; presumed lost "
            "when the worker stopped."
        )
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error_message", "finished_at", "updated_at"])

        contract = run.contract
        contract.status = ContractStatus.ANALYSIS_FAILED
        contract.save(update_fields=["status", "updated_at"])

        services.analysis_failed(run, run.error_message)
        recovered += 1

    return {"stalled": recovered}
