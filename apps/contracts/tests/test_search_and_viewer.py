"""Search scoping and the source viewer (§26, §39, §48)."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import Country, Entity, Role, User
from apps.analysis.models import AnalysisRun, Clause, Finding
from apps.contracts.models import Contract, ContractType
from apps.contracts.search import SearchFilters, search
from apps.documents.models import Block, Document, ExtractionMethod, Page, SourceFile, VerificationStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def entity():
    return Entity.objects.create(
        name="AJi - Saudi Arabia", code="aji-sa", country=Country.SAUDI_ARABIA
    )


def make_user(email, role, entity):
    return User.objects.create_user(
        email=email, password="test-password-1234", role=role, entity=entity
    )


@pytest.fixture
def reviewer(entity):
    return make_user("reviewer@aji.test", Role.CONTRACT_REVIEWER, entity)


@pytest.fixture
def contract(entity, reviewer):
    contract_type = ContractType.objects.create(code="design", name="Design")
    return Contract.objects.create(
        project_name="Riyadh Business District",
        contract_title="Design Consultancy Agreement",
        client_name="Ministry of Culture",
        reference="4500002530",
        country=Country.SAUDI_ARABIA, entity=entity,
        contract_type=contract_type, uploaded_by=reviewer,
        risk_level="high",
    )


@pytest.fixture
def evidence(contract):
    """A finding backed by a real block with coordinates."""
    source = SourceFile.objects.create(
        contract=contract, original_filename="c.pdf",
        stored_path="contracts/x/y.pdf", content_type="application/pdf",
        size_bytes=1000, sha256="a" * 64, page_count=2,
    )
    document = Document.objects.create(
        source_file=source, contract=contract, page_count=2
    )
    page = Page.objects.create(
        document=document, page_number=14, width=595.0, height=842.0,
        has_native_text=True, extraction_method=ExtractionMethod.NATIVE_PDF,
        verification_status=VerificationStatus.ACCEPTED,
    )
    block = Block.objects.create(
        page=page, document=document, block_index=0,
        original_source_text="Damages shall not exceed 10% of the contract value.",
        bbox_x0=72.0, bbox_y0=210.5, bbox_x1=520.0, bbox_y1=245.0,
        extraction_method=ExtractionMethod.NATIVE_PDF,
        verification_status=VerificationStatus.ACCEPTED,
        contains_critical_value=True,
    )
    run = AnalysisRun.objects.create(contract=contract, version=1, status="succeeded")
    Clause.objects.create(
        document=document, contract=contract, clause_number="12.3",
        text="Damages shall not exceed 10% of the contract value.",
        page_number=14, ordinal=1,
    )
    finding = Finding.objects.create(
        analysis=run, contract=contract, risk_level="high",
        title="Uncapped liquidated damages", clause_number="12.3", page_number=14,
        original_wording="Damages shall not exceed 10% of the contract value.",
        system_result="non_compliant", ai_result="non_compliant",
        verification_status="verified",
    )
    finding.source_blocks.set([block])
    contract.current_analysis = run
    contract.save(update_fields=["current_analysis"])
    return {"finding": finding, "block": block, "page": page, "document": document}


# ── search ───────────────────────────────────────────────────────────────

class TestSearch:
    def test_empty_filters_return_nothing(self, reviewer):
        results = search(reviewer, SearchFilters())
        assert results.total == 0

    def test_finds_by_client(self, reviewer, contract):
        results = search(reviewer, SearchFilters(q="Ministry"))
        assert contract in list(results.contracts)

    def test_finds_by_reference(self, reviewer, contract):
        results = search(reviewer, SearchFilters(q="4500002530"))
        assert results.contract_count == 1

    def test_structured_filter_without_free_text(self, reviewer, contract):
        results = search(reviewer, SearchFilters(risk="high"))
        assert results.contract_count == 1

    def test_wrong_country_excludes_it(self, reviewer, contract):
        results = search(reviewer, SearchFilters(q="Ministry", country=Country.JORDAN))
        assert results.contract_count == 0

    def test_searches_stored_clause_text(self, reviewer, contract, evidence):
        """A clause hit must be real document text, not a paraphrase."""
        results = search(reviewer, SearchFilters(q="10%", scope="clauses"))
        assert results.clause_count == 1

    def test_searches_findings(self, reviewer, contract, evidence):
        results = search(reviewer, SearchFilters(q="liquidated", scope="findings"))
        assert results.finding_count == 1

    def test_scope_limits_what_is_searched(self, reviewer, contract, evidence):
        results = search(reviewer, SearchFilters(q="Ministry", scope="contracts"))
        assert results.contract_count == 1
        assert results.clause_count == 0
        assert results.finding_count == 0


class TestSearchScoping:
    """Search must never become a way to discover a contract you cannot open."""

    def test_outsider_finds_no_contracts(self, contract, entity):
        outsider = make_user("outsider@aji.test", Role.USER, entity)
        results = search(outsider, SearchFilters(q="Ministry"))
        assert results.contract_count == 0

    def test_outsider_finds_no_clauses(self, contract, evidence, entity):
        outsider = make_user("outsider2@aji.test", Role.USER, entity)
        results = search(outsider, SearchFilters(q="10%", scope="clauses"))
        assert results.clause_count == 0

    def test_outsider_finds_no_findings(self, contract, evidence, entity):
        outsider = make_user("outsider3@aji.test", Role.USER, entity)
        results = search(outsider, SearchFilters(q="liquidated", scope="findings"))
        assert results.finding_count == 0

    def test_search_page_renders(self, client, reviewer, contract):
        client.force_login(reviewer)
        response = client.get(reverse("contracts:search"), {"q": "Ministry"})
        assert response.status_code == 200
        assert "Riyadh Business District" in response.content.decode()


# ── source viewer ────────────────────────────────────────────────────────

class TestSourceViewer:
    def test_renders_with_a_highlight(self, client, reviewer, evidence):
        client.force_login(reviewer)
        finding = evidence["finding"]
        response = client.get(reverse("analysis:source_viewer", args=[finding.pk]))
        assert response.status_code == 200
        assert response.context["has_geometry"] is True
        assert len(response.context["highlights"]) == 1

    def test_highlight_is_a_percentage_of_the_page(self, client, reviewer, evidence):
        """Percentages, so the overlay stays aligned at any render resolution."""
        client.force_login(reviewer)
        response = client.get(
            reverse("analysis:source_viewer", args=[evidence["finding"].pk])
        )
        highlight = response.context["highlights"][0]
        # bbox 72..520 of a 595pt page
        assert round(highlight.left, 1) == round(72.0 / 595.0 * 100, 1)
        assert round(highlight.width, 1) == round((520.0 - 72.0) / 595.0 * 100, 1)
        assert 0 <= highlight.top <= 100
        assert highlight.is_critical is True

    def test_uses_the_page_the_evidence_is_on(self, client, reviewer, evidence):
        """The block's page beats whatever the model reported."""
        client.force_login(reviewer)
        response = client.get(
            reverse("analysis:source_viewer", args=[evidence["finding"].pk])
        )
        assert response.context["page_number"] == 14

    def test_finding_without_evidence_is_flagged_not_faked(self, client, reviewer, contract):
        run = AnalysisRun.objects.create(contract=contract, version=2)
        orphan = Finding.objects.create(
            analysis=run, contract=contract, risk_level="medium",
            title="No evidence attached", clause_number="9.1", page_number=3,
        )
        client.force_login(reviewer)
        response = client.get(reverse("analysis:source_viewer", args=[orphan.pk]))
        body = response.content.decode()
        assert response.status_code == 200
        assert response.context["no_evidence"] is True
        assert "غير مُثبت" in body      # citation explicitly marked unverified

    def test_outsider_cannot_open_the_viewer(self, client, evidence, entity):
        outsider = make_user("outsider4@aji.test", Role.USER, entity)
        client.force_login(outsider)
        response = client.get(
            reverse("analysis:source_viewer", args=[evidence["finding"].pk])
        )
        assert response.status_code == 403

    def test_page_image_requires_authentication(self, client, evidence):
        response = client.get(
            reverse("documents:page_image", args=[evidence["document"].pk, 14])
        )
        assert response.status_code == 403

    def test_page_blocks_endpoint_returns_percentages(self, client, reviewer, evidence):
        client.force_login(reviewer)
        response = client.get(
            reverse("documents:page_blocks", args=[evidence["document"].pk, 14])
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["page"] == 14
        rect = payload["blocks"][0]["rect"]
        assert 0 <= rect["left"] <= 100
        assert payload["blocks"][0]["contains_critical_value"] is True
