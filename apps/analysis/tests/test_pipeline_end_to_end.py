"""The whole pipeline on a real PDF: upload -> extract -> analyse -> roll up.

Every other test exercises one stage. This one builds an actual PDF with a real
text layer, stores it through the upload service, extracts it with PyMuPDF and
runs the analysis orchestrator over the result.

The AI provider is stubbed, deliberately: the point is to prove the pipeline's
own wiring, and a test that depends on a live metered API proves nothing on the
day the quota runs out. The stub answers exactly as the real model answered
when this contract was run against a live model, including disagreeing with the
rules engine on the delay-damages clause.
"""
from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from apps.accounts.models import Entity, User
from apps.ai.providers.base import AICallResult
from apps.ai.schemas import (
    ClauseClassification,
    Compliance,
    SemanticAssessment,
    VerificationResult,
    VerifierVerdict,
)
from apps.analysis import services
from apps.analysis.models import (
    AnalysisStatus,
    ComplianceResult,
    MissingClause,
    VerificationOutcome,
)
from apps.contracts.models import ContractType
from apps.contracts.services import create_contract
from apps.documents.services import run_extraction

pytestmark = pytest.mark.django_db

#: Clause text, and the category the stub classifier reports for it. Values are
#: chosen to land in a different band of the seeded standards each time.
CLAUSES = [
    ("1.  SCOPE OF SERVICES",
     "The Consultant shall provide architectural and engineering design "
     "services for the Project as described in the Request for Proposal.",
     "scope"),
    ("2.  PAYMENT",
     "The total lump sum price is SAR 12,400,000. 30% of the Contract Price "
     "shall be linked to approved delivery milestones.",
     "financial"),
    ("3.  DELAY DAMAGES",
     "The Consultant shall pay liquidated damages for delay, capped at 15% of "
     "the total contract value.",
     "delayLiq"),
    ("4.  PROFESSIONAL INDEMNITY INSURANCE",
     "The Consultant shall maintain professional indemnity insurance with a "
     "limit of not less than SAR 18,000,000 for each and every claim.",
     "insurance"),
    ("5.  DISPUTE RESOLUTION",
     "Any dispute shall be referred to arbitration under the Rules of the "
     "Saudi Center for Commercial Arbitration, seated in Riyadh.",
     "dispute"),
]


def _build_pdf() -> bytes:
    """A genuine PDF with a selectable text layer, not an image."""
    pymupdf = pytest.importorskip("pymupdf")

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 72
    page.insert_text((72, y), "CONSULTANCY SERVICES AGREEMENT",
                     fontname="helv", fontsize=15)
    y += 30
    for heading, body, _ in CLAUSES:
        page.insert_text((72, y), heading, fontname="hebo", fontsize=10.5)
        y += 16
        page.insert_textbox(
            pymupdf.Rect(72, y, 523, 842 - 72), body,
            fontname="helv", fontsize=10, lineheight=1.35,
        )
        y += 58
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


class _StubProvider:
    """Answers the way the real model did, without the network or the quota."""

    def __init__(self):
        self.classify_calls = 0
        self.assess_calls = 0

    def _category_for(self, clause_text: str) -> str:
        lowered = clause_text.lower()
        for _, body, code in CLAUSES:
            # Match on a distinctive fragment so clause grouping is what is
            # being tested, not string equality.
            if body.split(".")[0][:40].lower() in lowered:
                return code
        return "scope"

    def classify_clause(self, *, clause_text, categories, language="ar", **kwargs):
        self.classify_calls += 1
        return AICallResult(
            parsed=ClauseClassification(
                primary=self._category_for(clause_text),
                alternatives=[], confidence=0.95, reasoning="stub",
            ),
            provider="stub", model="stub", request_type="classify_clause",
            latency_ms=1,
        )

    def assess_clause(self, *, clause_text, standard_position, **kwargs):
        self.assess_calls += 1
        lowered = clause_text.lower()
        if "15%" in lowered:
            # What the model actually said: 15% exceeds the preferred 10% but sits
            # inside the approved fallback, so "partial".
            result = Compliance.PARTIAL
            deviation = "Cap of 15% exceeds the preferred 10% but is within the fallback."
        elif "18,000,000" in lowered:
            result = Compliance.NON_COMPLIANT
            deviation = "Cover of SAR 18,000,000 is below the SAR 20,000,000 fallback."
        elif "arbitration" in lowered:
            result = Compliance.COMPLIANT
            deviation = ""
        else:
            result = Compliance.NON_COMPLIANT
            deviation = "Milestone-linked payment is below the standard."
        return AICallResult(
            parsed=SemanticAssessment(
                ai_result=result, deviation=deviation, explanation="stub",
                business_impact="stub", recommended_action="stub",
                confidence=0.95,
            ),
            provider="stub", model="stub", request_type="assess_clause",
            latency_ms=1,
        )

    def verify_finding(self, **kwargs):
        return AICallResult(
            parsed=VerificationResult(
                verdict=VerifierVerdict.AGREE, evidence="stub", confidence=0.9
            ),
            provider="stub", model="stub", request_type="verify_finding",
            latency_ms=1,
        )


@pytest.fixture
def stub(monkeypatch):
    provider = _StubProvider()
    for name in ("classify_clause", "assess_clause", "verify_finding"):
        monkeypatch.setattr(services.ai_service, name, getattr(provider, name))
    return provider


@pytest.fixture
def uploader(settings):
    settings.DEBUG = True
    call_command("seed_reference", verbosity=0)
    call_command("seed_demo_standards", verbosity=0)
    return User.objects.create_user(
        email="pipeline@aji.test", password="test-password-1234", role="user"
    )


@pytest.fixture
def analysed(uploader, stub):
    contract, source_file = create_contract(
        actor=uploader,
        uploaded_file=SimpleUploadedFile(
            "agreement.pdf", _build_pdf(), "application/pdf"
        ),
        project_name="Coastal District Masterplan",
        contract_title="Consultancy Services Agreement",
        client_name="NEOM Development Company",
        country="SA",
        entity=Entity.objects.get(code="aji-sa"),
        contract_type=ContractType.objects.get(code="arch"),
        contract_value=12400000, currency="SAR", language="en",
    )
    run_extraction(source_file)
    document = contract.documents.latest("created_at")
    run = services.start_analysis(contract, document=document, actor=uploader)
    outcome = services.run_analysis(run)
    run.refresh_from_db()
    contract.refresh_from_db()
    return contract, document, run, outcome


class TestExtraction:
    def test_the_pdf_is_read_as_native_text_not_ocr(self, analysed):
        _, document, _, _ = analysed
        assert document.page_count == 1
        assert document.requires_ocr is False
        assert document.native_text_coverage > 0.5

    def test_the_original_wording_is_stored_verbatim(self, analysed):
        """Blocks keep the document's own text, line breaks included.

        A PDF wraps wherever the layout says, so a figure can arrive as
        "SAR\\n18,000,000". Comparing on normalised whitespace is what a reader
        means by verbatim; the stored text itself is untouched.
        """
        _, document, _, _ = analysed
        raw = " ".join(
            b.original_source_text for b in document.pages.first().blocks.all()
        )
        text = " ".join(raw.split())
        assert "15% of" in text
        assert "SAR 18,000,000" in text

    def test_the_language_is_detected_from_the_text(self, analysed):
        """Nothing on the native path used to set a language, so every native
        PDF fell back to Arabic prompts - including English contracts."""
        _, document, _, _ = analysed
        assert document.detected_language == "en"
        assert document.pages.first().language == "en"
        assert all(
            b.language == "en" for b in document.pages.first().blocks.all()
            if len(b.original_source_text) > 40
        )

    def test_every_block_carries_geometry_for_the_source_viewer(self, analysed):
        _, document, _, _ = analysed
        blocks = list(document.pages.first().blocks.all())
        assert blocks
        for block in blocks:
            assert block.bbox_x1 > block.bbox_x0
            assert block.bbox_y1 > block.bbox_y0


class TestAnalysis:
    def test_the_run_completes_cleanly(self, analysed):
        _, _, run, outcome = analysed
        assert run.status == AnalysisStatus.SUCCEEDED
        assert outcome.ai_failures == 0
        assert outcome.unclassified_clauses == 0

    def test_clauses_were_detected_and_assessed(self, analysed):
        _, _, _, outcome = analysed
        assert outcome.clauses_detected >= len(CLAUSES)
        assert outcome.clauses_assessed >= 4

    def test_the_rules_engine_decided_the_arithmetic_itself(self, analysed):
        """15% against a 10% preferred / 15% fallback standard is PARTIAL, and
        that verdict comes from Python, not the model."""
        _, _, run, _ = analysed
        finding = run.findings.get(category__code="delayLiq")
        assert finding.system_result == ComplianceResult.PARTIAL
        assert "15" in finding.system_rule_detail["statement"]
        assert finding.system_rule_detail["expected"] == "10"
        assert finding.system_rule_detail["fallback"] == "15"

    def test_a_value_below_the_fallback_is_a_breach(self, analysed):
        _, _, run, _ = analysed
        finding = run.findings.get(category__code="insurance")
        assert finding.system_result == ComplianceResult.NON_COMPLIANT

    def test_agreement_between_rules_and_model_verifies(self, analysed):
        _, _, run, _ = analysed
        finding = run.findings.get(category__code="delayLiq")
        assert finding.ai_result == ComplianceResult.PARTIAL
        assert finding.verification_status == VerificationOutcome.VERIFIED

    def test_findings_quote_the_document_not_the_model(self, analysed):
        _, _, run, _ = analysed
        finding = run.findings.get(category__code="delayLiq")
        assert "15%" in finding.original_wording

    def test_absence_is_confirmed_because_every_clause_was_read(self, analysed):
        _, _, run, _ = analysed
        missing = MissingClause.objects.filter(analysis=run)
        assert missing.exists()
        assert not missing.filter(absence_confirmed=False).exists()

    def test_the_demo_standards_are_declared(self, analysed):
        _, _, _, outcome = analysed
        assert "uses_demo_fixture_standards" in outcome.notes


class TestRollUp:
    def test_the_contract_is_routed_to_a_human_not_auto_approved(self, analysed):
        contract, _, _, _ = analysed
        assert contract.status != "approved"

    def test_the_analysis_is_attached_to_the_contract(self, analysed):
        contract, _, run, _ = analysed
        assert contract.current_analysis_id == run.pk

    def test_the_risk_level_is_set_from_findings(self, analysed):
        _, _, run, _ = analysed
        assert run.overall_risk
        assert run.high_count + run.medium_count + run.low_count == run.findings.count()


class TestAConfidentlyCompliantClauseIsNotAFinding:
    """No rule can check the disputes clause; the model, at 0.95, finds it
    compliant. It was stored anyway -- a medium risk titled "no deviation"."""

    def test_no_finding_for_it(self, analysed):
        _, _, run, _ = analysed
        assert not run.findings.filter(original_wording__icontains="arbitration").exists()

    def test_an_unsure_compliant_reading_still_goes_to_a_human(self, uploader, stub, monkeypatch):
        real = stub.assess_clause

        def unsure(**kwargs):
            call = real(**kwargs)
            if "arbitration" in kwargs["clause_text"].lower():
                call.parsed.confidence = 0.5
            return call

        monkeypatch.setattr(services.ai_service, "assess_clause", unsure)
        contract, source_file = create_contract(
            actor=uploader,
            uploaded_file=SimpleUploadedFile("agreement.pdf", _build_pdf(), "application/pdf"),
            project_name="Coastal District Masterplan", contract_title="Consultancy Services Agreement",
            client_name="NEOM Development Company", country="SA",
            entity=Entity.objects.get(code="aji-sa"), contract_type=ContractType.objects.get(code="arch"),
            contract_value=12400000, currency="SAR", language="en",
        )
        run_extraction(source_file)
        run = services.start_analysis(contract, document=contract.documents.latest("created_at"), actor=uploader)
        services.run_analysis(run)
        assert run.findings.filter(original_wording__icontains="arbitration").exists()
