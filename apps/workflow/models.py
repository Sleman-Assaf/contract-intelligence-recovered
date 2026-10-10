"""Workflow state, transitions and comments (§27, §36).

History is normalised into rows rather than appended to a JSON array. The
Base44 version kept `history` and `comments` as arrays updated from the
browser, so two people acting at once could silently lose an entry.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class WorkflowStage(models.IntegerChoices):
    UPLOAD = 1, _("Contract upload")
    EXTRACTION = 2, _("Extraction & verification")
    ANALYSIS = 3, _("Initial AI analysis")
    CONTRACT_REVIEW = 4, _("Contract specialist review")
    LEGAL_REVIEW = 5, _("Legal review")
    MANAGEMENT_REVIEW = 6, _("Management decision")
    FINAL_APPROVAL = 7, _("Final approval")
    ARCHIVED = 8, _("Archived")


class Workflow(UUIDPrimaryKeyModel, TimeStampedModel):
    contract = models.OneToOneField(
        "contracts.Contract", on_delete=models.CASCADE, related_name="workflow"
    )
    current_stage = models.PositiveSmallIntegerField(
        choices=WorkflowStage.choices, default=WorkflowStage.UPLOAD, db_index=True
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="assigned_workflows",
    )
    last_action = models.CharField(max_length=160, blank=True)

    class Meta:
        indexes = [models.Index(fields=["current_stage", "-updated_at"])]

    def __str__(self) -> str:
        return f"{self.contract_id} @ {self.get_current_stage_display()}"


class WorkflowTransition(UUIDPrimaryKeyModel):
    """One immutable step in a contract's history."""

    workflow = models.ForeignKey(
        Workflow, on_delete=models.CASCADE, related_name="transitions"
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="transitions"
    )
    from_stage = models.PositiveSmallIntegerField(choices=WorkflowStage.choices, null=True)
    to_stage = models.PositiveSmallIntegerField(choices=WorkflowStage.choices)
    from_status = models.CharField(max_length=32, blank=True)
    to_status = models.CharField(max_length=32, blank=True)

    action = models.CharField(max_length=64)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="workflow_transitions",
    )
    actor_role = models.CharField(max_length=32, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["contract", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.action}: {self.from_stage} → {self.to_stage}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Workflow transitions are immutable.")
        return super().save(*args, **kwargs)


class WorkflowComment(UUIDPrimaryKeyModel, TimeStampedModel):
    workflow = models.ForeignKey(
        Workflow, on_delete=models.CASCADE, related_name="comments"
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="workflow_comments"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="workflow_comments"
    )
    body = models.TextField()
    stage = models.PositiveSmallIntegerField(choices=WorkflowStage.choices, null=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["contract", "created_at"])]

    def __str__(self) -> str:
        return f"comment by {self.author_id}"


class ApprovalRule(TimeStampedModel):
    """Configurable approval matrix (§30).

    "High liability deviation -> Legal + Management" must be a row an
    administrator can change, not a constant a developer has to redeploy.
    """

    name = models.CharField(max_length=160)
    category = models.ForeignKey(
        "standards.ClauseCategory", null=True, blank=True,
        on_delete=models.CASCADE, related_name="approval_rules",
    )
    min_risk_level = models.CharField(
        max_length=16, blank=True,
        help_text=_("Applies at this risk level and above: low | medium | high"),
    )
    requires_legal = models.BooleanField(default=False)
    requires_management = models.BooleanField(default=False)
    is_mandatory_escalation = models.BooleanField(
        default=False, help_text=_("Cannot be closed at contract-review level.")
    )
    is_active = models.BooleanField(default=True)
    priority = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-priority", "name"]

    def __str__(self) -> str:
        return self.name
