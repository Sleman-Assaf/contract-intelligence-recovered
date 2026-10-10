"""An identical analysis prompt is answered once.

The model is called for the first asking; the same prompt again -- a
re-analysis, or the same template clause in another contract -- reuses the
validated answer. Anything that changes the prompt asks the model again.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from django.test import override_settings
from django.utils import timezone

from apps.ai.models import AIResponseCache
from apps.ai.providers.ajibrain import AjiBrainProvider
from apps.ai.tests.test_ajibrain_provider import _REAL_CLIENT, CONFIGURED

pytestmark = pytest.mark.django_db

ANSWER = '{"primary": "payment", "alternatives": [], "confidence": 0.9}'


@pytest.fixture
def brain(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={
            "answer": ANSWER, "model": "qwen3.8:27b",
            "usage": {"prompt_tokens": 300, "completion_tokens": 30}})

    monkeypatch.setattr(httpx, "Client", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler)))
    with override_settings(**CONFIGURED, AI_RESPONSE_CACHE=True, AI_RESPONSE_CACHE_DAYS=90):
        yield AjiBrainProvider(), calls


def _classify(provider, text="The Client shall pay within 30 days."):
    return provider.classify_clause(clause_text=text, categories=["payment", "scope"])


class TestTheSamePromptIsAnsweredOnce:
    def test_the_second_asking_does_not_call_the_model(self, brain):
        provider, calls = brain
        first, second = _classify(provider), _classify(provider)
        assert len(calls) == 1
        assert not first.cached and second.cached
        assert second.parsed == first.parsed and second.latency_ms == 0
        assert AIResponseCache.objects.get().hits == 1

    def test_a_different_clause_asks_again(self, brain):
        provider, calls = brain
        _classify(provider)
        _classify(provider, "The Client shall pay within 45 days.")
        assert len(calls) == 2

    def test_the_prompt_itself_is_not_stored(self, brain):
        provider, _calls = brain
        _classify(provider)
        row = AIResponseCache.objects.get()
        assert len(row.key) == 64 and "30 days" not in row.key and row.raw_text == ANSWER


class TestWhenTheCacheStepsAside:
    def test_off_means_every_call_reaches_the_model(self, brain):
        provider, calls = brain
        with override_settings(AI_RESPONSE_CACHE=False):
            _classify(provider), _classify(provider)
        assert len(calls) == 2 and not AIResponseCache.objects.exists()

    def test_an_expired_answer_is_asked_again(self, brain):
        provider, calls = brain
        _classify(provider)
        AIResponseCache.objects.update(created_at=timezone.now() - timedelta(days=91))
        assert not _classify(provider).cached and len(calls) == 2

    def test_a_stored_answer_the_schema_no_longer_accepts_is_dropped(self, brain):
        provider, calls = brain
        _classify(provider)
        AIResponseCache.objects.update(raw_text='{"primary": "payment"}')     # confidence missing
        assert not _classify(provider).cached and len(calls) == 2
        assert AIResponseCache.objects.get().raw_text == ANSWER

    def test_the_advisor_is_never_cached(self):
        from apps.ai import cache

        with override_settings(AI_RESPONSE_CACHE=True):
            assert cache.enabled("assess_clause") and not cache.enabled("answer_question")
