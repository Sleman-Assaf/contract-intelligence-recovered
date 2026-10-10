"""Stopping an answer while it is being written.

The reader presses Stop; the browser cancels the request and the server sees
the connection close at the next piece. What had been shown is kept, marked
stopped (never validated or cited), and the request to the model is closed so
it stops writing -- the Brain answers one request at a time.
"""
from __future__ import annotations

import json
import time

import pytest
from django.urls import reverse

from apps.ai.models import AdvisorMessage, AIRequestLog
from apps.ai.providers.base import AICallResult
from apps.ai.schemas import AdvisorAnswer
from apps.ai.services import ai_service

pytestmark = pytest.mark.django_db

PIECES = ['{"answer": "الجزء ', 'الأول [SOURCE 1]', ' من الإجابة', ' والجزء الثاني"}']


class _Model:
    """A streaming model that writes at a readable pace and, like the real
    provider, stops when its Cancel is set."""

    def __init__(self, *, think=0.0):
        self.closed = False
        self.think = think

    def stream(self, *, cancel=None, **kwargs):
        waited = 0.0
        while waited < self.think:                     # still reading the prompt
            if cancel is not None and cancel.is_set:
                self.closed = True
                return
            time.sleep(0.02)
            waited += 0.02
        for piece in PIECES:
            if cancel is not None and cancel.is_set:
                self.closed = True
                return
            yield piece
            time.sleep(0.05)
        yield AICallResult(parsed=AdvisorAnswer(answer="الجزء الأول من الإجابة والجزء الثاني"),
                           provider="stub", model="m", request_type="answer_question",
                           latency_ms=1, raw_text="".join(PIECES))


@pytest.fixture
def model(monkeypatch):
    m = _Model()
    monkeypatch.setattr(ai_service, "answer_question_stream", m.stream)
    return m


def _chunks(response, count):
    """Read `count` chunk events, as the browser would before pressing Stop."""
    events, stream = [], iter(response.streaming_content)
    while len(events) < count:
        block = next(stream).decode("utf-8")
        events += [json.loads(p[5:]) for p in block.split("\n\n") if p.startswith("data:")]
    return events


def _assistant():
    return AdvisorMessage.objects.filter(role=AdvisorMessage.Role.ASSISTANT)


def _eventually(check, within=2.0):
    """The model is read on its own thread: give it a moment to notice."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    assert check()


class TestStopping:
    def test_what_was_shown_is_kept_and_marked_stopped(self, signed_in, model, chat_file):
        response = signed_in.post(reverse("analysis:advisor_stream"), {"contract": chat_file.pk, "question": "ما السقف؟"})
        seen = _chunks(response, 2)
        response.close()                                   # the connection closes: Stop
        turn = _assistant().get()
        assert turn.stopped is True and turn.error == ""
        # exactly what was sent before Stop -- two pieces -- with the label cleaned
        assert turn.body == "الجزء الأول"
        assert "".join(e["text"] for e in seen).startswith("الجزء الأول")
        assert turn.source_quotes == [] and turn.standard_quotes == []   # never cited

    def test_the_model_is_told_to_stop(self, signed_in, model, chat_file):
        response = signed_in.post(reverse("analysis:advisor_stream"), {"contract": chat_file.pk, "question": "q"})
        _chunks(response, 1)
        response.close()
        _eventually(lambda: model.closed)

    def test_stopped_while_it_is_still_thinking(self, signed_in, monkeypatch, settings, chat_file):
        """Before the first word there is nothing to send -- so keep-alives
        are sent, and the first one after Stop is what notices it."""
        thinking = _Model(think=5.0)
        monkeypatch.setattr(ai_service, "answer_question_stream", thinking.stream)
        monkeypatch.setattr("apps.analysis.views.AdvisorStreamView.HEARTBEAT", 0.05)
        started = time.monotonic()
        response = signed_in.post(reverse("analysis:advisor_stream"), {"contract": chat_file.pk, "question": "q"})
        first = next(iter(response.streaming_content)).decode("utf-8")
        assert first.startswith(":")                        # a keep-alive, not an answer
        response.close()
        _eventually(lambda: thinking.closed)
        assert time.monotonic() - started < 2               # not the five seconds of thinking
        turn = _assistant().get()
        assert turn.stopped is True and turn.body == ""     # the note, and no words

    def test_a_finished_answer_is_not_mistaken_for_a_stop(self, signed_in, model, chat_file):
        response = signed_in.post(reverse("analysis:advisor_stream"), {"contract": chat_file.pk, "question": "q"})
        body = b"".join(response.streaming_content)
        response.close()
        assert b'"type": "done"' in body
        turn = _assistant().get()
        assert turn.stopped is False and turn.body.endswith("والجزء الثاني")
        assert not AIRequestLog.objects.filter(error_message__contains="stopped").exists()


class TestThePage:
    def test_a_stopped_turn_says_so(self, signed_in, model, chat_file):
        response = signed_in.post(reverse("analysis:advisor_stream"), {"contract": chat_file.pk, "question": "q"})
        _chunks(response, 2)
        response.close()
        page = signed_in.get(reverse("analysis:advisor")).content.decode()
        assert 'class="turn__stopped"' in page and "الجزء الأول" in page

    def test_the_composer_has_a_stop_button(self, signed_in, chat_file):
        page = signed_in.get(reverse("analysis:advisor")).content.decode()
        assert "data-stop-answer" in page and 'data-stopped-label="' in page


class TestTheCancelSignal:
    def test_set_runs_every_closer_once_even_if_one_fails(self):
        from apps.ai.streaming import Cancel

        cancel, ran = Cancel(), []
        cancel.on(lambda: ran.append("client"))
        cancel.on(lambda: 1 / 0)                          # a failing closer does not stop the rest
        cancel.on(lambda: ran.append("response"))
        cancel.set()
        cancel.set()
        assert ran == ["client", "response"] and cancel.is_set

    def test_a_closer_registered_after_stop_runs_at_once(self):
        from apps.ai.streaming import Cancel

        cancel, ran = Cancel(), []
        cancel.set()
        cancel.on(lambda: ran.append("late"))
        assert ran == ["late"]
