"""Server-side audit trail (§35).

Every entry is written by backend code. The actor is taken from the
authenticated request, never from anything the client sends.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.models import UUIDPrimaryKeyModel


class AuditAction(models.TextChoices):
    # access
    LOGIN = "login", _("Login")
    LOGIN_FAILED = "login_failed", _("Login failed")
    LOGOUT = "logout", _("Logout")
    # contract lifecycle
    CONTRACT_UPLOADED = "contract.uploaded", _("Contract uploaded")
    CONTRACT_DOWNLOADED = "contract.downloaded", _("Contract downloaded")
    CONTRACT_UPDATED = "contract.updated", _("Contract updated")
    CONTRACT_DELETED = "contract.deleted", _("Contract deleted")
    # pipeline
    ANALYSIS_STARTED = "analysis.started", _("Analysis started")
    ANALYSIS_COMPLETED = "analysis.completed", _("Analysis completed")
    ANALYSIS_FAILED = "analysis.failed", _("Analysis failed")
    OCR_RETRY = "ocr.retry", _("OCR retry")
    VISION_VERIFICATION = "vision.verification", _("Vision verification")
    SOURCE_CONFLICT = "source.conflict", _("Source conflict raised")
    # standards
    STANDARD_SELECTED = "standard.selected", _("Standard selected")
    STANDARD_CREATED = "standard.created", _("Standard created")
    STANDARD_APPROVED = "standard.approved", _("Standard approved")
    STANDARD_SUPERSEDED = "standard.superseded", _("Standard superseded")
    # review
    FINDING_ACCEPTED = "finding.accepted", _("Finding accepted")
    FINDING_REJECTED = "finding.rejected", _("Finding rejected")
    FINDING_ESCALATED = "finding.escalated", _("Finding escalated")
    LEGAL_DECISION = "legal.decision", _("Legal decision")
    MANAGEMENT_DECISION = "management.decision", _("Management decision")
    STATUS_TRANSITION = "workflow.transition", _("Status transition")
    # output
    REPORT_GENERATED = "report.generated", _("Report generated")
    # administration
    PERMISSION_CHANGED = "user.permission_changed", _("User permission changed")
    USER_REGISTERED = "user.registered", _("User registered")
    USER_ACTIVATED = "user.activated", _("User activated")
    USER_DEACTIVATED = "user.deactivated", _("User deactivated")
    USER_DELETED = "user.deleted", _("User deleted")
    ADVISOR_MEMORY_CLEARED = "advisor.memory_cleared", _("Advisor memory cleared")


class AuditEvent(UUIDPrimaryKeyModel):
    """An immutable record of something that happened.

    There is deliberately no update path: rows are appended, never edited.
    """

    action = models.CharField(max_length=64, choices=AuditAction.choices, db_index=True)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="audit_events",
    )
    actor_email = models.EmailField(
        blank=True,
        help_text=_("Captured at write time so the trail survives user deletion."),
    )
    actor_role = models.CharField(max_length=32, blank=True)

    entity_type = models.CharField(max_length=64, blank=True, db_index=True)
    entity_id = models.CharField(max_length=64, blank=True, db_index=True)
    contract_id = models.CharField(max_length=64, blank=True, db_index=True)

    previous_state = models.CharField(max_length=64, blank=True)
    new_state = models.CharField(max_length=64, blank=True)

    details = models.JSONField(default=dict, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=320, blank=True)
    request_id = models.CharField(max_length=64, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["contract_id", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} by {self.actor_email or 'system'}"

    def save(self, *args, **kwargs):
        # Same trap as StandardVersion: a UUID pk exists before the first save,
        # so newness must be read from `_state.adding`.
        if not self._state.adding:
            raise ValueError("Audit events are immutable and cannot be modified.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit events cannot be deleted.")
