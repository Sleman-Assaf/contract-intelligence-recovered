"""Reuse of validated model answers for identical analysis prompts.

See AIResponseCache. Only the analysis pipeline's calls are cached: they are
deterministic readings of a clause against a standard. The advisor's chat is
not -- a conversation is not meant to repeat itself.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

CACHEABLE = frozenset({
    "classify_clause", "classify_clauses", "assess_clause", "verify_finding",
    "translate_prose", "translate_clause",
})


def enabled(request_type: str) -> bool:
    return bool(getattr(settings, "AI_RESPONSE_CACHE", False)) and request_type in CACHEABLE


def key_for(model: str, request_type: str, prompt: str) -> str:
    return hashlib.sha256(f"{model}\x00{request_type}\x00{prompt}".encode("utf-8")).hexdigest()


def lookup(key: str) -> str | None:
    """The stored answer for `key`, if it is still within the retention."""
    from .models import AIResponseCache

    days = getattr(settings, "AI_RESPONSE_CACHE_DAYS", 90)
    try:
        row = (AIResponseCache.objects
               .filter(key=key, created_at__gte=timezone.now() - timedelta(days=days))
               .only("pk", "raw_text").first())
        if row is None:
            return None
        AIResponseCache.objects.filter(pk=row.pk).update(hits=F("hits") + 1, last_used_at=timezone.now())
        return row.raw_text
    except Exception:                                  # a cache must never stop the work
        logger.warning("AI response cache lookup failed", exc_info=True)
        return None


def store(key: str, *, model: str, request_type: str, raw_text: str) -> None:
    from .models import AIResponseCache

    try:
        AIResponseCache.objects.update_or_create(
            key=key, defaults={"model": model, "request_type": request_type,
                               "raw_text": raw_text, "created_at": timezone.now()})
    except Exception:
        logger.warning("AI response cache store failed", exc_info=True)


def forget(key: str) -> None:
    from .models import AIResponseCache

    try:
        AIResponseCache.objects.filter(key=key).delete()
    except Exception:
        logger.warning("AI response cache delete failed", exc_info=True)
