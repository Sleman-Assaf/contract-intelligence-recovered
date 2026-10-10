"""The advisor reads the clauses the question is about, not the first twelve.

Asked about the liability article of a twenty-article contract, it used to
answer "the liability clause is not in the excerpts": only clauses 1 to 12
were ever sent. The excerpts are now chosen for the question -- and a
contract small enough to fit is sent whole.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.ai.providers.base import AICallResult
from apps.ai.schemas import AdvisorAnswer
from apps.ai.services import ai_service
from apps.analysis import advisor_context
from apps.analysis.models import AnalysisRun, AnalysisStatus, Clause, Finding
from apps.documents.models import Document, SourceFile
from apps.standards.models import ClauseCategory

pytestmark = pytest.mark.django_db

#: Twenty articles, in order; the subjects the questions below ask about.
ARTICLES = [
    ("scope", "The Consultant shall perform the design services described in Annex A."),
    ("deliverables", "Drawings and reports shall be issued in PDF and DWG formats."),
    ("acceptance", "The Client shall review each deliverable within twenty-one days."),
    ("schedule", "The Consultant shall complete each stage by the dates in the programme."),
    ("variations", "Variations up to twenty-five per cent shall be performed at no cost."),
    ("financial", "Each approved invoice shall be paid within one hundred and twenty days."),
    ("obligations", "The Client shall provide the site survey within fourteen days."),
    ("assignment", "The Consultant shall not subcontract without the Client's consent."),
    ("ip", "All intellectual property in the drawings vests in the Client on creation."),
    ("confidentiality", "Each Party shall keep the other Party's information confidential."),
    ("insurance", "The Consultant shall hold professional indemnity insurance of SAR 5,000,000."),
    ("delayLiq", "Delay damages of 0.5% per day apply, with no maximum amount."),
    ("suspension", "The Client may suspend the Services for up to 180 days without payment."),
    ("termination", "The Client may terminate for convenience on seven days' notice."),
    ("force_majeure", "Pandemics are not Force Majeure Events."),
    ("governing_law", "This Agreement is governed by the laws of the Kingdom of Saudi Arabia."),
    ("dispute", "Disputes go to the courts of Riyadh; the Parties waive arbitration."),
    ("liability", "The Consultant's liability shall not be limited in amount and includes indirect loss."),
    ("tax", "All amounts are exclusive of VAT."),
    ("data_protection", "Personal data shall be processed under applicable law."),
]


@pytest.fixture
def categories():
    names = {"liability": ("Liability cap and indemnities", "سقف المسؤولية والتعويضات"),
             "dispute": ("Dispute resolution and arbitration", "تسوية المنازعات والتحكيم"),
             "delayLiq": ("Liquidated damages for delay", "تعويضات التأخير"),
             "financial": ("Payment schedule", "جدول الدفعات")}
    return {code: ClauseCategory.objects.get_or_create(
        code=code, defaults={"name": names.get(code, (code.title(), ""))[0],
                             "name_ar": names.get(code, ("", ""))[1]})[0]
            for code, _ in ARTICLES}


def _document(contract):
    source = SourceFile.objects.create(contract=contract, original_filename="c.pdf", stored_path="c.pdf",
                                       content_type="application/pdf", size_bytes=1, sha256="0" * 64)
    return Document.objects.create(source_file=source, contract=contract, page_count=8)


def _run(contract, document, version, categories, *, words=60):
    """One analysis run with its own copy of every clause, as the pipeline stores them."""
    started = timezone.now() + timedelta(minutes=version)
    run = AnalysisRun.objects.create(contract=contract, document=document, version=version,
                                     status=AnalysisStatus.SUCCEEDED, started_at=started)
    clauses = []
    for n, (code, text) in enumerate(ARTICLES, 1):
        body = f"{n}.1 {text} " + " ".join(["Further detail follows."] * (words // 3))
        clause = Clause.objects.create(document=document, contract=contract, clause_number=f"{n}.1",
                                       text=body, ordinal=n, page_number=1 + n // 3,
                                       primary_category=categories[code])
        Clause.objects.filter(pk=clause.pk).update(created_at=started + timedelta(seconds=n))
        clauses.append(clause)
    run.finished_at = started + timedelta(seconds=60)
    run.save(update_fields=["finished_at"])
    contract.current_analysis = run
    contract.save(update_fields=["current_analysis"])
    return run, clauses


def _numbers(excerpts):
    return [e.split("]")[0][len("[Clause "):].split(" p.")[0] for e in excerpts]


@pytest.fixture
def analysed(contract, categories):
    document = _document(contract)
    _run(contract, document, 1, categories)                      # an older run, same document
    run, clauses = _run(contract, document, 2, categories)
    return contract, run, clauses


TIGHT = override_settings(ADVISOR_CONTRACT_BUDGET_CHARS=1500)


class TestTheQuestionPicksTheClauses:
    def test_a_small_contract_is_sent_whole_and_once(self, analysed):
        contract, _run_, _clauses = analysed
        excerpts, _topics = advisor_context.contract_excerpts(contract, "What is the liability cap?")
        assert _numbers(excerpts) == [f"{n}.1" for n in range(1, 21)]   # all twenty, not twelve, no repeats

    @TIGHT
    def test_the_liability_article_beyond_the_twelfth_is_read(self, analysed):
        contract, _run_, _clauses = analysed
        excerpts, topics = advisor_context.contract_excerpts(contract, "ما أهم مخاطر بند المسؤولية؟")
        assert "18.1" in _numbers(excerpts) and "liability" in topics
        assert sum(map(len, excerpts)) <= 1500 + 40 * len(excerpts)

    @TIGHT
    @pytest.mark.parametrize("question, number", [
        ("اشرح المادة 17", "17.1"),
        ("Explain clause 12.1", "12.1"),
        ("هل يوجد تحكيم؟", "17.1"),
        ("ما موقف العقد من غرامات التأخير؟", "12.1"),
        ("When is an invoice paid?", "6.1"),
    ])
    def test_named_or_topical_clauses_are_read(self, analysed, question, number):
        contract, _run_, _clauses = analysed
        assert number in _numbers(advisor_context.contract_excerpts(contract, question)[0])

    @TIGHT
    def test_a_general_question_reads_the_most_serious_findings(self, analysed):
        contract, run, clauses = analysed
        Finding.objects.create(analysis=run, contract=contract, clause=clauses[16], risk_level="high",
                               title="Courts only", category=clauses[16].primary_category)
        excerpts, _ = advisor_context.contract_excerpts(contract, "حلّل هذا العقد واذكر أهم المخاطر")
        chosen = _numbers(excerpts)
        # Only a few fit; the one with a high finding is among them, though it is article 17.
        assert "17.1" in chosen and len(chosen) < len(ARTICLES)

    def test_excerpts_keep_the_citation_label(self, analysed):
        contract, _run_, _clauses = analysed
        first = advisor_context.contract_excerpts(contract, "scope")[0][0]
        assert first.startswith("[Clause 1.1 p.1] 1.1 The Consultant shall perform")


class TestTheStandardsFollowTheQuestion:
    def test_the_asked_categories_standard_comes_first(self, analysed):
        contract, run, clauses = analysed
        for clause in (clauses[0], clauses[17]):
            Finding.objects.create(analysis=run, contract=contract, clause=clause, risk_level="medium",
                                   title="t", category=clause.primary_category,
                                   standard_position=f"AJi position on {clause.primary_category.code}")
        standards = advisor_context.standards_for(contract, {"liability"})
        assert standards[0].endswith("AJi position on liability")


class TestTheAdvisorUsesIt:
    """The chat retrieves from the file's RAG index (apps.analysis.rag), built
    from the same current-run clauses when the analysis finishes."""

    @override_settings(RAG_BUDGET_CHARS=1500)
    def test_the_model_is_sent_the_liability_article(self, signed_in, analysed, monkeypatch):
        from apps.analysis import rag

        contract, run, _clauses = analysed
        rag.build(run)
        seen = {}

        def answer(**kwargs):
            seen.update(kwargs)
            return AICallResult(parsed=AdvisorAnswer(answer="ok"), provider="stub", model="m",
                                request_type="answer_question", latency_ms=1)

        monkeypatch.setattr(ai_service, "answer_question", answer)
        signed_in.post(reverse("analysis:advisor"),
                       {"question": "ما أهم مخاطر بند المسؤولية؟", "contract": str(contract.pk)})
        assert any(e.startswith("[Clause 18.1") for e in seen["document_excerpts"])
