"""Only what can be judged is assessed, and the most serious goes first.

A table row is part of the clause it sits in; an annex is its own section;
a clause with no judgeable term (severability, signatures) is classified but
not assessed; clauses under the highest-impact standards are assessed first,
so the progress card can show the findings that matter while the rest run.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.ai.prompts import NO_CATEGORY
from apps.ai.schemas import ClauseClassification
from apps.analysis import services
from apps.analysis.clause_detection import TextUnit, group_into_clauses


def _group(*texts):
    return group_into_clauses(TextUnit(id=i, text=t, page_number=1) for i, t in enumerate(texts))


class TestTableRowsStayInTheirClause:
    def test_a_payment_table_is_part_of_its_clause(self):
        clauses = _group("6.1 The Contract Price is JOD 1,240,000.",
                         "6.2 The Contract Price shall be paid as follows:",
                         "1\nAdvance payment upon signature", "10%\n124,000",
                         "2\nMonthly progress payments", "30%\n372,000",
                         "6.3 The Consultant shall submit invoices monthly.")
        assert [c.clause_number for c in clauses] == ["6.1", "6.2", "6.3"]
        assert "Advance payment" in clauses[1].text and clauses[1].block_ids == [1, 2, 3, 4, 5]

    def test_a_bare_number_that_moves_the_numbering_on_is_a_clause(self):
        clauses = _group("16.3 The Consultant shall comply.", "17 Notices shall be in writing.",
                         "17.1 Notices go to the addresses above.")
        assert [c.clause_number for c in clauses] == ["16.3", "17", "17.1"]

    def test_contracts_numbered_with_bare_numbers_still_split(self):
        clauses = _group("1 Definitions apply.", "2 The scope is set out below.", "3 Payment is monthly.")
        assert [c.clause_number for c in clauses] == ["1", "2", "3"]


class TestAnAnnexIsItsOwnSection:
    def test_the_annex_does_not_run_on_inside_the_last_article(self):
        clauses = _group("20 IN WITNESS WHEREOF the Parties have signed.",
                         "Annex A – Scope of Services", "A.1 Stage 1 – Concept Design",
                         "Annex B – Design Programme", "1 – Concept Design\n20 Oct 2026",
                         "2 – Schematic Design\n1 Dec 2026")
        assert [c.clause_number for c in clauses] == ["20", "Annex A", "Annex B"]
        assert "Concept Design" not in clauses[0].text
        annex_b = clauses[2]
        assert annex_b.is_schedule and annex_b.heading == "Annex B – Design Programme"
        assert "Schematic Design" in annex_b.text

    def test_a_sentence_mentioning_an_annex_is_not_a_heading(self):
        clauses = _group("7.1 The Consultant shall provide key personnel.",
                         "Annex D lists the key personnel and their roles.")
        assert [c.clause_number for c in clauses] == ["7.1"]

    def test_numbered_clauses_inside_an_annex_are_named_after_it(self):
        clauses = _group("Schedule 2 – Particular Conditions", "1.1 The cap is 100%.", "1.2 Delay damages 5%.")
        assert [c.clause_number for c in clauses] == ["Schedule 2", "Schedule 2 1.1", "Schedule 2 1.2"]


def _parsed(primary, confidence, alternatives=()):
    return ClauseClassification(primary=primary, confidence=confidence, alternatives=list(alternatives))


class TestAClauseWithNoTerm:
    CATEGORIES = {"payment": object(), "termination": object()}

    def test_a_sure_none_is_not_assessed(self):
        assert services._primary_of(_parsed(NO_CATEGORY, 0.95), self.CATEGORIES) == NO_CATEGORY

    def test_an_unsure_none_is_assessed_under_its_best_alternative(self):
        parsed = _parsed(NO_CATEGORY, 0.6, ["warranty", "termination"])
        assert services._primary_of(parsed, self.CATEGORIES) == "termination"

    def test_a_real_category_is_left_alone(self):
        assert services._primary_of(_parsed("payment", 0.5), self.CATEGORIES) == "payment"


class TestTheMostSeriousFirst:
    def test_clauses_are_assessed_by_standard_impact_then_reading_order(self):
        clause = lambda pk: SimpleNamespace(pk=pk)             # noqa: E731
        stored = [(clause(n), None) for n in (1, 2, 3, 4, 5)]
        batched = {1: _parsed("scope", .9), 2: _parsed("liability", .9), 3: _parsed("payment", .9),
                   4: _parsed("liability", .9)}               # 5 was not batched
        standard = lambda impact: SimpleNamespace(version=SimpleNamespace(impact=impact))  # noqa: E731
        resolution = SimpleNamespace(resolved={"liability": standard(5), "payment": standard(4),
                                               "scope": standard(2)})
        order = [c.pk for c, _ in services._most_serious_first(stored, batched, resolution)]
        assert order == [2, 4, 3, 1, 5]


@pytest.mark.django_db
class TestTheCardShowsWhatWasFound:
    def test_counts_and_the_most_serious_findings(self, analysed):   # noqa: F811
        from django.utils import translation

        from apps.contracts.progress import found_so_far

        _contract, _document, run, _outcome = analysed
        with translation.override("en"):
            found = found_so_far(run)
        assert found["total"] == run.findings.count() > 0
        assert sum(c["count"] for c in found["counts"]) <= found["total"]
        assert [c["level"] for c in found["counts"]] == ["high", "medium", "low"]
        assert 0 < len(found["top"]) <= 4 and all(f["level"] in {"high", "medium"} for f in found["top"])
        assert all(f["title"] for f in found["top"])

    def test_the_running_card_carries_them(self, client, analysed):   # noqa: F811
        from apps.analysis.models import AnalysisStatus
        from apps.contracts.models import ContractStatus

        contract, _document, run, _outcome = analysed
        run.status = AnalysisStatus.RUNNING
        run.save(update_fields=["status"])
        contract.status = ContractStatus.AI_ANALYSIS
        contract.save(update_fields=["status"])
        client.force_login(contract.uploaded_by)
        from django.urls import reverse

        state = client.get(reverse("contracts:progress", args=[contract.pk])).json()
        assert state["found"]["total"] > 0 and state["live"] == reverse("analysis:detail", args=[run.pk])
        # the analysis page opens while the run is still going, card included
        response = client.get(state["live"])
        page = response.content.decode()
        assert response.status_code == 200
        assert "data-progress-found" in page and "data-found-list" in page


from apps.analysis.tests.test_pipeline_end_to_end import analysed, stub, uploader  # noqa: E402,F401  (fixtures)
