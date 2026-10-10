"""Central provider selection (§46).

Business code calls `get_ai_provider()` and never constructs a vendor client.
Switching production backends is an environment change, not a code change.
"""
from __future__ import annotations

from functools import lru_cache

from django.conf import settings

from .providers.base import AIProvider, AIProviderNotConfigured

_REGISTRY: dict[str, str] = {
    "ajibrain": "apps.ai.providers.ajibrain.AjiBrainProvider",
}


def _load(path: str) -> type[AIProvider]:
    from django.utils.module_loading import import_string

    return import_string(path)


@lru_cache(maxsize=None)
def _cached_provider(name: str) -> AIProvider:
    try:
        dotted = _REGISTRY[name]
    except KeyError as exc:
        raise AIProviderNotConfigured(
            f"Unknown AI_PROVIDER '{name}'. Known providers: {', '.join(sorted(_REGISTRY))}."
        ) from exc
    return _load(dotted)()


def get_ai_provider(name: str | None = None) -> AIProvider:
    """Return the configured provider instance."""
    return _cached_provider(name or settings.AI_PROVIDER)


def available_providers() -> dict[str, dict]:
    """Status of every registered provider — for the admin/system page."""
    out: dict[str, dict] = {}
    for name in _REGISTRY:
        provider = _cached_provider(name)
        ok, detail = provider.health_check()
        out[name] = {
            "configured": provider.is_configured(),
            "healthy": ok,
            "detail": detail,
            "active": name == settings.AI_PROVIDER,
        }
    return out


def reset_provider_cache() -> None:
    """Used by tests and after a settings change."""
    _cached_provider.cache_clear()
