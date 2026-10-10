"""Backend-enforced authorisation (§34).

Two layers, and both are required:

* **Capability checks** — may this role perform this action at all?
* **Queryset scoping** — which rows may this user see?

Hiding a button in a template is presentation. It is never protection, so no
view in this project decides access by reading the template context.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet
from django.utils.translation import gettext as _

from .models import Role, User

# ── Capabilities ─────────────────────────────────────────────────────────
# Named actions rather than model permissions, because authority here is about
# business decisions ("approve a contract"), not table access.

VIEW_ALL_CONTRACTS = "view_all_contracts"
UPLOAD_CONTRACT = "upload_contract"
DOWNLOAD_SOURCE_FILE = "download_source_file"
RUN_ANALYSIS = "run_analysis"
DECIDE_FINDING = "decide_finding"
LEGAL_DECISION = "legal_decision"
MANAGEMENT_DECISION = "management_decision"
FINAL_APPROVAL = "final_approval"
ARCHIVE_CONTRACT = "archive_contract"
MANAGE_STANDARDS = "manage_standards"
APPROVE_STANDARDS = "approve_standards"
VIEW_AUDIT_LOG = "view_audit_log"
MANAGE_USERS = "manage_users"
EXPORT_REPORT = "export_report"

ROLE_CAPABILITIES: dict[str, set[str]] = {
    Role.ADMIN: {
        VIEW_ALL_CONTRACTS, UPLOAD_CONTRACT, DOWNLOAD_SOURCE_FILE, RUN_ANALYSIS,
        VIEW_AUDIT_LOG, MANAGE_USERS, MANAGE_STANDARDS, EXPORT_REPORT,
        # Deliberately absent: FINAL_APPROVAL, LEGAL_DECISION,
        # MANAGEMENT_DECISION. Administering the system is not the same thing
        # as holding commercial or legal authority over a contract.
    },
    Role.CONTRACT_REVIEWER: {
        VIEW_ALL_CONTRACTS, UPLOAD_CONTRACT, DOWNLOAD_SOURCE_FILE, RUN_ANALYSIS,
        DECIDE_FINDING, EXPORT_REPORT,
    },
    Role.LEGAL_REVIEWER: {
        VIEW_ALL_CONTRACTS, DOWNLOAD_SOURCE_FILE, DECIDE_FINDING,
        LEGAL_DECISION, MANAGE_STANDARDS, EXPORT_REPORT,
    },
    Role.MANAGEMENT: {
        VIEW_ALL_CONTRACTS, DOWNLOAD_SOURCE_FILE, MANAGEMENT_DECISION,
        FINAL_APPROVAL, ARCHIVE_CONTRACT, EXPORT_REPORT,
    },
    Role.STANDARD_MANAGER: {
        MANAGE_STANDARDS, APPROVE_STANDARDS, VIEW_ALL_CONTRACTS,
    },
    Role.USER: {
        UPLOAD_CONTRACT,          # own contracts only — see scope_contracts()
    },
}


def capabilities_for(user: User) -> set[str]:
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return set()
    return ROLE_CAPABILITIES.get(user.role, set())


def has_capability(user: User, capability: str) -> bool:
    return capability in capabilities_for(user)


def require_capability(user: User, capability: str) -> None:
    """Raise unless the user holds the capability. Use at the top of a service."""
    if not has_capability(user, capability):
        # Shown to the user as it stands (workflow and decision views flash
        # it), so it names the role as the site does and leaves out the
        # internal capability code.
        role = getattr(user, "role", "")
        raise PermissionDenied(
            _("لا يملك دورك (%(role)s) صلاحية تنفيذ هذا الإجراء.") % {
                "role": Role(role).label if role in Role.values else _("زائر")}
        )


# ── Row scoping ──────────────────────────────────────────────────────────

def scope_contracts(user: User, queryset: QuerySet) -> QuerySet:
    """Restrict a Contract queryset to what this user may see.

    Every list, dashboard aggregate and detail view must pass through here.
    Filtering in the template is not filtering.
    """
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return queryset.none()

    if has_capability(user, VIEW_ALL_CONTRACTS):
        return queryset

    # Everyone else sees only what they uploaded or were assigned.
    return queryset.filter(Q(uploaded_by=user) | Q(reviewer=user))


def can_view_contract(user: User, contract) -> bool:
    if has_capability(user, VIEW_ALL_CONTRACTS):
        return True
    return contract.uploaded_by_id == user.pk or contract.reviewer_id == user.pk


def require_contract_access(user: User, contract) -> None:
    if not can_view_contract(user, contract):
        raise PermissionDenied(_("ليس لديك صلاحية الوصول إلى هذا العقد."))


# ── Separation of duties ─────────────────────────────────────────────────

def can_give_final_approval(user: User, contract) -> bool:
    """The uploader may never approve their own contract.

    This was the single largest governance gap in the previous system, where
    any signed-in user could both upload and approve.
    """
    if not has_capability(user, FINAL_APPROVAL):
        return False
    if contract.uploaded_by_id == user.pk:
        return False
    return True


def require_final_approval_rights(user: User, contract) -> None:
    if not has_capability(user, FINAL_APPROVAL):
        raise PermissionDenied(_("لا يملك هذا الدور صلاحية الاعتماد النهائي."))
    if contract.uploaded_by_id == user.pk:
        raise PermissionDenied(_(
            "فصل المهام: من رفع العقد لا يمكنه اعتماده. يجب أن يأتي الاعتماد "
            "النهائي من شخص آخر."
        ))
