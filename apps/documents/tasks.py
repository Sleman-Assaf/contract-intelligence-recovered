"""Background extraction (§2).

Runs in a worker so a user closing the browser cannot abandon a job — the
failure mode the Base44 version had, where a contract could be stranded
mid-analysis forever.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from apps.contracts.models import ContractStatus

from .extractors import EncryptedDocumentError, ExtractionError
from .models import SourceFile
from .services import run_extraction

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    acks_late=True,
)
def extract_source_file(self, source_file_id: str, *, then_analyse: bool = True):
    """Extract one uploaded file, then hand off to analysis."""
    source_file = SourceFile.objects.select_related("contract").get(pk=source_file_id)
    contract = source_file.contract

    contract.status = ContractStatus.EXTRACTING
    contract.save(update_fields=["status", "updated_at"])

    try:
        outcome = run_extraction(source_file)
    except EncryptedDocumentError as exc:
        # Not retryable: a human must supply an unlocked copy.
        contract.status = ContractStatus.EXTRACTION_FAILED
        contract.save(update_fields=["status", "updated_at"])
        logger.warning("Encrypted document for contract %s: %s", contract.pk, exc)
        return {"status": "encrypted", "detail": str(exc)}
    except ExtractionError as exc:
        contract.status = ContractStatus.EXTRACTION_FAILED
        contract.save(update_fields=["status", "updated_at"])
        logger.warning("Extraction failed for contract %s: %s", contract.pk, exc)
        return {"status": "failed", "detail": str(exc)}

    # Page kinds, key facts and the file's own inconsistencies. In "auto"
    # mode the OCR + vision figure check settles the figures by rule and the
    # analysis starts with no one asked (figure_check.py); otherwise a
    # scanned file, or one whose figures disagree, waits for a reviewer to
    # confirm them (documents.confirm_views.confirm_facts).
    from django.conf import settings

    from .key_facts import analyse_document

    summary = analyse_document(outcome.document)
    automatic = settings.KEY_FACTS_CONFIRMATION == "auto"
    figure_check = None
    to_enter = 0
    if automatic:
        from . import figure_check as figures

        figure_check = figures.run(outcome.document).summary()
        # What the site could not read clearly, the user types in.
        to_enter = figures.needs_input(outcome.document)

    result = {
        "status": "extracted",
        "pages_native": outcome.pages_native,
        "pages_ocr": outcome.pages_ocr,
        "blocks": outcome.blocks_created,
        "vision_regions": outcome.regions_sent_to_vision,
        "pages_excluded": summary.pages_excluded,
        "facts_to_confirm": to_enter if automatic else sum(1 for f in summary.facts if f.needs_attention),
    }
    if figure_check is not None:
        result["figure_check"] = figure_check

    if (outcome.needs_human_review and not automatic) or summary.needs_confirmation or to_enter:
        contract.status = ContractStatus.NEEDS_SOURCE_REVIEW
        contract.save(update_fields=["status", "updated_at"])

        from apps.notifications import services as notify

        if outcome.conflicts:
            notify.source_conflict(contract, outcome.conflicts)
        notify.facts_to_confirm(contract, result["facts_to_confirm"])
        result.update(status="needs_source_review", conflicts=outcome.conflicts,
                      pages_failed=outcome.pages_failed)
        return result

    if then_analyse:
        from .queueing import queue_analysis

        queue_analysis(contract, outcome.document)
        result["analysis"] = "queued"

    return result


@shared_task(acks_late=True)
def process_chat_file(extracted_file_id: str):
    """Read one chat attachment on a worker (EXTRACTION_ASYNC)."""
    from .chat_files import process

    file = process(extracted_file_id)
    return {"status": file.status, "vision_calls": file.vision_calls}
