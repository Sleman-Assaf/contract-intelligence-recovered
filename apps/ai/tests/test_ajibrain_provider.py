"""The AJi Brain provider and its context guard (§60).

The guard is the reason this provider is not a thin HTTP wrapper. AJi Brain
truncates an overflowing prompt from the *head* and says nothing, and every
prompt this system sends puts its instructions and the AJi standard at the
head. An answer produced that way is indistinguishable from a good one, so
these tests exist to keep the provider refusing rather than guessing.
"""
from __future__ import annotations

import json

import httpx
import pytest
from django.test import override_settings

from apps.ai.providers.base import (
    AIPermanentError,
    AIProviderNotConfigured,
    AIProviderUnavailable,
)
from apps.ai.providers.ajibrain import AjiBrainProvider

#: Captured before any test patches httpx.Client, so the mock transport can
#: still build a real client without calling back into its own patch.
_REAL_CLIENT = httpx.Client

CONFIGURED = dict(
    AJI_BRAIN_API_BASE_URL="http://172.30.0.4:8000",
    AJI_BRAIN_TEXT_MODEL="qwen3.8:27b",
    AJI_BRAIN_WIRE_FORMAT="aji_brain",
    AJI_BRAIN_MAX_NUM_CTX=32768,
    AJI_BRAIN_CTX_RESERVE=1536,
    AJI_BRAIN_API_KEY="",
    # The fixture is a text-only deployment; the vision tests switch it on.
    # Explicit, because the developer's .env sets a vision model and the
    # test settings inherit it.
    AJI_BRAIN_VISION_MODEL="",
)


@pytest.fixture
def provider():
    with override_settings(**CONFIGURED):
        yield AjiBrainProvider()


class TestWindowSizing:
    def test_small_prompt_takes_the_smallest_bucket(self, provider):
        assert provider._context_for("a short question") == 4096

    def test_window_grows_with_the_prompt(self, provider):
        small = provider._context_for("x" * 2_000)
        large = provider._context_for("x" * 60_000)
        assert small < large <= 32768

    def test_buckets_are_reused_rather_than_fitted_exactly(self, provider):
        """Ollama re-allocates its KV cache when the window changes size."""
        assert provider._context_for("x" * 20_000) == provider._context_for("x" * 21_000)

    def test_arabic_is_costed_higher_than_latin(self, provider):
        """Arabic packs into fewer characters per token, so it needs more room."""
        arabic = provider._estimate_tokens("غرامة التأخير في العقد " * 50)
        latin = provider._estimate_tokens("delay damages in the contract " * 50)
        assert arabic > latin

    def test_a_prompt_that_cannot_fit_is_refused_not_sent(self, provider):
        """The whole point: refuse, rather than let the head be discarded."""
        with pytest.raises(AIPermanentError, match="AJI_BRAIN_MAX_NUM_CTX"):
            provider._context_for("x" * 400_000)


class TestPayloadShape:
    def test_num_ctx_is_sent_at_the_top_level(self, provider):
        """Nested under 'options' the service accepts it and ignores it.

        This is the single most important line in the payload: the wrong
        spelling looks configured and silently is not.
        """
        payload = provider._build_payload(
            "question", model_name="m", image=None, num_ctx=16384
        )
        assert payload["num_ctx"] == 16384
        assert "options" not in payload

    def test_temperature_is_zero_so_a_review_can_be_reproduced(self, provider):
        payload = provider._build_payload("q", model_name="m", image=None, num_ctx=4096)
        assert payload["temperature"] == 0

    def test_thinking_is_disabled(self, provider):
        payload = provider._build_payload("q", model_name="m", image=None, num_ctx=4096)
        assert payload["think"] is False

    def test_an_image_travels_as_base64_in_images(self, provider):
        """The shape /api/general-chat/ forwards to Ollama since 2026-09-27:
        `images: ["<base64>"]`, no data: prefix. Verified live against the
        service with a generated picture reading "AJi 7 4 2 9"."""
        import base64

        from apps.ai.providers.base import ImageRegion

        payload = provider._build_payload(
            "q", model_name="m", num_ctx=4096,
            image=ImageRegion(image_bytes=b"\x89PNG-bytes", mime_type="image/png"),
        )
        assert payload["images"] == [base64.b64encode(b"\x89PNG-bytes").decode()]

    def test_no_image_means_no_images_field(self, provider):
        assert "images" not in provider._build_payload("q", model_name="m", num_ctx=4096, image=None)

    def test_unknown_wire_format_is_rejected(self):
        with override_settings(**{**CONFIGURED, "AJI_BRAIN_WIRE_FORMAT": "invented"}):
            with pytest.raises(AIProviderNotConfigured, match="Unsupported"):
                AjiBrainProvider()._build_payload(
                    "q", model_name="m", image=None, num_ctx=4096
                )


class TestTruncationGuard:
    def test_a_count_below_what_the_text_could_tokenise_to_is_truncation(self, provider):
        """The measured case: 2050 reported for ~107,000 characters."""
        with pytest.raises(AIProviderUnavailable, match="truncated"):
            provider._check_context("x" * 100_000, 4096, 2050)

    def test_arabic_truncation_is_caught_too(self, provider):
        """Arabic runs near 2 characters per token, so a floor calibrated for
        Latin text lets a truncated Arabic prompt through unnoticed."""
        with pytest.raises(AIProviderUnavailable, match="truncated"):
            provider._check_context("بند التأخير " * 3_000, 32768, 2050)

    def test_a_filled_window_is_refused(self, provider):
        with pytest.raises(AIProviderUnavailable, match="filled the context"):
            provider._check_context("x" * 100_000, 8192, 8192)

    def test_an_honest_count_passes(self, provider):
        provider._check_context("x" * 40_000, 16384, 11_500)

    def test_the_floor_sits_below_a_real_tokenisation(self, provider):
        """Measured against the live service: ~85,000 characters of English
        came back as 12,063 tokens. The floor must clear that comfortably or
        the guard refuses perfectly good answers."""
        provider._check_context("x" * 85_000, 16384, 12_063)

    def test_a_short_prompt_near_half_the_window_is_fine(self, provider):
        provider._check_context("x" * 600, 4096, 2048)

    def test_a_missing_count_is_not_treated_as_truncation(self, provider):
        provider._check_context("x" * 40_000, 16384, None)


class TestResponseHandling:
    def test_reads_the_aji_brain_shape(self, provider):
        text, tokens = provider._extract_text({
            "answer": '{"ok": true}',
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        })
        assert text == '{"ok": true}'
        assert tokens == {"prompt": 120, "completion": 30}

    def test_an_unexpected_shape_is_an_error_not_an_empty_answer(self, provider):
        with pytest.raises(AIProviderUnavailable, match="Unexpected"):
            provider._extract_text({"result": "..."})


class TestVisionIsRefused:
    def test_region_verification_refuses_without_a_vision_model(self, provider):
        """A text-only model shown OCR text simply agrees with it. That is a
        check that always passes, which is worse than no check at all -- so
        with no vision model configured the call is refused, not faked."""
        from apps.ai.providers.base import ImageRegion

        assert provider.supports_vision is False
        with pytest.raises(AIProviderNotConfigured, match="AJI_BRAIN_VISION_MODEL"):
            provider.verify_text_region(region=ImageRegion(image_bytes=b"\x89PNG"))

    def test_a_configured_vision_model_switches_it_on(self):
        with override_settings(**{**CONFIGURED, "AJI_BRAIN_VISION_MODEL": "qwen3.8:27b"}):
            assert AjiBrainProvider().supports_vision is True

    def test_a_photo_on_a_question_needs_the_vision_model_too(self, provider):
        from apps.ai.providers.base import ImageRegion

        with pytest.raises(AIProviderNotConfigured, match="AJI_BRAIN_VISION_MODEL"):
            provider.answer_question(
                question="ما هذا البند؟", document_excerpts=[], standards_excerpts=[],
                image=ImageRegion(image_bytes=b"\x89PNG"),
            )


class TestTransport:
    """End to end over a mocked transport, so the guard is exercised for real."""

    @staticmethod
    def _client(handler):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler))

    def test_a_good_call_returns_the_parsed_answer(self, provider, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={
                "answer": '{"primary": "reporting", "alternatives": [], '
                          '"confidence": 1.0, "reasoning": "clear"}',
                "model": "qwen3.8:27b",
                "usage": {"prompt_tokens": 310, "completion_tokens": 44},
            })

        monkeypatch.setattr(httpx, "Client", lambda **kw: self._client(handler))
        result = provider.classify_clause(
            clause_text="يلتزم الاستشاري بتقديم تقرير شهري.",
            categories=["reporting", "payment"],
        )
        assert result.parsed.primary == "reporting"
        assert result.prompt_tokens == 310
        assert seen["num_ctx"] >= 4096
        assert seen["temperature"] == 0

    def test_a_truncated_reply_raises_instead_of_being_returned(self, provider, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            # Exactly what the live service returns when it ignores the
            # requested window and falls back to its 4096 default.
            return httpx.Response(200, json={
                "answer": '{"primary": "payment", "alternatives": [], '
                          '"confidence": 0.9, "reasoning": "x"}',
                "usage": {"prompt_tokens": 2050},
            })

        monkeypatch.setattr(httpx, "Client", lambda **kw: self._client(handler))
        with pytest.raises(AIProviderUnavailable, match="truncated"):
            provider.classify_clause(
                clause_text="بند " * 8_000, categories=["reporting", "payment"]
            )

    def test_a_rejected_api_key_says_so(self, provider, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "Unauthorized."})

        monkeypatch.setattr(httpx, "Client", lambda **kw: self._client(handler))
        with pytest.raises(AIProviderNotConfigured, match="AJI_BRAIN_API_KEY"):
            provider.classify_clause(clause_text="بند", categories=["reporting"])

    def test_the_api_key_is_sent_in_the_header_the_service_reads(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={
                "answer": '{"primary": "reporting", "alternatives": [], '
                          '"confidence": 1.0, "reasoning": "x"}',
                "usage": {"prompt_tokens": 200},
            })

        with override_settings(**{**CONFIGURED, "AJI_BRAIN_API_KEY": "s3cret"}):
            p = AjiBrainProvider()
            monkeypatch.setattr(httpx, "Client", lambda **kw: self._client(handler))
            p.classify_clause(clause_text="بند", categories=["reporting"])
        assert seen["x-api-key"] == "s3cret"

    def test_an_answer_cut_off_at_the_output_limit_is_refused(self, provider, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "answer": '{"primary": "report',
                "done_reason": "length",
                "usage": {"prompt_tokens": 200},
            })

        monkeypatch.setattr(httpx, "Client", lambda **kw: self._client(handler))
        with pytest.raises(AIProviderUnavailable, match="output limit"):
            provider.classify_clause(clause_text="بند", categories=["reporting"])


class TestImagesTakeUpContext:
    """A scanned page is ~1,150 tokens. Sizing the window from the text alone
    asked for one too small to hold the picture."""

    def _png(self, w, h):
        import io

        from PIL import Image

        buf = io.BytesIO(); Image.new("RGB", (w, h), "white").save(buf, "PNG")
        return buf.getvalue()

    def test_a_page_image_is_counted_in_28px_patches(self, provider):
        from apps.ai.providers.base import ImageRegion

        tokens = provider._image_tokens(ImageRegion(image_bytes=self._png(910, 1287)))
        assert 1400 <= tokens <= 1600            # 33 x 46 patches, near the measured 1,156 + text

    def test_several_pages_add_up(self, provider):
        from apps.ai.providers.base import ImageRegion

        one = provider._image_tokens(ImageRegion(image_bytes=self._png(700, 1000)))
        three = provider._image_tokens([ImageRegion(image_bytes=self._png(700, 1000))] * 3)
        assert three == 3 * one

    def test_the_window_grows_to_hold_the_picture(self, provider):
        text_only = provider._context_for("x" * 2_000)
        with_page = provider._context_for("x" * 2_000, extra_tokens=3_000)
        assert with_page > text_only

    def test_no_image_costs_nothing(self, provider):
        assert provider._image_tokens(None) == 0
