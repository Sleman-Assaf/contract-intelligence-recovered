"""The advisor's answer, shown while it is being written.

The model streams raw JSON; the reader is sent only the words of its
"answer" field, as they arrive, and then the finished turn -- validated,
cleaned and cited exactly as the page's own POST records it -- which
replaces the draft. Failures end the stream with the page's own failure turn.
"""
from __future__ import annotations

import json

import httpx
import pytest
from django.test import override_settings
from django.urls import reverse

from apps.ai.models import AdvisorMessage
from apps.ai.providers.ajibrain import AjiBrainProvider
from apps.ai.providers.base import AICallResult, AIProviderUnavailable
from apps.ai.schemas import AdvisorAnswer
from apps.ai.services import ai_service
from apps.ai.streaming import AnswerTap
from apps.ai.tests.test_ajibrain_provider import _REAL_CLIENT, CONFIGURED

RAW = '{"answer": "السقف \\"20%\\" أعلى من المعيار.\\nراجع [SOURCE 1] البند.", "source_references": [], "standards_referenced": [], "is_interpretation": true}'


def _pieces(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


class TestTheAnswerIsReadOutOfTheJson:
    @pytest.mark.parametrize("size", [1, 2, 3, 7, 500])
    def test_any_split_gives_the_same_words(self, size):
        tap = AnswerTap()
        text = "".join(tap.feed(p) for p in _pieces(RAW, size))
        assert text == json.loads(RAW)["answer"]
        assert tap.finished

    def test_nothing_before_the_answer_field_is_shown(self):
        tap = AnswerTap()
        assert tap.feed('```json\n{"ans') == ""
        assert tap.feed('wer": "Yes') == "Yes"

    def test_unicode_escapes_even_when_split(self):
        tap = AnswerTap()
        raw = '{"answer": "\\u0627\\u0644\\ud83d\\ude00 ok"}'
        assert "".join(tap.feed(p) for p in _pieces(raw, 1)) == "ال\U0001F600 ok"

    def test_what_follows_the_answer_is_not_shown(self):
        tap = AnswerTap()
        tap.feed('{"answer": "done", "source_references": ["[SOURCE 1] secret text"]}')
        assert tap.feed(" more") == ""


def _sse(*events):
    return "".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events)


@pytest.fixture
def brain(monkeypatch):
    replies = {}

    def handler(request):
        replies["payload"] = json.loads(request.content)
        return replies["response"]

    monkeypatch.setattr(httpx, "Client", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler)))
    with override_settings(**CONFIGURED):
        yield AjiBrainProvider(), replies


def _ask(provider):
    return list(provider.answer_question_stream(
        question="ما السقف؟", document_excerpts=["[Clause 1] cap 20%"], standards_excerpts=[]))


class TestTheBrainStreams:
    def test_pieces_then_the_validated_answer(self, brain):
        provider, replies = brain
        chunks = _pieces(RAW, 9)
        replies["response"] = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse(
            *[{"chunk": c} for c in chunks],
            {"done": True, "done_reason": "stop", "usage": {"prompt_tokens": 2000, "completion_tokens": 40}}))
        out = _ask(provider)
        assert replies["payload"]["stream"] is True and replies["payload"]["num_ctx"] >= 4096
        assert out[:-1] == chunks
        result = out[-1]
        assert isinstance(result.parsed, AdvisorAnswer) and result.completion_tokens == 40
        assert result.parsed.answer == json.loads(RAW)["answer"]

    def test_an_error_in_the_stream_is_raised(self, brain):
        provider, replies = brain
        replies["response"] = httpx.Response(200, text=_sse({"chunk": '{"answer": "x'},
                                                             {"error": "LLM service unavailable"}))
        with pytest.raises(AIProviderUnavailable, match="stream error"):
            _ask(provider)

    def test_a_cut_off_answer_is_not_passed_as_complete(self, brain):
        provider, replies = brain
        replies["response"] = httpx.Response(200, text=_sse({"chunk": '{"answer": "x'},
                                                             {"done": True, "done_reason": "length"}))
        with pytest.raises(AIProviderUnavailable, match="output limit"):
            _ask(provider)

    def test_a_stream_that_just_stops_is_an_error(self, brain):
        provider, replies = brain
        replies["response"] = httpx.Response(200, text=_sse({"chunk": '{"answer": "x'}))
        with pytest.raises(AIProviderUnavailable, match="before the answer was complete"):
            _ask(provider)


def _result(raw=RAW):
    return AICallResult(parsed=AdvisorAnswer.model_validate_json(raw), provider="stub", model="m",
                        request_type="answer_question", latency_ms=1, raw_text=raw)


@pytest.mark.django_db
class TestTheServiceFallsBack:
    def test_a_stream_that_fails_before_any_word_asks_again_the_ordinary_way(self, monkeypatch):
        class Broken:
            name = "stub"

            def answer_question_stream(self, **kwargs):
                raise AIProviderUnavailable("down")
                yield                                     # pragma: no cover

        monkeypatch.setattr("apps.ai.services.get_ai_provider", lambda: Broken())
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: _result())
        out = list(ai_service.answer_question_stream(question="q", document_excerpts=[]))
        assert out[0] == RAW and out[-1].parsed.answer == json.loads(RAW)["answer"]

    def test_a_stream_that_fails_after_words_were_shown_is_not_repeated(self, monkeypatch):
        class Halfway:
            name = "stub"

            def answer_question_stream(self, **kwargs):
                yield '{"answer": "half'
                raise AIProviderUnavailable("dropped")

        monkeypatch.setattr("apps.ai.services.get_ai_provider", lambda: Halfway())
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: pytest.fail("asked twice"))
        with pytest.raises(AIProviderUnavailable):
            list(ai_service.answer_question_stream(question="q", document_excerpts=[]))


def _events(response):
    body = b"".join(response.streaming_content).decode("utf-8")
    return [json.loads(block[5:]) for block in body.split("\n\n") if block.startswith("data:")]


@pytest.mark.django_db
class TestTheStreamEndpoint:
    def test_words_then_the_finished_turn(self, signed_in, monkeypatch, chat_file):
        monkeypatch.setattr(ai_service, "answer_question_stream",
                            lambda **kw: iter(_pieces(RAW, 5) + [_result()]))
        response = signed_in.post(reverse("analysis:advisor_stream"), {"contract": chat_file.pk, "question": "ما السقف؟"})
        assert response["Content-Type"].startswith("text/event-stream")
        assert response["X-Accel-Buffering"] == "no"
        events = _events(response)
        words = "".join(e["text"] for e in events if e["type"] == "chunk")
        assert words == json.loads(RAW)["answer"]
        done = events[-1]
        assert done["type"] == "done" and 'class="bubble bubble--answer"' in done["html"]
        # the finished turn is the cleaned one: the label the draft showed is gone
        assert "[SOURCE 1]" not in done["html"] and "20%" in done["html"]
        saved = AdvisorMessage.objects.filter(role=AdvisorMessage.Role.ASSISTANT).get()
        assert saved.body.startswith("السقف") and "[SOURCE" not in saved.body
        assert AdvisorMessage.objects.filter(role=AdvisorMessage.Role.USER, body="ما السقف؟").exists()

    def test_a_failure_ends_with_the_failure_turn(self, signed_in, monkeypatch, chat_file):
        def failing(**kw):
            yield '{"answer": "par'
            raise AIProviderUnavailable("dropped")

        monkeypatch.setattr(ai_service, "answer_question_stream", failing)
        events = _events(signed_in.post(reverse("analysis:advisor_stream"), {"contract": chat_file.pk, "question": "سؤال"}))
        assert events[-1]["type"] == "error" and 'role="alert"' in events[-1]["html"]
        assert AdvisorMessage.objects.filter(role=AdvisorMessage.Role.ASSISTANT).exclude(error="").exists()

    def test_nothing_to_ask_is_the_same_redirect_as_the_page(self, signed_in, chat_file):
        response = signed_in.post(reverse("analysis:advisor_stream"), {"contract": chat_file.pk, "question": "  "})
        assert response.status_code == 302 and response["Location"] == reverse("analysis:advisor")

    def test_only_post(self, signed_in):
        assert signed_in.get(reverse("analysis:advisor_stream")).status_code == 405

    def test_signing_in_is_required(self, client):
        response = client.post(reverse("analysis:advisor_stream"), {"question": "q"})
        assert response.status_code == 302 and "login" in response["Location"]

    def test_the_page_tells_the_script_where_to_stream(self, signed_in, chat_file):
        body = signed_in.get(reverse("analysis:advisor")).content.decode()
        assert f'data-stream-url="{reverse("analysis:advisor_stream")}"' in body

    def test_the_page_post_still_answers_without_the_script(self, signed_in, monkeypatch, chat_file):
        monkeypatch.setattr(ai_service, "answer_question", lambda **kw: _result())
        response = signed_in.post(reverse("analysis:advisor"), {"contract": chat_file.pk, "question": "ما السقف؟"})
        assert response.status_code == 302
        page = signed_in.get(reverse("analysis:advisor")).content.decode()
        assert "أعلى من المعيار" in page


@pytest.mark.django_db
def test_an_answer_citing_everything_keeps_only_the_first_quotes(reviewer, rf):
    """Asked for "at most 10", a page-by-page summary of a whole file still
    cited 30 excerpts; what is saved and shown under the answer is capped."""
    from types import SimpleNamespace

    from apps.ai.models import AdvisorConversation
    from apps.analysis.views import AdvisorView

    excerpts = [f"[Clause {n}.1 p.{n}] Clause {n} of the attached file, with its own words." for n in range(1, 41)]
    parsed = AdvisorAnswer(answer="ملخص الصفحات", source_references=[f"SOURCE {n}" for n in range(1, 41)])
    call = AICallResult(parsed=parsed, provider="stub", model="m", request_type="answer_question", latency_ms=1)
    request = rf.post("/advisor/")
    request.user, request.session = reviewer, {}
    conversation = AdvisorConversation.objects.create(user=reviewer, title="t")
    turn = SimpleNamespace(question="لخص", contract=None, file=None, excerpts=excerpts, standards=[],
                           doc_label="file.pdf", conversation=conversation, ask={"recalled": []})
    saved = AdvisorView()._answer(request, turn, call)
    assert saved.body == "ملخص الصفحات"
    assert len(parsed.source_references) == 30                    # the schema's own cap
    assert saved.source_quotes == excerpts[:AdvisorView.MAX_QUOTES]   # what is shown
