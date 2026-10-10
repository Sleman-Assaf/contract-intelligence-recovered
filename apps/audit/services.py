"""The only supported way to write an audit entry (§35).

The actor, IP and request id are taken from the request context bound by
`AuditContextMiddleware` — never from anything a caller passes in from the
client side.
"""
from __future__ import annotations

import logging
from typing import Any

from .middleware import get_audit_context
from .models import AuditEvent

logger = logging.getLogger(__name__)

# Keys whose values must never reach the audit table. Audit records what
# happened, not the confidential text it happened to (§45).
_REDACTED_KEYS = {
    "password", "token", "api_key", "secret", "authorization",
    "original_wording", "clause_text", "source_text", "document_text",
    "prompt", "raw_response",
}


def _scrub(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    clean: dict[str, Any] = {}
    for key, value in details.items():
        if key.lower() in _REDACTED_KEYS:
            clean[key] = "[redacted]"
        elif isinstance(value, dict):
            clean[key] = _scrub(value)
        elif isinstance(value, str) and len(value) > 500:
            # Never let a long body slip into the trail by accident.
            clean[key] = value[:500] + f"… [truncated {len(value) - 500} chars]"
        else:
            clean[key] = value
    return clean


def record(
    action: str,
    *,
    actor=None,
    entity_type: str = "",
    entity_id: str | int = "",
    contract_id: str | int = "",
    previous_state: str = "",
    new_state: str = "",
    details: dict[str, Any] | None = None,
) -> AuditEvent | None:
    """Append one audit entry.

    Never raises: an audit failure must not roll back the business operation
    that succeeded. It is logged loudly instead.
    """
    ctx = get_audit_context()
    actor = actor or ctx.get("actor")

    try:
        return AuditEvent.objects.create(
            action=action,
            actor=actor,
            actor_email=getattr(actor, "email", "") or "",
            actor_role=getattr(actor, "role", "") or "",
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else "",
            contract_id=str(contract_id) if contract_id else "",
            previous_state=previous_state or "",
            new_state=new_state or "",
            details=_scrub(details),
            ip_address=ctx.get("ip_address"),
            user_agent=ctx.get("user_agent", ""),
            request_id=ctx.get("request_id", ""),
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to write audit event action=%s entity=%s", action, entity_id)
        return None
