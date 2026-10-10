"""Classifying several clauses per call: faster, and never less correct.

A batch saves the fixed wait each call carries. Whatever a batch does not
answer cleanly -- a missing id, a repeated id, a category not on the list, a
failed call -- is asked about one clause at a time, as before batching.
"""
from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import Entity
from apps.ai import prompts
from apps.ai.providers.base import AICallResult, AIProvider, AIProviderUnavailable
from apps.ai.schemas import BatchedClassification, ClauseClassificationBatch
from apps.ai.services import ai_service
from apps.analysis import services
from apps.analysis.models import Clause
from apps.analysis.tests.test_pipeline_end_to_end import _build_pdf, stub, uploader  # noqa: F401  (fixtures)
from apps.contracts.models import ContractType
from apps.contracts.services import create_contract
from apps.documents.services import run_extraction

CATEGORIES = ["insurance", "payment", "scope"]


class TestThePrompts:
    def test_the_single_call_no_longer_writes_reasoning(self):
        text = prompts.classify_clause(clause_text="x", categories=CATEGORIES)
        assert '"reasoning"' not in text

    def test_the_batch_names_every_clause_by_id_and_number(self):
        text = prompts.classify_clauses(
            clauses=[(1, "6.1", "The Client shall pay."), (2, "", "Insurance cover.")],
            categories=CATEGORIES)
        assert '[1] clause 6.1:\n"""The Client shall pay."""' in text
        assert '[2] clause (unnumbered):' in text and '"results"' in text


def _answer(*items):
    parsed = ClauseClassificationBatch(results=[
        BatchedClassification(id=i, primary=p, confidence=0.9) for i, p in items])
    return AICallResult(parsed=parsed, provider="stub", model="stub",
                        request_type="classify_clauses", latency_ms=1)


class _Batcher:
    """Only the batch call exists; nothing else is reached here."""

    name = "stub"

    def __init__(self, reply):
        self.reply = reply

    def classify_clauses(self, *, clauses, categories):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(ai_service, "_log", lambda *a, **k: None)

    def use(provider):
        monkeypatch.setattr("apps.ai.services.get_ai_provider", lambda: provider)
    return use


ASKED = [(1, "6.1", "a"), (2, "6.2", "b"), (3, "6.3", "c")]


class TestOnlyTrustworthyAnswersAreKept:
    def test_a_clean_batch(self, quiet):
        quiet(_Batcher(_answer((1, "payment"), (2, "payment"), (3, "insurance"))))
        got = ai_service.classify_clauses(clauses=ASKED, categories=CATEGORIES)
        assert {k: v.primary for k, v in got.items()} == {1: "payment", 2: "payment", 3: "insurance"}

    def test_missing_repeated_unknown_and_invented_ids_are_left_out(self, quiet):
        quiet(_Batcher(_answer((1, "payment"), (2, "scope"), (2, "payment"), (3, "warranty"), (9, "scope"))))
        assert ai_service.classify_clauses(clauses=ASKED, categories=CATEGORIES).keys() == {1}

    def test_a_failed_call_leaves_everything_to_the_single_calls(self, quiet, settings):
        settings.AI_MAX_RETRIES = 1
        quiet(_Batcher(AIProviderUnavailable("down")))
        assert ai_service.classify_clauses(clauses=ASKED, categories=CATEGORIES) == {}

    def test_a_provider_without_batching_is_asked_one_at_a_time(self, quiet):
        class Plain(_Batcher):
            classify_clauses = AIProvider.classify_clauses
        quiet(Plain(None))
        assert ai_service.classify_clauses(clauses=ASKED, categories=CATEGORIES) == {}


@pytest.mark.django_db
class TestThePipeline:
    def _analyse(self, uploader):
        contract, source = create_contract(
            actor=uploader, uploaded_file=SimpleUploadedFile("a.pdf", _build_pdf(), "application/pdf"),
            project_name="P", contract_title="T", client_name="C", country="SA",
            entity=Entity.objects.get(code="aji-sa"), contract_type=ContractType.objects.get(code="arch"),
            contract_value=12400000, currency="SAR", language="en",
        )
        run_extraction(source)
        run = services.start_analysis(contract, document=contract.documents.latest("created_at"), actor=uploader)
        services.run_analysis(run)
        run.refresh_from_db()
        return run

    def _categories(self, run):
        return {c.clause_number: c.primary_category.code if c.primary_category else None
                for c in Clause.objects.filter(document=run.document)}

    def test_same_answers_fewer_calls_and_a_dropped_clause_is_still_read(
            self, uploader, stub, monkeypatch, settings):
        one_by_one = self._analyse(uploader)
        single_calls = stub.classify_calls

        settings.AI_CLASSIFY_BATCH_SIZE = 8
        batches = []

        def classify_clauses(*, clauses, categories, **kwargs):
            batches.append([cid for cid, _n, _t in clauses])
            # answer all but the last clause of every batch
            return {cid: BatchedClassification(id=cid, primary=stub._category_for(text), confidence=0.95)
                    for cid, _number, text in clauses[:-1]}

        monkeypatch.setattr(services.ai_service, "classify_clauses", classify_clauses)
        stub.classify_calls = 0
        batched = self._analyse(uploader)

        assert batches and all(1 < len(b) <= 8 for b in batches)
        # every clause a batch did not answer went through the single call
        answered = sum(len(b) - 1 for b in batches)
        total = Clause.objects.filter(document=batched.document).count()
        assert stub.classify_calls == total - answered < single_calls
        assert self._categories(batched) == self._categories(one_by_one)
        assert batched.status == one_by_one.status
