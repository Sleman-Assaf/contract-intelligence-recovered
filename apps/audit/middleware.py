"""Binds request metadata to the current thread so audit writes can pick up
the actor, IP and request id without every service taking a `request` argument.
"""
from __future__ import annotations

import contextvars
import uuid

_request_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "audit_request_ctx", default={}
)


def get_audit_context() -> dict:
    return _request_ctx.get()


def _client_ip(request):
    # X-Forwarded-For only behind a known proxy (TRUST_X_FORWARDED_FOR):
    # otherwise the audit trail would record whatever address a caller typed.
    from apps.accounts.throttle import client_ip

    return client_ip(request) or None


class AuditContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        token = _request_ctx.set({
            "actor": user if getattr(user, "is_authenticated", False) else None,
            "ip_address": _client_ip(request),
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:320],
            "request_id": request.headers.get("X-Request-ID") or uuid.uuid4().hex,
        })
        try:
            return self.get_response(request)
        finally:
            _request_ctx.reset(token)
