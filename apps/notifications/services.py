"""Notification dispatch (§42).

Channels are modular. `notify()` always writes the in-app record first, then
attempts each configured channel. A channel failure is logged and never
propagates — a mail server being down must not roll back the contract
transition that triggered the message.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction

from .models import Notification, NotificationKind

logger = logging.getLogger(__name__)


@dataclass
class Channel:
    """A delivery mechanism. Adding Teams or SMS means adding one of these."""

    name: str
    enabled: bool

    def send(self, notification: Notification) -> bool:  # pragma: no cover - base
        raise NotImplementedError


class EmailChannel(Channel):
    def __init__(self):
        super().__init__(name="email", enabled=True)

    def send(self, notification: Notification) -> bool:
        recipient = notification.recipient
        if not recipient.email or not recipient.is_active:
            return False
        try:
            send_mail(
                subject=notification.title,
                # Deliberately terse: contract text never goes out by email.
                message=(
                    f"{notification.body}\n\n"
                    "Open Sanad to view the details."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Email notification failed for %s", notification.pk)
            return False

        from django.utils import timezone

        notification.emailed_at = timezone.now()
        notification.save(update_fields=["emailed_at"])
        return True


def _channels() -> list[Channel]:
    if getattr(settings, "NOTIFY_EMAIL_ENABLED", True):
        return [EmailChannel()]
    return []


def notify(
    *,
    recipient,
    kind: str,
    title: str,
    body: str = "",
    url: str = "",
    contract=None,
    dedupe_key: str = "",
) -> Notification | None:
    """Create one notification and attempt delivery.

    Returns None when the message was suppressed as a duplicate.
    """
    if recipient is None or not getattr(recipient, "is_active", False):
        return None

    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                recipient=recipient, kind=kind, title=title, body=body,
                url=url, contract=contract, dedupe_key=dedupe_key,
            )
    except IntegrityError:
        # Same recipient, same dedupe key — already told them.
        return None

    for channel in _channels():
        if channel.enabled:
            channel.send(notification)
    return notification


def notify_many(recipients, **kwargs) -> list[Notification]:
    seen: set[int] = set()
    out: list[Notification] = []
    for recipient in recipients:
        if recipient is None or recipient.pk in seen:
            continue
        seen.add(recipient.pk)
        created = notify(recipient=recipient, **kwargs)
        if created:
            out.append(created)
    return out


# ── role lookups ─────────────────────────────────────────────────────────

def users_with_role(role: str, *, entity=None):
    from apps.accounts.models import User

    qs = User.objects.filter(role=role, is_active=True)
    if entity is not None:
        qs = qs.filter(entity=entity)
    return qs


# ── event helpers, called from services rather than views ────────────────

def analysis_completed(run) -> None:
    contract = run.contract
    unresolved = run.unresolved_count
    title = f"Analysis complete — {contract.project_name}"
    body = (
        f"{run.high_count} high, {run.medium_count} medium, {run.low_count} low."
        + (f" {unresolved} finding(s) need a human decision." if unresolved else "")
    )
    notify_many(
        [contract.uploaded_by, contract.reviewer],
        kind=NotificationKind.ANALYSIS_COMPLETED,
        title=title, body=body, contract=contract,
        url=f"/analysis/{run.pk}/",
        dedupe_key=f"analysis-complete:{run.pk}",
    )


def analysis_failed(run, error: str) -> None:
    contract = run.contract
    notify_many(
        [contract.uploaded_by, contract.reviewer],
        kind=NotificationKind.ANALYSIS_FAILED,
        title=f"Analysis failed — {contract.project_name}",
        body=error[:500], contract=contract,
        url=f"/contracts/{contract.pk}/",
        dedupe_key=f"analysis-failed:{run.pk}",
    )


def source_conflict(contract, count: int) -> None:
    """OCR and vision disagreed — nobody may pick a reading silently."""
    notify_many(
        [contract.uploaded_by, contract.reviewer],
        kind=NotificationKind.SOURCE_CONFLICT,
        title=f"Source conflict — {contract.project_name}",
        body=(
            f"{count} region(s) where OCR and vision verification disagree. "
            "The original text has not been altered; a human must decide."
        ),
        contract=contract, url=f"/contracts/{contract.pk}/",
        dedupe_key=f"source-conflict:{contract.pk}",
    )


def facts_to_confirm(contract, count: int) -> None:
    """The file was scanned, or its figures disagree: the key facts wait for a
    reviewer before the analysis runs."""
    notify_many(
        [contract.uploaded_by, contract.reviewer],
        kind=NotificationKind.SOURCE_CONFLICT,
        title=f"Confirm the key facts — {contract.project_name}",
        body=(
            f"The file has been read. {count} key figure(s) need a look before the analysis "
            "starts; confirm them and the pages to analyse."
            if count else
            "The file has been read. Confirm the key figures and the pages to analyse, "
            "then the analysis starts."
        ),
        contract=contract, url=f"/contracts/{contract.pk}/confirm/",
        dedupe_key=f"facts-to-confirm:{contract.pk}",
    )


def review_requested(contract, *, role: str, kind: str, title: str) -> None:
    notify_many(
        users_with_role(role),
        kind=kind, title=title,
        body=f"{contract.project_name} — {contract.client_name}",
        contract=contract, url=f"/workflow/?contract={contract.pk}",
        dedupe_key=f"{kind}:{contract.pk}:{contract.status}",
    )


def expiry_approaching(contract, days: int) -> None:
    notify_many(
        [contract.reviewer, contract.uploaded_by],
        kind=NotificationKind.EXPIRY_APPROACHING,
        title=f"Expires in {days} days — {contract.project_name}",
        body=f"Expiry date {contract.expiry_date}. Client: {contract.client_name}.",
        contract=contract, url=f"/contracts/{contract.pk}/",
        # One notice per threshold per contract, not one per nightly run.
        dedupe_key=f"expiry:{contract.pk}:{days}",
    )


def contract_expired(contract) -> None:
    notify_many(
        [contract.reviewer, contract.uploaded_by],
        kind=NotificationKind.CONTRACT_EXPIRED,
        title=f"Expired — {contract.project_name}",
        body=f"Expiry date {contract.expiry_date} has passed.",
        contract=contract, url=f"/contracts/{contract.pk}/",
        dedupe_key=f"expired:{contract.pk}",
    )
