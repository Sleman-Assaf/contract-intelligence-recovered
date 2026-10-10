"""Contract creation and lifecycle services (§4, §36).

State transitions live here, not in views and not in the browser. The Base44
version updated contract status and workflow history as separate client-side
calls, which meant two people acting at once could leave them inconsistent.
Everything here runs inside a transaction.
"""
from __future__ import annotations

import uuid

from django.db import transaction
from django.utils import timezone

from apps.accounts import permissions as perms
from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.documents import storage
from apps.documents.models import SourceFile

from .models import Contract, ContractParty, ContractStatus, PartyRole


@transaction.atomic
def create_contract(*, actor, uploaded_file, **metadata) -> tuple[Contract, SourceFile]:
    """Validate, store privately, and record the contract.

    Atomic on purpose: a Contract row without its file, or a stored file with
    no row pointing at it, are both worse than a clean failure.
    """
    perms.require_capability(actor, perms.UPLOAD_CONTRACT)

    contract = Contract.objects.create(uploaded_by=actor, **metadata)

    try:
        stored = storage.store(uploaded_file, contract_id=contract.pk)
    except Exception:
        # The transaction rolls the row back; nothing partial survives.
        raise

    source_file = SourceFile.objects.create(
        contract=contract,
        original_filename=stored.original_filename,
        stored_path=stored.stored_path,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )

    # The AJi side of the contract, recorded explicitly. Which side AJi is on
    # decides the direction of every risk finding.
    ContractParty.objects.create(
        contract=contract,
        name=contract.entity.name,
        name_ar=contract.entity.name_ar,
        role=contract.analysis_perspective,
        is_aji=True,
    )
    ContractParty.objects.create(
        contract=contract,
        name=contract.client_name,
        role=(
            PartyRole.CLIENT
            if contract.analysis_perspective == PartyRole.CONSULTANT
            else PartyRole.CONSULTANT
        ),
        is_aji=False,
    )

    contract.submitted_at = timezone.now()
    contract.status = ContractStatus.UPLOADED
    contract.save(update_fields=["submitted_at", "status", "updated_at"])

    audit.record(
        AuditAction.CONTRACT_UPLOADED,
        actor=actor,
        entity_type="Contract",
        entity_id=contract.pk,
        contract_id=contract.pk,
        new_state=contract.status,
        details={
            "filename": stored.original_filename,
            "size_bytes": stored.size_bytes,
            "sha256": stored.sha256,
            "country": contract.country,
            "entity": contract.entity.code,
        },
    )
    return contract, source_file


def queue_processing(contract: Contract, source_file: SourceFile, *, actor=None) -> str:
    """Hand the file to the worker. Returns an idempotency key."""
    from apps.documents.queueing import queue_extraction

    key = uuid.uuid4().hex
    transaction.on_commit(lambda: queue_extraction(source_file, then_analyse=True))
    return key


def contracts_for(user, *, queryset=None):
    """Every list and aggregate must go through here (§34, §40)."""
    qs = queryset if queryset is not None else Contract.objects.all()
    return perms.scope_contracts(user, qs)


def start_new_round(*, contract, uploaded_file, actor, note: str = ""):
    """Record a revised draft as the next negotiation round (§41).

    The previous round is left exactly as it was — its file, its extraction and
    its findings all stay queryable. That is what makes "what changed since
    last time" answerable at all, and it is the record you need if the
    negotiation is ever disputed.
    """
    perms.require_capability(actor, perms.UPLOAD_CONTRACT)

    previous = contract.source_files.order_by("-round_number").first()
    next_round = (previous.round_number + 1) if previous else 1

    stored = storage.store(uploaded_file, contract_id=contract.pk)
    source_file = SourceFile.objects.create(
        contract=contract,
        original_filename=stored.original_filename,
        stored_path=stored.stored_path,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        round_number=next_round,
        supersedes=previous,
        round_note=note[:255],
    )

    # A new draft invalidates the old verdict: the contract is back in the
    # pipeline until the new round has been read and analysed.
    contract.status = ContractStatus.UPLOADED
    contract.save(update_fields=["status", "updated_at"])

    audit.record(
        AuditAction.CONTRACT_UPLOADED,
        actor=actor,
        entity_type="SourceFile",
        entity_id=source_file.pk,
        contract_id=contract.pk,
        new_state=contract.status,
        details={
            "round": next_round,
            "supersedes_round": previous.round_number if previous else None,
            "filename": stored.original_filename,
            "sha256": stored.sha256,
            "note": note[:255],
        },
    )
    return source_file
