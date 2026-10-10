"""The reader deletes the thread while its question is still being answered.

Seen live: an answer took two and a half minutes, the reader gave up and
deleted the conversation from the sidebar, and when the model finally came
back the view wrote the turn into a row that no longer existed -- a
foreign-key error, and a 500 page for having cleaned up. Four times in one
minute.

The turn is dropped instead. They threw the thread away, and with it the
answer to a question they no longer wanted.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.ai.models import AdvisorConversation, AdvisorMessage
from apps.ai.providers.base import AICallResult, AIProviderUnavailable
from apps.ai.schemas import AdvisorAnswer

pytestmark = pytest.mark.django_db

ADVISOR = reverse("analysis:advisor")


@pytest.fixture
def signed_in(client, reviewer):
    client.force_login(reviewer)
    return client


def _deleting(behaviour):
    """A fake `answer_question` that first deletes every thread -- what the
    reader did in the sidebar while waiting -- and then behaves as asked."""

    def fake(**kwargs):
        AdvisorConversation.objects.all().delete()
        return behaviour()

    return fake


def _answer():
    return AICallResult(
        parsed=AdvisorAnswer(answer="السقف عشرة بالمئة."),
        provider="fake", model="m", request_type="answer_question",
        latency_ms=1, prompt_tokens=1, completion_tokens=1, raw_text="{}",
    )


def _unavailable():
    raise AIProviderUnavailable("AJi Brain server error 504")


class TestNoServerErrorEitherWay:
    def test_when_the_answer_arrives(self, signed_in, monkeypatch, chat_file):
        from apps.ai.services import ai_service

        monkeypatch.setattr(ai_service, "answer_question", _deleting(_answer))
        response = signed_in.post(ADVISOR, {"contract": chat_file.pk, "question": "ما سقف الغرامة؟"})
        assert response.status_code == 302
        # Nothing left to write into, and nothing written anywhere else.
        assert AdvisorMessage.objects.count() == 0

    def test_when_the_call_fails(self, signed_in, monkeypatch, chat_file):
        from apps.ai.services import ai_service

        monkeypatch.setattr(ai_service, "answer_question", _deleting(_unavailable))
        response = signed_in.post(ADVISOR, {"contract": chat_file.pk, "question": "ما سقف الغرامة؟"})
        assert response.status_code == 302
        assert AdvisorMessage.objects.count() == 0

    def test_the_session_forgets_the_deleted_thread(self, signed_in, monkeypatch, chat_file):
        """Otherwise the next question would try to continue a thread that
        is gone, and the page would show an empty conversation as open."""
        from apps.ai.services import ai_service
        from apps.analysis.views import AdvisorView

        monkeypatch.setattr(ai_service, "answer_question", _deleting(_answer))
        signed_in.post(ADVISOR, {"contract": chat_file.pk, "question": "ما سقف الغرامة؟"})
        assert AdvisorView.SESSION_KEY not in signed_in.session

    def test_the_next_question_starts_a_fresh_thread(self, signed_in, monkeypatch, chat_file):
        from apps.ai.services import ai_service

        monkeypatch.setattr(ai_service, "answer_question", _deleting(_answer))
        signed_in.post(ADVISOR, {"contract": chat_file.pk, "question": "الأول"})
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: _answer())
        signed_in.post(ADVISOR, {"contract": chat_file.pk, "question": "الثاني"})
        assert AdvisorConversation.objects.count() == 1
        bodies = list(AdvisorMessage.objects.order_by("created_at").values_list("body", flat=True))
        assert bodies == ["الثاني", "السقف عشرة بالمئة."]


def test_a_thread_that_is_still_there_gets_its_turn_as_before(signed_in, monkeypatch, chat_file):
    from apps.ai.services import ai_service

    monkeypatch.setattr(ai_service, "answer_question", lambda **kw: _answer())
    signed_in.post(ADVISOR, {"contract": chat_file.pk, "question": "ما سقف الغرامة؟"})
    roles = list(AdvisorMessage.objects.order_by("created_at").values_list("role", flat=True))
    assert roles == ["user", "assistant"]
