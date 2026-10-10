"""The chat is RAG over analysed files: no free chat, no uploads in the chat.

Asked for: when a contract is uploaded and its analysis finishes, the file is
kept in two forms -- the original (the uploaded PDF or Word file, untouched)
and a RAG form the chat answers from -- and appears in the chat's list of
files. The chat answers only from one chosen file's retrieved passages and
the AJi standards its analysis applied; general questions are declined.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from django.urls import reverse

from apps.ai.models import AdvisorConversation, AdvisorMessage
from apps.ai.providers.base import AICallResult
from apps.ai.schemas import AdvisorAnswer
from apps.ai.services import ai_service
from apps.analysis import rag
from apps.analysis.models import AnalysisStatus, RagIndex, RagPassage
from apps.documents.models import SourceFile

from .test_advisor_reads_the_right_clauses import ARTICLES, _document, _run, categories  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def analysed(contract, categories):  # noqa: F811 - the imported fixture
    document = _document(contract)
    run, clauses = _run(contract, document, 1, categories)
    return contract, run, clauses


def _numbers(excerpts):
    return [re.match(r"\[Clause (\S+)", e).group(1) for e in excerpts if not e.startswith(rag.FILE_NOTE)]


class TestTwoFormats:
    def test_the_rag_form_is_built_from_the_analysed_clauses(self, analysed):
        contract, run, clauses = analysed
        index = rag.build(run)
        assert index.contract == contract and index.analysis == run
        assert index.passages_count == len(ARTICLES)
        first = index.passages.first()
        assert first.clause == clauses[0] and first.clause_number == "1.1"
        assert first.page_number == clauses[0].page_number and first.category_code == "scope"
        assert index.doc_freq and index.avg_terms > 0

    def test_the_original_file_is_kept_as_uploaded(self, analysed):
        contract, run, _ = analysed
        before = list(SourceFile.objects.filter(contract=contract).values_list("stored_path", "sha256"))
        rag.build(run)
        assert list(SourceFile.objects.filter(contract=contract)
                    .values_list("stored_path", "sha256")) == before

    def test_a_long_clause_becomes_overlapping_passages(self, contract, categories):  # noqa: F811
        run, _ = _run(contract, _document(contract), 1, categories, words=900)
        index = rag.build(run)
        pieces = list(RagPassage.objects.filter(index=index, clause_number="1.1"))
        assert len(pieces) >= 2
        # the cut is overlapped: the end of one passage opens the next
        assert " ".join(pieces[0].text.split()[-rag.OVERLAP_WORDS:]) in pieces[1].text

    def test_rebuilding_replaces_rather_than_duplicates(self, analysed):
        _, run, _ = analysed
        rag.build(run)
        rag.build(run)
        assert RagIndex.objects.filter(analysis=run).count() == 1
        assert RagPassage.objects.filter(index__analysis=run).count() == len(ARTICLES)

    def test_a_reanalysis_gets_its_own_index_and_the_chat_follows_it(self, contract, categories):  # noqa: F811
        document = _document(contract)
        old, _ = _run(contract, document, 1, categories)
        rag.build(old)
        new, _ = _run(contract, document, 2, categories)
        rag.build(new)
        contract.refresh_from_db()
        assert rag.index_for(contract).analysis == new


class TestItIsBuiltWhenTheAnalysisFinishes:
    def _task(self, monkeypatch, contract, run, status):
        from apps.analysis import tasks

        run.status = status
        run.save(update_fields=["status"])
        monkeypatch.setattr(tasks, "start_analysis", lambda *a, **k: run)
        monkeypatch.setattr(tasks, "run_analysis", lambda r: SimpleNamespace(
            findings_created=0, conflicts=0, missing_clauses=0, ai_failures=0, unclassified_clauses=0))
        return tasks.analyse_contract.apply(args=[str(contract.pk)]).get()

    def test_a_finished_analysis_is_indexed(self, analysed, monkeypatch):
        contract, run, _ = analysed
        self._task(monkeypatch, contract, run, AnalysisStatus.SUCCEEDED)
        assert RagIndex.objects.filter(analysis=run, passages_count=len(ARTICLES)).exists()

    def test_a_failed_one_is_not(self, analysed, monkeypatch):
        contract, run, _ = analysed
        self._task(monkeypatch, contract, run, AnalysisStatus.FAILED)
        assert not RagIndex.objects.filter(analysis=run).exists()

    def test_an_indexing_error_never_fails_the_analysis(self, analysed, monkeypatch):
        contract, run, _ = analysed
        monkeypatch.setattr(rag, "build", lambda run: 1 / 0)
        result = self._task(monkeypatch, contract, run, AnalysisStatus.SUCCEEDED)
        assert result["status"] == AnalysisStatus.SUCCEEDED


class TestRetrieval:
    @pytest.fixture
    def index(self, analysed):
        return rag.build(analysed[1])

    def test_a_named_clause_comes_first(self, index):
        excerpts, _ = rag.search(index, "ماذا يقول البند 17.1؟", budget=1500)
        assert _numbers(excerpts)[0] == "17.1"

    def test_an_arabic_question_finds_an_english_clause(self, index):
        excerpts, topics = rag.search(index, "ما سقف المسؤولية؟", budget=1500)
        assert "18.1" in _numbers(excerpts) and "liability" in topics

    def test_an_english_question_across_word_forms(self, index):
        excerpts, _ = rag.search(index, "Can the client terminate the agreement?", budget=1500)
        assert "14.1" in _numbers(excerpts)

    def test_excerpts_carry_clause_and_page(self, index):
        excerpts, _ = rag.search(index, "insurance", budget=1500)
        assert re.match(r"\[Clause 11\.1 p\.\d+\] 11\.1 The Consultant shall hold", excerpts[0])

    def test_a_small_file_is_given_whole(self, index):
        excerpts, _ = rag.search(index, "لخص العقد", budget=100_000)
        assert len(excerpts) == len(ARTICLES)


def _answer(text="ok"):
    return AICallResult(parsed=AdvisorAnswer(answer=text), provider="stub", model="m",
                        request_type="answer_question", latency_ms=1)


class TestTheChat:
    @pytest.fixture
    def file(self, analysed):
        contract, run, _ = analysed
        rag.build(run)
        return contract

    def test_the_file_list_is_the_analysed_files(self, signed_in, file, contract_type, entity, reviewer):
        from apps.contracts.models import Contract

        unready = Contract.objects.create(project_name="Still Being Read", client_name="C",
                                          entity=entity, contract_type=contract_type,
                                          uploaded_by=reviewer, country=file.country)
        body = signed_in.get(reverse("analysis:advisor")).content.decode()
        assert f"?contract={file.pk}" in body
        assert "Still Being Read" not in body and f"?contract={unready.pk}" not in body

    def test_a_file_still_being_analysed_says_so(self, signed_in, contract_type, entity, reviewer):
        from apps.contracts.models import Contract

        unready = Contract.objects.create(project_name="Still Being Read", client_name="C",
                                          entity=entity, contract_type=contract_type,
                                          uploaded_by=reviewer, country="SA")
        body = signed_in.get(reverse("analysis:advisor") + f"?contract={unready.pk}").content.decode()
        assert "Still Being Read" in body and "notice--warn" in body

    def test_a_question_is_answered_from_the_files_passages_and_standards(self, signed_in, file, monkeypatch):
        seen = {}
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: seen.update(kw) or _answer())
        signed_in.post(reverse("analysis:advisor"),
                       {"question": "ما سقف المسؤولية؟", "contract": file.pk})
        assert "18.1" in _numbers(seen["document_excerpts"])
        assert seen["contract_id"] == file.pk
        assert "image" not in seen

    def test_no_file_means_no_question(self, signed_in, file, monkeypatch):
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: pytest.fail("asked"))
        response = signed_in.post(reverse("analysis:advisor"), {"question": "مرحبا"})
        assert response.status_code == 302 and not AdvisorMessage.objects.exists()

    def test_a_file_that_is_not_indexed_is_refused(self, signed_in, file, monkeypatch):
        RagIndex.objects.all().delete()
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: pytest.fail("asked"))
        signed_in.post(reverse("analysis:advisor"), {"question": "q", "contract": file.pk})
        assert not AdvisorMessage.objects.exists()

    def test_another_users_file_is_not_offered_or_answered(self, client, file, entity, monkeypatch):
        from apps.accounts.models import Role, User

        stranger = User.objects.create_user(email="stranger@aji.test", password="x-password-1",
                                            role=Role.USER, entity=entity)
        client.force_login(stranger)
        body = client.get(reverse("analysis:advisor")).content.decode()
        assert f"?contract={file.pk}" not in body
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: pytest.fail("asked"))
        client.post(reverse("analysis:advisor"), {"question": "q", "contract": file.pk})
        assert not AdvisorMessage.objects.exists()

    def test_another_file_starts_a_new_chat(self, signed_in, file, contract, categories, monkeypatch):  # noqa: F811
        from apps.contracts.models import Contract

        other = Contract.objects.create(project_name="Second", client_name="C", entity=file.entity,
                                        contract_type=file.contract_type, uploaded_by=file.uploaded_by,
                                        country=file.country)
        rag.build(_run(other, _document(other), 1, categories)[0])
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: _answer())
        signed_in.post(reverse("analysis:advisor"), {"question": "one", "contract": file.pk})
        signed_in.post(reverse("analysis:advisor"), {"question": "two", "contract": other.pk})
        chats = AdvisorConversation.objects.order_by("created_at")
        assert [c.contract for c in chats] == [file, other]
        # and each thread's history is its own file's only
        seen = {}
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: seen.update(kw) or _answer())
        signed_in.post(reverse("analysis:advisor"), {"question": "three", "contract": other.pk})
        assert [h["body"] for h in seen["history"]] == ["two", "ok"]

    def test_memory_of_past_chats_stays_with_the_file(self, reviewer, file, contract_type, entity):
        from apps.ai.recall import recall_for
        from apps.contracts.models import Contract

        other = Contract.objects.create(project_name="Other", client_name="C", entity=entity,
                                        contract_type=contract_type, uploaded_by=reviewer, country="SA")
        chat = AdvisorConversation.objects.create(user=reviewer, contract=other, title="t")
        AdvisorMessage.objects.create(conversation=chat, role="user", body="ما سقف المسؤولية في العقد؟")
        AdvisorMessage.objects.create(conversation=chat, role="assistant", body="السقف مليون ريال.")
        assert recall_for(reviewer, "ما سقف المسؤولية؟", contract=file) == []

    def test_an_old_chat_with_no_file_is_read_only(self, signed_in, file, reviewer):
        from apps.analysis.views import AdvisorView

        chat = AdvisorConversation.objects.create(user=reviewer, title="free chat")
        AdvisorMessage.objects.create(conversation=chat, role="user", body="ما هي وصفة الكبسة؟")
        session = signed_in.session
        session[AdvisorView.SESSION_KEY] = str(chat.pk)
        session.save()
        body = signed_in.get(reverse("analysis:advisor")).content.decode()
        assert "advisor__readonly" in body and "ما هي وصفة الكبسة؟" in body
        assert 'class="advisor__composer"' not in body


class TestHostileQuestions:
    def test_markup_does_not_steer_retrieval(self, analysed):
        """"<script>alert(1)</script> ما سقف المسؤولية؟" retrieved the
        definitions clause on "script" and "alert"."""
        index = rag.build(analysed[1])
        clean, _ = rag.search(index, "ما سقف المسؤولية؟", budget=1500)
        dirty, _ = rag.search(index, "<script>alert(1)</script> ما سقف المسؤولية؟ https://x.example/a",
                              budget=1500)
        assert _numbers(dirty) == _numbers(clean)


class TestEveryPageIsRead:
    """Asked for: the chat reads all pages of the selected file."""

    def test_a_clause_over_a_page_break_is_indexed_on_both_pages(self, contract, categories):  # noqa: F811
        from apps.documents.models import Block, Page

        document = _document(contract)
        run, clauses = _run(contract, document, 1, categories)
        annex = clauses[-1]
        for n, text in ((9, "Annex D starts on page nine."), (10, "It ends with the key personnel table on page ten.")):
            page, _ = Page.objects.get_or_create(document=document, page_number=n,
                                                 defaults={"extraction_method": "native_pdf"})
            block = Block.objects.create(page=page, document=document, block_index=0,
                                         original_source_text=text, extraction_method="native_pdf")
            annex.source_blocks.add(block)
        index = rag.build(run)
        pages = {p.page_number for p in index.passages.filter(clause=annex)}
        assert pages == {9, 10}
        assert index.passages.get(clause=annex, page_number=10).text.endswith("on page ten.")

    def test_a_file_that_fits_is_sent_complete_for_every_question(self, analysed):
        index = rag.build(analysed[1])
        for question in ("ما سقف المسؤولية؟", "What is the governing law?"):
            excerpts, _ = rag.search(index, question)
            assert len(excerpts) == len(ARTICLES) + 1
            assert excerpts[-1].startswith(rag.FILE_NOTE) and "complete file" in excerpts[-1]

    def test_a_large_file_gets_every_page_for_a_whole_file_question(self, contract, categories, settings):  # noqa: F811
        settings.RAG_WHOLE_FILE_CHARS = 2000
        run, clauses = _run(contract, _document(contract), 1, categories)
        index = rag.build(run)
        all_pages = {c.page_number for c in clauses}
        excerpts, _ = rag.search(index, "لخصلي العقد صفحة صفحة")
        pages = {int(re.search(r" p\.(\d+)\]", e).group(1)) for e in excerpts if not e.startswith(rag.FILE_NOTE)}
        assert pages == all_pages
        assert "Every page is represented" in excerpts[-1]
        # a specific question on the same large file is still answered by search
        specific, _ = rag.search(index, "ما سقف المسؤولية؟")
        assert "18.1" in _numbers(specific) and not specific[-1].startswith(rag.FILE_NOTE)

    def test_the_note_is_never_shown_as_a_quotation(self, signed_in, analysed, monkeypatch):
        contract, run, _ = analysed
        rag.build(run)
        note_label = f"SOURCE {len(ARTICLES) + 1}"
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: AICallResult(
            parsed=AdvisorAnswer(answer="ok", source_references=["SOURCE 1", note_label]),
            provider="stub", model="m", request_type="answer_question", latency_ms=1))
        signed_in.post(reverse("analysis:advisor"), {"question": "لخص", "contract": contract.pk})
        quotes = AdvisorMessage.objects.get(role="assistant").source_quotes
        assert quotes and not any(q.startswith(rag.FILE_NOTE) for q in quotes)
