"""Fewer, truer findings (tidy.py): one point once, no risk without evidence."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.analysis.models import Finding
from apps.analysis.tidy import recount, tidy_findings
from apps.standards.models import ClauseCategory

pytestmark = pytest.mark.django_db


def add(run, category, clause, title, *, level="medium", score=9, ai="partial", system="not_applicable",
        confidence=0.7):
    return Finding.objects.create(
        analysis=run, contract=run.contract, category=category, risk_level=level, impact=3, probability=3,
        risk_score=score, title=title, deviation=title, clause_number=clause, page_number=1,
        system_result=system, ai_result=ai, verification_status="needs_human_review", ai_confidence=confidence)


@pytest.fixture
def obligations():
    return ClauseCategory.objects.create(code="obligations", name="Obligations")


def test_the_same_point_in_many_clauses_is_one_finding(run_with_finding, obligations):
    run = run_with_finding
    add(run, obligations, "20.2", "Absence of any provision regarding the provision of information by the Client",
        score=12)
    add(run, obligations, "21.4", "Absence of a provision regarding the provision of information by the Client")
    add(run, obligations, "21.5", "Absence of provisions regarding the provision of information by the Client")
    add(run, obligations, "8.1", "The consultant's duty of care is lower than reasonable skill and care")
    result = tidy_findings(run)
    assert result["merged"] == 2
    kept = Finding.objects.get(analysis=run, clause_number="20.2")
    assert kept.also_in == ["21.4", "21.5"]
    assert Finding.objects.filter(analysis=run, clause_number="8.1").exists()


def test_no_verdict_and_no_rule_is_not_a_medium_risk(run_with_finding, obligations):
    run = run_with_finding
    add(run, obligations, "30.2", "Clause text not found", ai="unknown")
    add(run, obligations, "9.9", "Assessment failed", ai="unknown", confidence=None)     # the model never answered
    tidy_findings(run)
    recount(run)
    assert Finding.objects.get(clause_number="30.2").risk_level == "unknown"
    assert Finding.objects.get(clause_number="9.9").risk_level == "medium"
    run.refresh_from_db()
    assert run.medium_count == 1 and run.high_count == 1


def test_a_different_topic_is_never_merged(run_with_finding, obligations):
    run = run_with_finding
    other = ClauseCategory.objects.create(code="financial", name="Financial")
    add(run, obligations, "20.2", "No late payment interest")
    add(run, other, "6.1", "No late payment interest")
    assert tidy_findings(run)["merged"] == 0


def test_the_page_lists_the_other_clauses(client, reviewer, run_with_finding, obligations):
    run = run_with_finding
    add(run, obligations, "20.2", "Absence of any provision regarding information by the Client", score=12)
    add(run, obligations, "21.4", "Absence of a provision regarding information by the Client")
    tidy_findings(run)
    reviewer.preferred_language = "en"
    reviewer.save(update_fields=["preferred_language"])
    client.force_login(reviewer)
    body = client.get(reverse("analysis:detail", args=[run.pk]) + "?tab=findings").content.decode()
    assert "same point also in" in body and "21.4" in body


def test_a_topic_no_clause_covers_keeps_one_finding(run_with_finding, obligations):
    """Association agreement: its only "termination" clause was about returning
    equipment; "no termination provisions" must survive the tidy-up."""
    from apps.analysis.models import Clause
    from apps.documents.models import Document, SourceFile

    run = run_with_finding
    source = SourceFile.objects.create(contract=run.contract, original_filename="a.pdf", stored_path="t/a.pdf",
                                       content_type="application/pdf", size_bytes=1, sha256="2" * 64)
    run.document = Document.objects.create(contract=run.contract, source_file=source, page_count=1)
    run.save(update_fields=["document"])
    termination = ClauseCategory.objects.create(code="termination", name="Termination")
    clause = Clause.objects.create(contract=run.contract, document=run.document, clause_number="11.4",
                                   text="On termination the equipment returns to the Employer.", ordinal=1,
                                   primary_category=termination)
    finding = add(run, termination, "11.4", "Absence of provisions on member withdrawal or default",
                  ai="unknown", confidence=0.95)
    finding.clause = clause
    finding.save()
    tidy_findings(run)
    finding.refresh_from_db()
    assert finding.risk_level == "medium" and finding.system_rule_detail.get("covers_none")


def test_one_uncovered_clause_among_covered_ones_stays_undetermined(run_with_finding, obligations):
    from apps.analysis.models import Clause
    from apps.documents.models import Document, SourceFile

    run = run_with_finding
    source = SourceFile.objects.create(contract=run.contract, original_filename="b.pdf", stored_path="t/b.pdf",
                                       content_type="application/pdf", size_bytes=1, sha256="3" * 64)
    run.document = Document.objects.create(contract=run.contract, source_file=source, page_count=1)
    run.save(update_fields=["document"])
    for number in ("8.1", "8.2"):
        Clause.objects.create(contract=run.contract, document=run.document, clause_number=number, text="...",
                              ordinal=int(number[-1]), primary_category=obligations)
    noise = add(run, obligations, "8.2", "Clause text not found", ai="unknown", confidence=0.95)
    tidy_findings(run)
    noise.refresh_from_db()
    assert noise.risk_level == "unknown"
