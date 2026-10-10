"""Behaviour under a provider that is rate limiting or unavailable.

Written after a real end-to-end run against Gemini's free tier, where 11 of 12
calls were rejected with HTTP 429. The pipeline survived, but three things were
wrong: we ignored the retry delay the service told us, we sent the whole batch
at once, and the run reported itself as succeeded.
"""
from __future__ import annotations

import time

import pytest

from apps.ai.providers.base import _retry_after_from
from apps.ai.services import _RateLimiter


class TestRetryAfterParsing:
    """The service says how long to wait. Guessing instead wastes the quota."""

    @pytest.mark.parametrize("message,expected", [
        # Exactly what the live API returned during the end-to-end run.
        ("429 RESOURCE_EXHAUSTED ... Please retry in 49.352007055s.", 49.352007055),
        ("Please retry in 1.735006783s.", 1.735006783),
        ("{'error': {...}, 'retryDelay': '8s'}", 8.0),
        ('{"retryDelay": "1.7s"}', 1.7),
        ("Retry In 12s", 12.0),
    ])
    def test_extracts_the_delay(self, message, expected):
        assert _retry_after_from(message) == pytest.approx(expected)

    def test_returns_none_when_not_stated(self):
        # A 503 carries no delay; the caller falls back to its own backoff.
        assert _retry_after_from(
            "503 UNAVAILABLE. This model is currently experiencing high demand."
        ) is None

    def test_a_malformed_delay_does_not_raise(self):
        assert _retry_after_from("retry in later") is None


class TestRateLimiter:
    def test_disabled_by_default_costs_nothing(self, settings):
        settings.AI_MAX_REQUESTS_PER_MINUTE = 0
        limiter = _RateLimiter()
        assert limiter.wait() == 0.0
        assert limiter.wait() == 0.0

    def test_first_call_is_never_delayed(self, settings):
        settings.AI_MAX_REQUESTS_PER_MINUTE = 60
        assert _RateLimiter().wait() == 0.0

    def test_spaces_subsequent_calls(self, settings):
        settings.AI_MAX_REQUESTS_PER_MINUTE = 120      # one every 0.5s
        limiter = _RateLimiter()
        limiter.wait()
        started = time.monotonic()
        waited = limiter.wait()
        elapsed = time.monotonic() - started
        assert waited > 0.3
        assert elapsed > 0.3

    def test_the_limit_can_be_changed_without_a_restart(self, settings):
        limiter = _RateLimiter()
        settings.AI_MAX_REQUESTS_PER_MINUTE = 120
        limiter.wait()
        settings.AI_MAX_REQUESTS_PER_MINUTE = 0        # turned off mid-flight
        assert limiter.wait() == 0.0
