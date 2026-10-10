"""Server-enforced workflow transitions (§27, §30, §36).

Three rules, all enforced here rather than in a template:

1. Only a permitted transition may happen, from the stage the contract is
   actually in.
2. Only a role with the authority may perform it.
3. The uploader can never give final approval to their own contract.

Every transition writes stage, contract status, history and audit inside one
`transaction.atomic()` block — they cannot drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext_lazy as _
from django.db import transaction
from django.utils import timezone

from apps.accounts import permissions as perms
from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.contracts.models import ContractStatus

from .models import ApprovalRule, Workflow, WorkflowComment, WorkflowStage, WorkflowTransition


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    to_stage: int
    to_status: str
    capability: str
    from_stages: tuple[int, ...]


#: The permitted moves. Anything not listed here cannot happen.
ACTIONS: dict[str, Action] = {
    "send_to_legal": Action(
        "send_to_legal", _("Send to legal review"),
        WorkflowStage.LEGAL_REVIEW, ContractStatus.LEGAL_REVIEW,
        perms.DECIDE_FINDING, (WorkflowStage.CONTRACT_REVIEW,),
    ),
    "legal_complete": Action(
        "legal_complete", _("Complete legal review"),
        WorkflowStage.MANAGEMENT_REVIEW, ContractStatus.MANAGEMENT_REVIEW,
        perms.LEGAL_DECISION, (WorkflowStage.LEGAL_REVIEW,),
    ),
    "management_decision": Action(
        "management_decision", _("Record management decision"),
        WorkflowStage.FINAL_APPROVAL, ContractStatus.MANAGEMENT_REVIEW,
        perms.MANAGEMENT_DECISION, (WorkflowStage.MANAGEMENT_REVIEW,),
    ),
    "approve": Action(
        "approve", _("Final approval"),
        WorkflowStage.FINAL_APPROVAL, ContractStatus.APPROVED,
        perms.FINAL_APPROVAL,
        (WorkflowStage.MANAGEMENT_REVIEW, WorkflowStage.FINAL_APPROVAL),
    ),
    "reject": Action(
        "reject", _("Reject contract"),
        WorkflowStage.FINAL_APPROVAL, ContractStatus.REJECTED,
        perms.MANAGEMENT_DECISION,
        (WorkflowStage.LEGAL_REVIEW, WorkflowStage.MANAGEMENT_REVIEW,
         WorkflowStage.FINAL_APPROVAL),
    ),
    "return_for_revision": Action(
        "return_for_revision", _("Return for revision"),
        WorkflowStage.CONTRACT_REVIEW, ContractStatus.RETURNED_FOR_REVISION,
        perms.LEGAL_DECISION,
        (WorkflowStage.LEGAL_REVIEW, WorkflowStage.MANAGEMENT_REVIEW),
    ),
    "archive": Action(
        "archive", _("Archive"),
        WorkflowStage.ARCHIVED, ContractStatus.ARCHIVED,
        perms.ARCHIVE_CONTRACT, (WorkflowStage.FINAL_APPROVAL,),
    ),
}


def get_or_create_workflow(contract) -> Workflow:
    workflow, _ = Workflow.objects.get_or_create(
        contract=contract,
        defaults={"current_stage": WorkflowStage.CONTRACT_REVIEW},
    )
    return workflow


def available_actions(user, contract) -> list[Action]:
    """What this user may actually do right now.

    Templates use this to render buttons — but the same checks run again in
    `transition()`, because a hidden button is not a permission.
    """
    workflow = get_or_create_workflow(contract)
    out = []
    for action in ACTIONS.values():
        if workflow.current_stage not in action.from_stages:
            continue
        if not perms.has_capability(user, action.capability):
            continue
        if action.key == "approve" and not perms.can_give_final_approval(user, contract):
            continue
        out.append(action)
    return out


@transaction.atomic
def transition(*, contract, user, action_key: str, note: str = "") -> WorkflowTransition:
    """Move a contract to its next stage. Atomic, validated and audited."""
    try:
        action = ACTIONS[action_key]
    except KeyError as exc:
        raise ValidationError(_("إجراء غير معروف في مسار المراجعة.")) from exc

    # Lock the contract row so two reviewers cannot transition it at once.
    contract = type(contract).objects.select_for_update().get(pk=contract.pk)
    workflow = get_or_create_workflow(contract)

    # Authorisation first: an unauthorised user should not learn which stage
    # the contract is in from the shape of the error.
    perms.require_capability(user, action.capability)

    if workflow.current_stage not in action.from_stages:
        raise ValidationError(
            _("الإجراء «%(action)s» غير متاح في مرحلة «%(stage)s».") % {
                "action": action.label, "stage": workflow.get_current_stage_display()}
        )

    if action.key == "approve":
        # Separation of duties, enforced at the point of action.
        perms.require_final_approval_rights(user, contract)

    if action.key in {"approve", "management_decision"}:
        _require_complete_analysis(contract)
        _require_escalations_resolved(contract)

    from_stage = workflow.current_stage
    from_status = contract.status

    workflow.current_stage = action.to_stage
    workflow.last_action = action.label
    workflow.save(update_fields=["current_stage", "last_action", "updated_at"])

    contract.status = action.to_status
    fields = ["status", "updated_at"]
    if action.to_status == ContractStatus.APPROVED:
        contract.approved_at = timezone.now()
        fields.append("approved_at")
    if action.to_status == ContractStatus.ARCHIVED:
        contract.archived_at = timezone.now()
        fields.append("archived_at")
    contract.save(update_fields=fields)

    record = WorkflowTransition.objects.create(
        workflow=workflow,
        contract=contract,
        from_stage=from_stage,
        to_stage=action.to_stage,
        from_status=from_status,
        to_status=action.to_status,
        action=action.key,
        actor=user,
        actor_role=user.role,
        note=note,
    )

    _notify_next_stage(contract, action)

    audit.record(
        AuditAction.STATUS_TRANSITION,
        actor=user,
        entity_type="Contract",
        entity_id=contract.pk,
        contract_id=contract.pk,
        previous_state=from_status,
        new_state=action.to_status,
        details={"action": action.key, "note": note[:500]},
    )
    return record


#: Who needs to know once a contract reaches a stage.
_STAGE_AUDIENCE = {
    "send_to_legal": ("legal_reviewer", "legal.review.requested", "Legal review requested"),
    "legal_complete": ("management", "management.approval.required", "Management approval required"),
    "return_for_revision": (None, "contract.returned", "Returned for revision"),
}


def _notify_next_stage(contract, action) -> None:
    """Tell the people whose turn it now is (§42)."""
    from apps.notifications import services as notify

    mapping = _STAGE_AUDIENCE.get(action.key)
    if mapping is None:
        return
    role, kind, title = mapping

    if role is None:
        notify.notify_many(
            [contract.uploaded_by, contract.reviewer],
            kind=kind, title=f"{title} — {contract.project_name}",
            body=contract.client_name, contract=contract,
            url=f"/workflow/?contract={contract.pk}",
            dedupe_key=f"{kind}:{contract.pk}:{contract.status}",
        )
        return

    notify.review_requested(
        contract, role=role, kind=kind, title=f"{title} — {contract.project_name}"
    )


def _require_complete_analysis(contract) -> None:
    """Approval may not rest on an analysis that never read the contract.

    A degraded run produces few findings, so every other guard here passes
    vacuously: the mandatory-escalation check finds nothing blocking precisely
    because the clauses it cares about were never classified. Silence from a
    broken analysis must not be read as a clean result (§30, §54).

    Re-run the analysis. If it must be approved regardless, that is a decision
    a human records explicitly, not one the system makes by omission.
    """
    from apps.analysis.models import AnalysisStatus

    run = contract.current_analysis
    if run is None or run.status != AnalysisStatus.DEGRADED:
        return

    # The stored `gaps` sentences are English; the counts beside them are
    # what is said to the reader. Older runs without the counts keep theirs.
    breakdown = run.confidence_breakdown or {}
    gaps = breakdown.get("gaps") or []
    if "unclassified_clauses" in breakdown:
        unclassified = breakdown.get("unclassified_clauses") or 0
        failed = (breakdown.get("ai_failures") or 0) - unclassified
        gaps = []
        if unclassified:
            gaps.append(_("تعذّر تصنيف %(n)s بند") % {"n": unclassified})
        if failed > 0:
            gaps.append(_("فشل %(n)s طلب للذكاء الاصطناعي") % {"n": failed})
    raise ValidationError(
        _("آخر تحليل لهذا العقد غير مكتمل%(gaps)s. قد تكون بعض الملاحظات ناقصة، "
          "وقد تكون البنود المُبلَّغ عن غيابها لم تُقرأ أصلاً. أعد تشغيل التحليل "
          "قبل الاعتماد.") % {"gaps": f" ({'; '.join(gaps)})" if gaps else ""}
    )


def _require_escalations_resolved(contract) -> None:
    """Mandatory escalations must be dealt with before approval (§30)."""
    run = contract.current_analysis
    if run is None:
        return

    mandatory = {
        rule.category_id
        for rule in ApprovalRule.objects.filter(is_active=True, is_mandatory_escalation=True)
        if rule.category_id
    }
    if not mandatory:
        return

    blocking = run.findings.filter(
        category_id__in=mandatory,
    ).exclude(decisions__decision__in=["accept", "policy_exception"]).exists()

    if blocking:
        raise ValidationError(_(
            "في هذا العقد ملاحظات تخضع لقاعدة تصعيد إلزامي ولم تُحسم بعد. يجب "
            "قبولها أو منحها استثناءً من السياسة قبل الاعتماد."
        ))


def add_comment(*, contract, user, body: str) -> WorkflowComment:
    perms.require_contract_access(user, contract)
    if not body.strip():
        raise ValidationError(_("لا يمكن أن يكون التعليق فارغاً."))

    workflow = get_or_create_workflow(contract)
    comment = WorkflowComment.objects.create(
        workflow=workflow, contract=contract, author=user,
        body=body.strip(), stage=workflow.current_stage,
    )
    audit.record(
        AuditAction.STATUS_TRANSITION,
        actor=user, entity_type="WorkflowComment", entity_id=comment.pk,
        contract_id=contract.pk, details={"action": "comment_added"},
    )
    return comment


@transaction.atomic
def record_decision(*, finding, user, decision: str, comment: str = "",
                    edited_recommendation: str = "", edited_fallback: str = ""):
    """A human decision on one finding (§28, §29).

    The model's original output stays on the Finding; a human edit is stored
    alongside it so both remain visible.
    """
    from apps.analysis.models import ReviewDecision

    contract = finding.contract
    perms.require_contract_access(user, contract)

    is_legal = decision in {"policy_exception"} or user.is_legal_reviewer
    perms.require_capability(
        user, perms.LEGAL_DECISION if is_legal else perms.DECIDE_FINDING
    )

    workflow = get_or_create_workflow(contract)
    record = ReviewDecision.objects.create(
        finding=finding,
        contract=contract,
        decision=decision,
        stage=workflow.get_current_stage_display(),
        comment=comment,
        edited_recommendation=edited_recommendation,
        edited_fallback_wording=edited_fallback,
        decided_by=user,
        decided_by_role=user.role,
    )

    audit.record(
        {
            "accept": AuditAction.FINDING_ACCEPTED,
            "reject": AuditAction.FINDING_REJECTED,
            "escalate": AuditAction.FINDING_ESCALATED,
        }.get(decision, AuditAction.LEGAL_DECISION),
        actor=user,
        entity_type="Finding",
        entity_id=finding.pk,
        contract_id=contract.pk,
        details={
            "decision": decision,
            "risk_level": finding.risk_level,
            "verification_status": finding.verification_status,
            "edited": bool(edited_recommendation or edited_fallback),
        },
    )
    return record
