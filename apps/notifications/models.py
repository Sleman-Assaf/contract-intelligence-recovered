"""Notifications (§42).

Delivery channels are pluggable: an in-app record is always written, and
additional channels (email, and later Teams or SMS) are layered on top. The
record is the source of truth, so a channel outage never loses the event.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import UUIDPrimaryKeyModel


class NotificationKind(models.TextChoices):
    ANALYSIS_COMPLETED = "analysis.completed", _("Analysis completed")
    ANALYSIS_FAILED = "analysis.failed", _("Analysis failed")
    SOURCE_CONFLICT = "source.conflict", _("Source conflict")
    SYSTEM_AI_CONFLICT = "system.ai.conflict", _("System / AI conflict")
    CONTRACT_ASSIGNED = "contract.assigned", _("Contract assigned")
    LEGAL_REVIEW_REQUESTED = "legal.review.requested", _("Legal review requested")
    MANAGEMENT_APPROVAL_REQUIRED = "management.approval.required", _("Management approval required")
    RETURNED_FOR_REVISION = "contract.returned", _("Returned for revision")
    EXPIRY_APPROACHING = "contract.expiry.approaching", _("Expiry approaching")
    CONTRACT_EXPIRED = "contract.expired", _("Contract expired")


class Notification(UUIDPrimaryKeyModel):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    kind = models.CharField(max_length=48, choices=NotificationKind.choices, db_index=True)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    url = models.CharField(max_length=512, blank=True)

    contract = models.ForeignKey(
        "contracts.Contract", null=True, blank=True,
        on_delete=models.CASCADE, related_name="notifications",
    )

    #: Collapses repeat sends of the same event — a nightly expiry job must not
    #: notify the same person about the same contract every single night.
    dedupe_key = models.CharField(max_length=160, blank=True, db_index=True)

    read_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "read_at", "-created_at"]),
            models.Index(fields=["dedupe_key"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=~models.Q(dedupe_key=""),
                name="uniq_notification_dedupe_per_recipient",
            )
        ]

    def __str__(self) -> str:
        return f"{self.kind} → {self.recipient_id}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_read(self) -> None:
        if self.read_at is None:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at"])
