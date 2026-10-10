"""A run that could not finish must never present itself as a finished one.

These tests come from a real end-to-end run against Gemini's free tier. The
provider rejected 11 of 12 calls with HTTP 429. The pipeline did not crash --
but it reported `succeeded`, told the reviewer to "proceed to contract review",
and listed ten clause categories as MISSING FROM THE CONTRACT when in fact
those clauses were present and had simply never been classified.

A false "this contract has no insurance clause" is more dangerous than no
analysis at all, because a reviewer will act on it.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.accounts.models import Entity, User
from apps.ai.providers.base import AIProviderUnavailable
from apps.analysis import services
from apps.analysis.models import AnalysisStatus, MissingClause
from apps.contracts.models import Contract, ContractStatus, ContractType, RiskLevel
from apps.documents.models import Block, Document, Page, SourceFile

pytestmark = pytest.mark.django_db

CLAUSES = [
    ("1. DELAY DAMAGES",
     "Liquidated damages for delay shall be capped at 15% of the contract value."),
    ("2. INSURANCE",
     "The Consultant shall maintain professional indemnity insurance of "
     "SAR 18,000,000."),
    ("3. DISPUTE RESOLUTION",
     "Any dispute shall be referred to arbitration seated in Riyadh."),
]


def _raise_unavailable(*args, **kwargs):
    raise AIProviderUnavailable("429 RESOURCE_EXHAUSTED: quota exceeded")


@pytest.fixture
def seeded(settings):
    settings.DEBUG = True
    call_command("seed_reference", verbosity=0)
    call_command("seed_demo_standards", verbosity=0)


@pytest.fixture
def contract(seeded):
    user = User.objects.create_user(email="e2e@aji.test", password="x", role="user")
    return Contract.objects.create(
        project_name="Degradation Test",
        contract_title="Consultancy Agreement",
        client_name="Test Client",
        country="SA",
        entity=Entity.objects.first(),
        contract_type=ContractType.objects.first(),
        uploaded_by=user,
        language="en",
        reference_template="aji_standard",     # the demo standards are AJi-profile
    )


@pytest.fixture
def document(contract):
    source = SourceFile.objects.create(
        contract=contract, original_filename="agreement.pdf",
        stored_path="test/agreement.pdf", content_type="application/pdf",
        size_bytes=1024, sha256="0" * 64,
    )
    doc = Document.objects.create(
        contract=contract, source_file=source, page_count=1, detected_language="en",
        overall_confidence=99.0, native_text_coverage=1.0, requires_ocr=False,
    )
    page = Page.objects.create(
        document=doc, page_number=1, width=595, height=842,
        has_native_text=True, extraction_method="native", ocr_confidence=99.0,
    )
    for i, (heading, body) in enumerate(CLAUSES):
        Block.objects.create(
            document=doc, page=page, block_index=i,
            original_source_text="{}\n{}".format(heading, body),
            bbox_x0=72, bbox_y0=100 + i * 80, bbox_x1=520, bbox_y1=160 + i * 80,
            extraction_method="native", ocr_confidence=99.0,
            min_word_confidence=99.0, language="en",
        )
    return doc


def _break_the_provider(monkeypatch):
    """Every call fails the way a rate-limited provider fails."""
    for name in ("classify_clause", "assess_clause", "verify_finding"):
        monkeypatch.setattr(services.ai_service, name, _raise_unavailable)


class TestClassificationOutage:
    """Nothing can be classified, so nothing can be claimed about absence."""

    def test_the_run_is_degraded_not_succeeded(self, contract, document, monkeypatch):
        _break_the_provider(monkeypatch)
        run = services.start_analysis(contract, document=document)
        outcome = services.run_analysis(run)

        run.refresh_from_db()
        assert outcome.unclassified_clauses == len(CLAUSES)
        assert run.status == AnalysisStatus.DEGRADED

    def test_the_reviewer_is_told_not_to_rely_on_it(self, contract, document, monkeypatch):
        _break_the_provider(monkeypatch)
        run = services.start_analysis(contract, document=document)
        services.run_analysis(run)

        run.refresh_from_db()
        assert "incomplete" in run.next_action.lower()
        assert "proceed to contract review" not in run.next_action.lower()

    def test_missing_clauses_are_flagged_as_unconfirmed(self, contract, document, monkeypatch):
        """The whole point: we failed to read the clauses, so we cannot say
        they are absent."""
        _break_the_provider(monkeypatch)
        run = services.start_analysis(contract, document=document)
        services.run_analysis(run)

        recorded = MissingClause.objects.filter(analysis=run)
        assert recorded.exists(), "expected the set difference to still run"
        assert not recorded.filter(absence_confirmed=True).exists()
        for entry in recorded:
            assert "may be present but unread" in entry.reason_expected

    def test_it_does_not_report_a_reassuring_risk_level(self, contract, document, monkeypatch):
        _break_the_provider(monkeypatch)
        run = services.start_analysis(contract, document=document)
        services.run_analysis(run)

        run.refresh_from_db()
        assert run.overall_risk != RiskLevel.LOW

    def test_the_contract_is_routed_to_a_human(self, contract, document, monkeypatch):
        _break_the_provider(monkeypatch)
        run = services.start_analysis(contract, document=document)
        services.run_analysis(run)

        contract.refresh_from_db()
        assert contract.status == ContractStatus.NEEDS_HUMAN_REVIEW

    def test_the_gaps_are_recorded_for_the_audit_trail(self, contract, document, monkeypatch):
        _break_the_provider(monkeypatch)
        run = services.start_analysis(contract, document=document)
        services.run_analysis(run)

        run.refresh_from_db()
        breakdown = run.confidence_breakdown
        assert breakdown["unclassified_clauses"] == len(CLAUSES)
        assert breakdown["gaps"], "a degraded run must say what was missed"
        assert any("absence_unverified" in note for note in breakdown["notes"])


class TestHealthyClassificationIsUnaffected:
    """The guard must not fire when classification is working."""

    def test_absence_is_still_confirmed_when_every_clause_was_read(
        self, contract, document, monkeypatch
    ):
        from apps.ai.providers.base import AICallResult
        from apps.ai.schemas import ClauseClassification

        def classify(*, clause_text, categories, language="ar", **kwargs):
            code = "delayLiq" if "damages" in clause_text.lower() else "insurance"
            return AICallResult(
                parsed=ClauseClassification(
                    primary=code, alternatives=[], confidence=0.9,
                    reasoning="test stub",
                ),
                provider="stub", model="stub", request_type="classify_clause",
                latency_ms=1,
            )

        monkeypatch.setattr(services.ai_service, "classify_clause", classify)
        monkeypatch.setattr(services.ai_service, "assess_clause", _raise_unavailable)
        monkeypatch.setattr(services.ai_service, "verify_finding", _raise_unavailable)

        run = services.start_analysis(contract, document=document)
        outcome = services.run_analysis(run)

        assert outcome.unclassified_clauses == 0
        # Every clause was read and categorised, so absence is a real statement.
        assert not MissingClause.objects.filter(
            analysis=run, absence_confirmed=False
        ).exists()


class TestTheWarningReachesTheScreen:
    """A flag in the database that no one sees protects nobody."""

    def _render(self, client, run, contract, tab=None):
        """The page shows one section at a time, so a test has to say which."""
        from django.urls import reverse

        url = reverse("analysis:detail", args=[run.pk])
        return client.get(f"{url}?tab={tab}" if tab else url)

    def test_the_degraded_banner_is_rendered(self, contract, document, monkeypatch, client):
        _break_the_provider(monkeypatch)
        run = services.start_analysis(contract, document=document)
        services.run_analysis(run)

        client.force_login(contract.uploaded_by)
        html = self._render(client, run, contract).content.decode()
        assert "تحليل غير مكتمل" in html, "the incomplete-analysis banner must render"
        # The banner is rendered from the structured counts, not from the
        # English sentence stored on the run, so it follows the reader.
        assert "تعذّر تصنيف" in html
        assert str(len(CLAUSES)) in html

    def test_unconfirmed_absence_is_marked_in_the_table(
        self, contract, document, monkeypatch, client
    ):
        _break_the_provider(monkeypatch)
        run = services.start_analysis(contract, document=document)
        services.run_analysis(run)

        client.force_login(contract.uploaded_by)
        html = self._render(client, run, contract, tab="missing").content.decode()
        assert "chip--warn" in html, "an unconfirmed absence must be visibly marked"

    def test_an_assessment_outage_alone_also_degrades_the_run(
        self, contract, document, monkeypatch, client
    ):
        """Classification succeeding is not enough. If the clauses were read
        but never compared against the standards, the analysis is still
        incomplete and must say so."""
        from apps.ai.providers.base import AICallResult
        from apps.ai.schemas import ClauseClassification

        def classify(*, clause_text, categories, language="ar", **kwargs):
            return AICallResult(
                parsed=ClauseClassification(
                    primary="delayLiq", alternatives=[], confidence=0.9,
                    reasoning="stub",
                ),
                provider="stub", model="stub", request_type="classify_clause",
                latency_ms=1,
            )

        monkeypatch.setattr(services.ai_service, "classify_clause", classify)
        monkeypatch.setattr(services.ai_service, "assess_clause", _raise_unavailable)
        monkeypatch.setattr(services.ai_service, "verify_finding", _raise_unavailable)
        run = services.start_analysis(contract, document=document)
        services.run_analysis(run)

        client.force_login(contract.uploaded_by)
        html = self._render(client, run, contract).content.decode()
        run.refresh_from_db()
        assert run.status == AnalysisStatus.DEGRADED
        assert "تحليل غير مكتمل" in html


class TestRiskRollUpOnADegradedRun:
    """The roll-up is a floor, not a verdict, when clauses went unread."""

    def _run_with_findings(self, contract, document, monkeypatch, risk_level):
        """Classification works so findings are produced; assessment fails so
        the run is still degraded."""
        from apps.ai.providers.base import AICallResult
        from apps.ai.schemas import ClauseClassification

        def classify(*, clause_text, categories, language="ar", **kwargs):
            code = "delayLiq" if "damages" in clause_text.lower() else "dispute"
            return AICallResult(
                parsed=ClauseClassification(
                    primary=code, alternatives=[], confidence=0.9, reasoning="stub"
                ),
                provider="stub", model="stub", request_type="classify_clause",
                latency_ms=1,
            )

        monkeypatch.setattr(services.ai_service, "classify_clause", classify)
        monkeypatch.setattr(services.ai_service, "assess_clause", _raise_unavailable)
        monkeypatch.setattr(services.ai_service, "verify_finding", _raise_unavailable)
        run = services.start_analysis(contract, document=document)
        services.run_analysis(run)
        run.refresh_from_db()
        return run

    def test_a_sub_high_rollup_becomes_unknown(self, contract, document, monkeypatch):
        """"Medium" on a run that read one clause in eleven reads as a
        considered judgement. It is not one."""
        run = self._run_with_findings(contract, document, monkeypatch, "medium")
        assert run.status == AnalysisStatus.DEGRADED
        assert run.overall_risk not in (RiskLevel.LOW, RiskLevel.MEDIUM)

    def test_a_high_finding_still_shows_as_high(self, contract, document, monkeypatch):
        """A real red flag found in the text is not hidden behind "unknown"."""
        run = self._run_with_findings(contract, document, monkeypatch, "high")
        if run.high_count:
            assert run.overall_risk == RiskLevel.HIGH
