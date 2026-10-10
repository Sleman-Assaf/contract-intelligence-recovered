"""Negotiation rounds, and the diff between them.

The dangerous word here is "resolved". A reviewer reads it as *the
counterparty conceded this point* and stops arguing it. So the one thing these
tests guard hardest is that a point can only be called resolved when both
rounds were actually analysed — a category missing from a degraded run looks
identical to one that was fixed.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.accounts.models import Entity, User
from apps.analysis import rounds
from apps.analysis.models import AnalysisRun, AnalysisStatus, Finding
from apps.contracts.models import Contract, ContractType
from apps.documents.models import Document, SourceFile
from apps.standards.models import ClauseCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded(settings):
    settings.DEBUG = True
    call_command("seed_reference", verbosity=0)


@pytest.fixture
def contract(seeded):
    user = User.objects.create_user(
        email="rounds@aji.test", password="test-password-1234", role="user"
    )
    return Contract.objects.create(
        project_name="Coastal District Masterplan",
        contract_title="Consultancy Agreement",
        client_name="NEOM Development Company",
        country="SA",
        entity=Entity.objects.first(),
        contract_type=ContractType.objects.first(),
        uploaded_by=user,
    )


def make_round(contract, number, *, status=AnalysisStatus.SUCCEEDED, supersedes=None):
    source = SourceFile.objects.create(
        contract=contract, original_filename=f"draft-{number}.pdf",
        stored_path=f"test/draft-{number}.pdf", content_type="application/pdf",
        size_bytes=1024, sha256=str(number) * 64,
        round_number=number, supersedes=supersedes,
    )
    document = Document.objects.create(
        contract=contract, source_file=source, page_count=1,
        detected_language="en", overall_confidence=99.0,
    )
    run = AnalysisRun.objects.create(
        contract=contract, document=document, version=number, status=status
    )
    return source, document, run


def add_finding(run, code, *, level="high", score=15, wording="original text"):
    return Finding.objects.create(
        analysis=run, contract=run.contract,
        category=ClauseCategory.objects.get(code=code),
        title=f"{code} finding", risk_level=level, risk_score=score,
        original_wording=wording,
    )


class TestRoundNumbering:
    def test_rounds_are_listed_oldest_first(self, contract):
        make_round(contract, 1)
        make_round(contract, 2)
        listed = rounds.rounds_for(contract)
        assert [r["source_file"].round_number for r in listed] == [1, 2]

    def test_a_round_number_is_unique_per_contract(self, contract):
        from django.db import IntegrityError, transaction

        make_round(contract, 1)
        with pytest.raises(IntegrityError), transaction.atomic():
            SourceFile.objects.create(
                contract=contract, original_filename="dup.pdf",
                stored_path="test/dup.pdf", content_type="application/pdf",
                size_bytes=10, sha256="f" * 64, round_number=1,
            )

    def test_an_earlier_round_survives_a_later_one(self, contract):
        """The previous draft is the evidence for what changed; it is never
        overwritten."""
        first, _, first_run = make_round(contract, 1)
        add_finding(first_run, "delayLiq")
        make_round(contract, 2, supersedes=first)

        first.refresh_from_db()
        assert first.pk is not None
        assert first_run.findings.count() == 1


class TestComparison:
    def test_a_point_dropped_between_rounds_is_resolved(self, contract):
        _, _, before = make_round(contract, 1)
        add_finding(before, "delayLiq")
        add_finding(before, "insurance")
        _, _, after = make_round(contract, 2)
        add_finding(after, "insurance")

        diff = rounds.compare_runs(before, after)
        assert [c.code for c in diff.resolved] == ["delayLiq"]

    def test_a_new_point_is_introduced(self, contract):
        _, _, before = make_round(contract, 1)
        add_finding(before, "insurance")
        _, _, after = make_round(contract, 2)
        add_finding(after, "insurance")
        add_finding(after, "ip")

        diff = rounds.compare_runs(before, after)
        assert [c.code for c in diff.introduced] == ["ip"]

    def test_a_point_present_in_both_is_carried(self, contract):
        _, _, before = make_round(contract, 1)
        add_finding(before, "insurance")
        _, _, after = make_round(contract, 2)
        add_finding(after, "insurance")

        diff = rounds.compare_runs(before, after)
        assert [c.code for c in diff.carried] == ["insurance"]

    def test_a_downgraded_risk_counts_as_improved(self, contract):
        _, _, before = make_round(contract, 1)
        add_finding(before, "delayLiq", level="high", score=15)
        _, _, after = make_round(contract, 2)
        add_finding(after, "delayLiq", level="medium", score=8)

        diff = rounds.compare_runs(before, after)
        assert [c.code for c in diff.improved] == ["delayLiq"]
        assert diff.worsened == []

    def test_an_escalated_risk_counts_as_worsened(self, contract):
        _, _, before = make_round(contract, 1)
        add_finding(before, "delayLiq", level="low", score=3)
        _, _, after = make_round(contract, 2)
        add_finding(after, "delayLiq", level="high", score=15)

        diff = rounds.compare_runs(before, after)
        assert [c.code for c in diff.worsened] == ["delayLiq"]

    def test_rewrapped_text_is_not_reported_as_a_change(self, contract):
        """A PDF re-export rewraps lines; nobody negotiated that."""
        _, _, before = make_round(contract, 1)
        add_finding(before, "insurance", wording="capped at\n15% of the value")
        _, _, after = make_round(contract, 2)
        add_finding(after, "insurance", wording="capped at 15%  of the value")

        change = rounds.compare_runs(before, after).carried[0]
        assert not change.wording_changed

    def test_genuinely_edited_text_is_reported(self, contract):
        _, _, before = make_round(contract, 1)
        add_finding(before, "insurance", wording="capped at 15% of the value")
        _, _, after = make_round(contract, 2)
        add_finding(after, "insurance", wording="capped at 10% of the value")

        change = rounds.compare_runs(before, after).carried[0]
        assert change.wording_changed

    def test_the_worst_point_leads(self, contract):
        _, _, before = make_round(contract, 1)
        _, _, after = make_round(contract, 2)
        add_finding(after, "insurance", level="low", score=2)
        add_finding(after, "delayLiq", level="high", score=20)

        diff = rounds.compare_runs(before, after)
        assert diff.changes[0].code == "delayLiq"


class TestReliability:
    """The guard on the word "resolved"."""

    def test_two_clean_runs_compare_reliably(self, contract):
        _, _, before = make_round(contract, 1)
        _, _, after = make_round(contract, 2)
        assert rounds.compare_runs(before, after).is_reliable

    def test_a_degraded_current_run_makes_the_diff_unreliable(self, contract):
        _, _, before = make_round(contract, 1)
        add_finding(before, "delayLiq")
        _, _, after = make_round(contract, 2, status=AnalysisStatus.DEGRADED)

        diff = rounds.compare_runs(before, after)
        # The point still reads as resolved -- which is exactly why the flag
        # has to travel with it.
        assert [c.code for c in diff.resolved] == ["delayLiq"]
        assert not diff.is_reliable
        assert "current" in diff.unreliable_because

    def test_a_degraded_previous_run_also_makes_it_unreliable(self, contract):
        _, _, before = make_round(contract, 1, status=AnalysisStatus.DEGRADED)
        _, _, after = make_round(contract, 2)

        diff = rounds.compare_runs(before, after)
        assert not diff.is_reliable
        assert "previous" in diff.unreliable_because


class TestLatestComparison:
    def test_one_round_has_nothing_to_compare(self, contract):
        make_round(contract, 1)
        assert rounds.latest_comparison(contract) is None

    def test_the_two_most_recent_analysed_rounds_are_used(self, contract):
        make_round(contract, 1)
        _, _, second = make_round(contract, 2)
        _, _, third = make_round(contract, 3)

        diff = rounds.latest_comparison(contract)
        assert diff.previous == second
        assert diff.current == third

    def test_an_unanalysed_round_is_skipped(self, contract):
        """A draft uploaded but not yet analysed cannot be compared against."""
        _, _, first = make_round(contract, 1)
        _, _, second = make_round(contract, 2)
        SourceFile.objects.create(
            contract=contract, original_filename="draft-3.pdf",
            stored_path="test/draft-3.pdf", content_type="application/pdf",
            size_bytes=10, sha256="3" * 64, round_number=3,
        )
        diff = rounds.latest_comparison(contract)
        assert diff.previous == first
        assert diff.current == second


class TestNewRoundService:
    def test_uploading_a_revision_increments_the_round(self, contract):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.contracts.services import start_new_round

        uploader = contract.uploaded_by
        make_round(contract, 1)

        source = start_new_round(
            contract=contract,
            uploaded_file=SimpleUploadedFile("rev.pdf", b"%PDF-1.4 revised", "application/pdf"),
            actor=uploader,
            note="Counterparty returned with edits to clause 3",
        )
        assert source.round_number == 2
        assert source.supersedes.round_number == 1
        assert source.round_note.startswith("Counterparty returned")

    def test_a_new_round_reopens_the_contract(self, contract):
        """A revised draft invalidates the previous verdict."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.contracts.models import ContractStatus
        from apps.contracts.services import start_new_round

        contract.status = ContractStatus.PENDING_CONTRACT_REVIEW
        contract.save(update_fields=["status"])

        start_new_round(
            contract=contract,
            uploaded_file=SimpleUploadedFile("rev.pdf", b"%PDF-1.4", "application/pdf"),
            actor=contract.uploaded_by,
        )
        contract.refresh_from_db()
        assert contract.status == ContractStatus.UPLOADED
