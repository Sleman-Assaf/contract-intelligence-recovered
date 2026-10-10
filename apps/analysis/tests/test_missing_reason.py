"""Why a clause was expected, in the reader's language.

`MissingClause.reason_expected` is written once, in English, when the analysis
runs -- "AJi standard late_payment (country) v1 expects this clause." -- and
was then shown verbatim on the analysis page and in the report, so the Arabic
report carried an English sentence in the middle of every missing-clause row.
The standard it describes is still attached to the row, so the sentence is now
rebuilt from it at render time, in whichever language the page is in.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.analysis.models import MissingClause
from apps.standards.models import ClauseCategory, Standard, StandardVersion

pytestmark = pytest.mark.django_db

STORED = "AJi standard late_payment (country) v1 expects this clause."


@pytest.fixture
def missing(run_with_finding, contract):
    category = ClauseCategory.objects.create(
        code="late_payment", name="Late payment", name_ar="التأخر في الدفع")
    standard = Standard.objects.create(
        category=category, scope="country", country="SA", title="Late payment interest")
    version = StandardVersion.objects.create(
        standard=standard, version=1, preferred_position="Interest accrues from day 30.")
    return MissingClause.objects.create(
        analysis=run_with_finding, contract=contract, standard_version=version,
        category=category, title="Late payment interest",
        reason_expected=STORED, absence_confirmed=False,
    )


def page(client, reviewer, run, lang):
    reviewer.preferred_language = lang
    reviewer.save(update_fields=["preferred_language"])
    client.force_login(reviewer)
    return client.get(reverse("analysis:detail", args=[run.pk]) + "?tab=missing").content.decode()


class TestOnTheAnalysisPage:
    def test_arabic(self, client, reviewer, run_with_finding, missing):
        body = page(client, reviewer, run_with_finding, "ar")
        assert "يتوقّع هذا البند" in body
        assert "التأخر في الدفع" in body          # the category, in Arabic
        assert STORED not in body
        # The caveat rides along, translated, when absence is not certain.
        assert "لم يمكن تصنيف بعض بنود هذا العقد" in body

    def test_english(self, client, reviewer, run_with_finding, missing):
        body = page(client, reviewer, run_with_finding, "en")
        assert "expects this clause" in body
        assert "Country Override" in body       # the scope, by its label
        assert "could not be classified" in body


class TestTheReport:
    def test_arabic_report_carries_no_english_reason(
            self, client, reviewer, run_with_finding, missing, contract):
        reviewer.preferred_language = "ar"
        reviewer.save(update_fields=["preferred_language"])
        client.force_login(reviewer)
        body = client.get(reverse("reports:index") + f"?contract={contract.pk}").content.decode()
        assert "يتوقّع هذا البند" in body
        assert STORED not in body


def test_a_row_whose_standard_is_gone_falls_back_to_the_stored_text(
        client, reviewer, run_with_finding, contract):
    """Old rows, or one whose standard version was later removed: the stored
    sentence is the only record of why, so it is shown rather than nothing."""
    MissingClause.objects.create(
        analysis=run_with_finding, contract=contract, standard_version=None,
        title="Orphaned", reason_expected="Kept for the record.")
    body = page(client, reviewer, run_with_finding, "ar")
    assert "Kept for the record." in body


class TestMentionedInPassing:
    """A topic that is only a secondary topic of some clause (liability inside
    "General provisions") is not reported as plainly missing."""

    def _record(self, run, contract, alternatives):
        from types import SimpleNamespace

        from apps.analysis import services
        from apps.analysis.models import Clause
        from apps.documents.models import Document, SourceFile

        source = SourceFile.objects.create(
            contract=contract, original_filename="a.pdf", stored_path="test/a.pdf",
            content_type="application/pdf", size_bytes=1, sha256="1" * 64)
        run.document = Document.objects.create(contract=contract, source_file=source, page_count=1)
        run.save(update_fields=["document"])
        liability = ClauseCategory.objects.create(code="liability", name="Liability")
        general = ClauseCategory.objects.create(code="obligations", name="Obligations")
        Clause.objects.create(contract=contract, document=run.document, clause_number="22",
                              heading="General provisions", text="...", ordinal=1,
                              primary_category=general, alternative_categories=alternatives)
        standard = Standard.objects.create(category=liability, scope="global", title="Liability cap")
        version = StandardVersion.objects.create(standard=standard, version=1, impact=4)
        resolution = SimpleNamespace(resolved={"liability": SimpleNamespace(
            standard=standard, version=version, scope="global")})
        services._record_missing_clauses(run, resolution)
        return MissingClause.objects.get(analysis=run, category=liability)

    def test_mentioned_topic_is_unconfirmed_and_names_the_clause(self, run_with_finding, contract):
        row = self._record(run_with_finding, contract, ["liability"])
        assert row.mentioned_in == ["22"]
        assert not row.absence_confirmed
        assert "clause(s) 22 also touch on" in row.reason_expected

    def test_a_topic_mentioned_nowhere_is_still_missing(self, run_with_finding, contract):
        row = self._record(run_with_finding, contract, ["ip"])
        assert row.mentioned_in == []
        assert row.absence_confirmed

    def test_the_page_says_where_it_is_mentioned(self, client, reviewer, run_with_finding, contract):
        self._record(run_with_finding, contract, ["liability"])
        body = page(client, reviewer, run_with_finding, "en")
        assert "No clause of its own, but clause(s) 22 also touch on this topic" in body
