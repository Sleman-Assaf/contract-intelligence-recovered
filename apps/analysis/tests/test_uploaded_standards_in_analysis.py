"""An uploaded standard, all the way into an analysis.

Upload a standards file in Settings, save it, approve it, analyse a contract:
the analysis must compare against the uploaded position where one is
approved, the FIDIC baseline everywhere else, and never against a draft. The
pages that show the result -- the analysis, the risk matrix -- must agree.

Same real-PDF pipeline and stubbed model as test_pipeline_end_to_end.
"""
from __future__ import annotations

import io
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse

from apps.accounts.models import Entity, Role, User
from apps.analysis import services
from apps.analysis.models import ComplianceResult, Finding
from apps.analysis.tests.test_pipeline_end_to_end import _build_pdf, stub  # noqa: F401  (fixture)
from apps.contracts.models import ContractType
from apps.contracts.services import create_contract
from apps.documents.services import run_extraction
from apps.standards import normalization
from apps.standards.models import StandardProfile

pytestmark = pytest.mark.django_db

#: What AJi Legal uploads: two positions for Saudi Arabia, in the Legal
#: template's columns -- the delay-damages cap and the insurance floor.
POLICY_CSV = (
    "category,preferred_position,acceptable_range,red_flags,impact\n"
    "delayLiq,Delay damages capped at 10% of the contract value.,"
    "Up to 15% with Legal approval.,No cap on delay damages.,5\n"
    # Amounts with thousands separators are quoted, as any CSV writer does.
    'insurance,"Professional indemnity insurance of at least SAR 20,000,000.",,'
    '"Below SAR 15,000,000.",4\n'
)


@pytest.fixture
def people(settings, tmp_path):
    settings.DEBUG = True
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.STANDARDS_DEFAULT_PROFILE = "fidic"
    call_command("seed_reference", verbosity=0)
    call_command("seed_fidic_baseline", stdout=io.StringIO())
    entity = Entity.objects.get(code="aji-sa")
    make = lambda role, email: User.objects.create_user(  # noqa: E731
        email=email, password="test-password-1234", role=role, entity=entity)
    return {"admin": make(Role.ADMIN, "admin@aji.test"),
            "manager": make(Role.STANDARD_MANAGER, "standards@aji.test"),
            "uploader": make(Role.USER, "uploader@aji.test")}


def _upload_policy(people, *, effective=date(2020, 1, 1)):
    item = normalization.create_upload(people["admin"], POLICY_CSV.encode(), "ksa-positions.csv",
                                       profile=StandardProfile.AJI, country="SA", effective_date=effective)
    # the insurance row's rule is added by the reviewer on the review page
    for it in item.items:
        if it["category"] == "delayLiq":
            it["rule"] = {"extract": "percentage", "operator": "lte", "value": 10, "fallback_value": 15}
        if it["category"] == "insurance":
            it["rule"] = {"extract": "money", "operator": "gte", "value": 20000000, "currency": "SAR"}
    return normalization.save_drafts(item, item.items, people["admin"])


def _analyse(people, *, contract_date=None):
    contract, source_file = create_contract(
        actor=people["uploader"],
        uploaded_file=SimpleUploadedFile("agreement.pdf", _build_pdf(), "application/pdf"),
        project_name="Coastal District Masterplan", contract_title="Consultancy Services Agreement",
        client_name="NEOM Development Company", country="SA",
        entity=Entity.objects.get(code="aji-sa"), contract_type=ContractType.objects.get(code="arch"),
        contract_value=12400000, currency="SAR", language="en", effective_date=contract_date,
    )
    run_extraction(source_file)
    run = services.start_analysis(contract, document=contract.documents.latest("created_at"),
                                  actor=people["uploader"])
    services.run_analysis(run)
    run.refresh_from_db()
    return contract, run


def _finding(run, code):
    return Finding.objects.select_related("standard_version__standard").get(
        analysis=run, category__code=code)


def test_a_draft_is_never_used(people, stub):
    _upload_policy(people)                                   # saved, not approved
    _contract, run = _analyse(people)
    assert _finding(run, "delayLiq").standard_version.is_baseline


def test_an_approved_upload_is_what_the_contract_is_judged_against(people, stub):
    drafts = _upload_policy(people)
    for version in drafts:
        normalization.approve(version, people["manager"])
    _contract, run = _analyse(people)

    delay = _finding(run, "delayLiq")
    assert delay.standard_version == next(v for v in drafts if v.standard.category.code == "delayLiq")
    assert not delay.standard_version.is_baseline
    # The uploaded rule decided the arithmetic: 15% is over the 10% cap, inside the 15% fallback.
    assert delay.system_result in {ComplianceResult.PARTIAL, ComplianceResult.NON_COMPLIANT}
    insurance = _finding(run, "insurance")
    assert not insurance.standard_version.is_baseline
    assert insurance.system_result == ComplianceResult.NON_COMPLIANT      # SAR 18M < SAR 20M
    assert insurance.impact == 4                                         # the impact Legal set

    # Everything AJi did not upload is judged against the FIDIC baseline, and says so.
    # (A clause that complies produces no finding, so every finding outside
    # the two uploaded clauses is checked rather than a fixed list.)
    others = (Finding.objects.filter(analysis=run).exclude(category__code__in=["delayLiq", "insurance"])
              .exclude(standard_version=None).select_related("standard_version__standard"))
    assert others and all(f.standard_version.is_baseline for f in others)
    profiles = {entry["profile"] for entry in run.standards_snapshot.values()}
    assert profiles == {"aji_standard", "fidic"}
    # The progress the contract page showed ended on every clause.
    assert run.progress_total > 0 and run.progress_done == run.progress_total


def test_a_standard_not_yet_in_force_on_the_contracts_date_does_not_apply(people, stub):
    """Resolution is as of the contract's own date: a standard effective in
    2026 does not apply to a contract dated 2025, which gets FIDIC."""
    for version in _upload_policy(people, effective=date(2026, 10, 1)):
        normalization.approve(version, people["manager"])
    _contract, run = _analyse(people, contract_date=date(2025, 3, 1))
    assert _finding(run, "delayLiq").standard_version.is_baseline


def test_the_pages_agree_with_the_analysis(client, people, stub):
    for version in _upload_policy(people):
        normalization.approve(version, people["manager"])
    contract, run = _analyse(people)
    client.force_login(people["uploader"])

    detail = client.get(reverse("analysis:detail", args=[run.pk]) + "?tab=findings").content.decode()
    assert "FIDIC" in detail                                # the baseline-labelled findings

    matrix = client.get(reverse("analysis:risk_matrix"))
    findings = Finding.objects.filter(contract=contract)
    cells = [c for row in matrix.context["rows"] for c in row["cells"]]
    placed = findings.exclude(impact__isnull=True).exclude(probability__isnull=True).count()
    assert sum(c["count"] for c in cells) == placed and matrix.context["totals"]["all"] == findings.count()
