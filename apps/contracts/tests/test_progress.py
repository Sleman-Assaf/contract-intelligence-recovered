"""The contract's progress card: measured from the pipeline, never invented."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Country, Entity, Role, User
from apps.analysis.models import AnalysisRun, AnalysisStatus
from apps.contracts.models import Contract, ContractStatus, ContractType
from apps.contracts.progress import progress_for
from apps.documents.models import Block, Document, Page, SourceFile

pytestmark = pytest.mark.django_db


@pytest.fixture
def contract():
    entity = Entity.objects.create(name="E", code="e", country=Country.JORDAN)
    user = User.objects.create_user(email="u@aji.test", password="x-long-passphrase-42",
                                    role=Role.CONTRACT_REVIEWER, entity=entity)
    return Contract.objects.create(project_name="P", contract_title="T", client_name="C",
                                   country=Country.JORDAN, entity=entity, uploaded_by=user,
                                   contract_type=ContractType.objects.create(code="x", name="X"))


def _document(contract, pages=4, read=0):
    source = SourceFile.objects.create(contract=contract, original_filename="c.pdf", stored_path="c.pdf",
                                       content_type="application/pdf", size_bytes=1, sha256="0" * 64)
    doc = Document.objects.create(source_file=source, contract=contract, page_count=pages)
    for n in range(1, pages + 1):
        page = Page.objects.create(document=doc, page_number=n, extraction_method="ocr")
        if n <= read:
            Block.objects.create(page=page, document=doc, block_index=0, original_source_text="x",
                                 extraction_method="ocr")
    return doc


class TestThePercentage:
    def test_waiting(self, contract):
        state = progress_for(contract)
        assert state["percent"] == 3 and state["step"] == "queued" and not state["stalled"]

    def test_waiting_too_long_is_said_plainly(self, contract):
        Contract.objects.filter(pk=contract.pk).update(updated_at=timezone.now() - timedelta(minutes=5))
        contract.refresh_from_db()
        assert progress_for(contract)["stalled"] is True

    def test_reading_moves_with_the_pages(self, contract):
        contract.status = ContractStatus.EXTRACTING
        _document(contract, pages=4, read=2)
        state = progress_for(contract)
        assert state["step"] == "reading" and state["percent"] == 5 + round(35 * 2 / 4)
        assert "2" in state["detail"] and "4" in state["detail"]

    def test_analysing_moves_with_the_clauses(self, contract):
        contract.status = ContractStatus.AI_ANALYSIS
        AnalysisRun.objects.create(contract=contract, version=1, status=AnalysisStatus.RUNNING,
                                   progress_done=5, progress_total=20)
        state = progress_for(contract)
        assert state["step"] == "analysing" and state["percent"] == 45 + round(50 * 5 / 20)

    def test_it_only_moves_forward_across_stages(self, contract):
        seen = [progress_for(contract)["percent"]]
        contract.status = ContractStatus.EXTRACTING
        _document(contract, pages=2, read=2)
        seen.append(progress_for(contract)["percent"])
        contract.status = ContractStatus.AI_ANALYSIS
        run = AnalysisRun.objects.create(contract=contract, version=1, status=AnalysisStatus.RUNNING,
                                         progress_done=0, progress_total=10)
        seen.append(progress_for(contract)["percent"])
        run.progress_done = 10; run.save()
        seen.append(progress_for(contract)["percent"])
        contract.status = ContractStatus.PENDING_CONTRACT_REVIEW
        seen.append(progress_for(contract)["percent"])
        assert seen == sorted(seen) and seen[-1] == 100

    def test_done_points_at_the_result(self, contract):
        contract.status = ContractStatus.PENDING_CONTRACT_REVIEW
        run = AnalysisRun.objects.create(contract=contract, version=1, status=AnalysisStatus.SUCCEEDED)
        contract.current_analysis = run
        state = progress_for(contract)
        assert state["done"] and state["next"] == reverse("analysis:detail", args=[run.pk])

    def test_a_failure_is_not_shown_as_success(self, contract):
        contract.status = ContractStatus.ANALYSIS_FAILED
        state = progress_for(contract)
        assert state["done"] and state["failed"]


class TestThePage:
    def test_the_card_replaces_the_banner(self, client, contract):
        client.force_login(contract.uploaded_by)
        body = client.get(reverse("contracts:detail", args=[contract.pk])).content.decode()
        assert "data-progress" in body and 'role="progressbar"' in body and "3%" in body
        assert "js/progress.js" in body

    def test_the_endpoint_is_scoped(self, client, contract):
        client.force_login(contract.uploaded_by)
        assert client.get(reverse("contracts:progress", args=[contract.pk])).json()["percent"] == 3
        outsider = User.objects.create_user(email="o@aji.test", password="x-long-passphrase-42",
                                            role=Role.USER, entity=contract.entity)
        client.force_login(outsider)
        assert client.get(reverse("contracts:progress", args=[contract.pk])).status_code == 404
