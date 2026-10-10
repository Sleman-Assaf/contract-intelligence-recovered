"""Every screen must actually render, and every guard must actually hold.

§1 of the brief: no disconnected pages, no fake buttons. These tests load each
page as a real request and assert the response — a template that does not
compile, or a permission that is only enforced in the UI, fails here.
"""
from __future__ import annotations

import io

import pytest
from django.urls import reverse

from apps.accounts.models import Country, Entity, Role, User
from apps.analysis.models import AnalysisRun, Finding
from apps.contracts.models import Contract, ContractStatus, ContractType
from apps.standards.models import ClauseCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def entity():
    return Entity.objects.create(
        name="AJi - Saudi Arabia", code="aji-sa", country=Country.SAUDI_ARABIA
    )


@pytest.fixture
def contract_type():
    return ContractType.objects.create(code="design", name="Design Consultancy")


@pytest.fixture
def reviewer(entity):
    return User.objects.create_user(
        email="reviewer@aji.test", password="test-password-1234",
        role=Role.CONTRACT_REVIEWER, entity=entity,
    )


@pytest.fixture
def contract(entity, contract_type, reviewer):
    return Contract.objects.create(
        project_name="Riyadh Business District",
        contract_title="Design Consultancy Agreement",
        client_name="Ministry of Culture",
        reference="4500002530",
        country=Country.SAUDI_ARABIA,
        entity=entity,
        contract_type=contract_type,
        uploaded_by=reviewer,
        status=ContractStatus.PENDING_CONTRACT_REVIEW,
    )


@pytest.fixture
def run_with_finding(contract):
    run = AnalysisRun.objects.create(
        contract=contract, version=1, status="succeeded",
        overall_risk="high", high_count=1,
        executive_summary="Uncapped delay damages.",
    )
    category = ClauseCategory.objects.create(code="delay", name="Delay damages")
    Finding.objects.create(
        analysis=run, contract=contract, category=category,
        risk_level="high", impact=5, probability=4, risk_score=20,
        title="Uncapped liquidated damages",
        clause_number="12.3", page_number=14,
        original_wording="Damages shall not exceed ...",
        system_result="non_compliant", ai_result="non_compliant",
        verification_status="verified",
    )
    contract.current_analysis = run
    contract.save(update_fields=["current_analysis"])
    return run


@pytest.fixture
def signed_in(client, reviewer):
    client.force_login(reviewer)
    return client


# ── every screen renders ─────────────────────────────────────────────────

class TestScreensRender:
    @pytest.mark.parametrize("name", [
        "contracts:dashboard",
        "contracts:list",
        "contracts:upload",
        "analysis:risk_matrix",
        "analysis:comparison",
        "analysis:advisor",
        "workflow:index",
        "reports:index",
        "accounts:settings",
    ])
    def test_screen_returns_200(self, signed_in, contract, name):
        response = signed_in.get(reverse(name))
        assert response.status_code == 200, f"{name} failed"

    def test_contract_detail_renders(self, signed_in, contract):
        response = signed_in.get(reverse("contracts:detail", args=[contract.pk]))
        assert response.status_code == 200
        assert contract.project_name in response.content.decode()

    def test_analysis_detail_renders(self, signed_in, run_with_finding):
        url = reverse("analysis:detail", args=[run_with_finding.pk])
        response = signed_in.get(url)
        assert response.status_code == 200

        # Findings live on their own tab now; the summary no longer lists them.
        response = signed_in.get(f"{url}?tab=findings")
        body = response.content.decode()
        assert response.status_code == 200
        assert "Uncapped liquidated damages" in body
        assert "12.3" in body

    def test_settings_tabs_all_render(self, signed_in):
        for tab in ("users", "audit", "system"):
            response = signed_in.get(reverse("accounts:settings"), {"tab": tab})
            assert response.status_code == 200, f"settings tab {tab} failed"


# ── authentication and scoping ───────────────────────────────────────────

class TestAccessControl:
    @pytest.mark.parametrize("name", [
        "contracts:dashboard", "contracts:list", "analysis:risk_matrix",
        "workflow:index", "reports:index", "accounts:settings",
    ])
    def test_anonymous_is_redirected_to_login(self, client, name):
        response = client.get(reverse(name))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_outsider_cannot_open_another_users_contract(self, client, contract, entity):
        outsider = User.objects.create_user(
            email="outsider@aji.test", password="test-password-1234",
            role=Role.USER, entity=entity,
        )
        client.force_login(outsider)
        response = client.get(reverse("contracts:detail", args=[contract.pk]))
        assert response.status_code == 404      # scoped out, not merely hidden

    def test_outsider_sees_no_contracts_in_the_list(self, client, contract, entity):
        outsider = User.objects.create_user(
            email="outsider2@aji.test", password="test-password-1234",
            role=Role.USER, entity=entity,
        )
        client.force_login(outsider)
        response = client.get(reverse("contracts:list"))
        assert response.status_code == 200
        assert contract.project_name not in response.content.decode()

    def test_role_without_upload_capability_is_refused(self, client, entity):
        exec_user = User.objects.create_user(
            email="exec@aji.test", password="test-password-1234",
            role=Role.MANAGEMENT, entity=entity,
        )
        client.force_login(exec_user)
        response = client.get(reverse("contracts:upload"))
        assert response.status_code == 403

    def test_download_requires_access(self, client, contract, entity):
        from apps.documents.models import SourceFile

        source = SourceFile.objects.create(
            contract=contract, original_filename="c.pdf",
            stored_path="contracts/x/y.pdf", content_type="application/pdf",
            size_bytes=10, sha256="0" * 64,
        )
        outsider = User.objects.create_user(
            email="outsider3@aji.test", password="test-password-1234",
            role=Role.USER, entity=entity,
        )
        client.force_login(outsider)
        response = client.get(reverse("contracts:download", args=[source.pk]))
        assert response.status_code in (403, 404)


# ── upload ───────────────────────────────────────────────────────────────

class TestUploadForm:
    def _pdf(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        # A minimal but genuine PDF, so magic-number validation passes.
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Clause 1. The Consultant shall provide services "
                                    "in accordance with this Agreement and applicable law.")
        data = doc.tobytes()
        doc.close()
        return SimpleUploadedFile("contract.pdf", data, content_type="application/pdf")

    def test_country_is_required(self, signed_in, contract_type, entity):
        response = signed_in.post(reverse("contracts:upload"), {
            "project_name": "P", "client_name": "C", "contract_title": "T",
            "contract_type": contract_type.pk, "currency": "SAR", "language": "ar",
            "entity": entity.pk, "analysis_perspective": "consultant",
            "counterparty_type": "private", "reference_template": "aji_standard",
            "file": self._pdf(),
        })
        assert response.status_code == 200          # redisplayed with errors
        assert "country" in response.context["form"].errors

    def test_rejects_a_non_pdf_masquerading_as_one(self, signed_in, contract_type, entity):
        from django.core.files.uploadedfile import SimpleUploadedFile

        fake = SimpleUploadedFile("evil.pdf", b"MZ\x90\x00 not a pdf",
                                  content_type="application/pdf")
        response = signed_in.post(reverse("contracts:upload"), {
            "project_name": "P", "client_name": "C", "contract_title": "T",
            "contract_type": contract_type.pk, "country": Country.SAUDI_ARABIA,
            "currency": "SAR", "language": "ar", "entity": entity.pk,
            "analysis_perspective": "consultant", "counterparty_type": "private",
            "reference_template": "aji_standard", "file": fake,
        })
        assert response.status_code == 200
        assert "file" in response.context["form"].errors

    def test_expiry_before_effective_is_rejected(self, signed_in, contract_type, entity):
        response = signed_in.post(reverse("contracts:upload"), {
            "project_name": "P", "client_name": "C", "contract_title": "T",
            "contract_type": contract_type.pk, "country": Country.SAUDI_ARABIA,
            "currency": "SAR", "language": "ar", "entity": entity.pk,
            "analysis_perspective": "consultant", "counterparty_type": "private",
            "reference_template": "aji_standard",
            "effective_date": "2026-09-01", "expiry_date": "2026-01-01",
            "file": self._pdf(),
        })
        assert response.status_code == 200
        assert "expiry_date" in response.context["form"].errors


# ── the source viewer endpoint ───────────────────────────────────────────

class TestFindingSource:
    def test_returns_evidence_for_a_finding(self, signed_in, run_with_finding):
        finding = run_with_finding.findings.first()
        response = signed_in.get(reverse("analysis:finding_source", args=[finding.pk]))
        assert response.status_code == 200
        payload = response.json()
        assert payload["clause_number"] == "12.3"
        assert payload["page_number"] == 14
        assert "blocks" in payload


class TestUploadProblemsAreSaidAtTheTop:
    """A form that came back with errors showed them only beside their fields,
    which may be out of view: nothing seemed to happen."""

    def _alert(self, html):
        start = html.index("data-form-alert")
        return html[html.rindex("<div", 0, start):html.index("</ul>", start)]

    def test_no_notice_on_a_fresh_form(self, signed_in):
        html = signed_in.get(reverse("contracts:upload")).content.decode()
        assert "hidden" in self._alert(html).split(">", 1)[0]

    def test_every_problem_is_listed_at_the_top(self, signed_in, contract_type, entity):
        from django.core.files.uploadedfile import SimpleUploadedFile

        fake = SimpleUploadedFile("evil.pdf", b"MZ\x90\x00 not a pdf", content_type="application/pdf")
        response = signed_in.post(reverse("contracts:upload"), {
            "project_name": "", "client_name": "C", "contract_title": "T",
            "contract_type": contract_type.pk, "currency": "SAR", "language": "ar",
            "entity": entity.pk, "analysis_perspective": "consultant",
            "counterparty_type": "private", "reference_template": "aji_standard", "file": fake,
        })
        html = response.content.decode()
        alert = self._alert(html)
        assert "hidden" not in alert.split(">", 1)[0]
        assert html.index("data-form-alert") < html.index('class="split"')      # above the form
        form = response.context["form"]
        for name in ("project_name", "country"):
            assert name in form.errors
            assert f'href="#{form[name].id_for_label}"' in alert
        assert alert.count("<li>") >= 2
