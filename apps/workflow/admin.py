"""Approval matrix administration (S30)."""
from django.contrib import admin

from .models import ApprovalRule, Workflow, WorkflowTransition


@admin.register(ApprovalRule)
class ApprovalRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "min_risk_level", "requires_legal",
                    "requires_management", "is_mandatory_escalation",
                    "is_active", "priority")
    list_editable = ("requires_legal", "requires_management",
                     "is_mandatory_escalation", "is_active", "priority")
    list_filter = ("is_active", "is_mandatory_escalation", "requires_legal")
    autocomplete_fields = ("category",)


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display = ("contract", "current_stage", "assigned_to", "last_action", "updated_at")
    list_filter = ("current_stage",)
    readonly_fields = ("contract",)


@admin.register(WorkflowTransition)
class WorkflowTransitionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "contract", "action", "from_status",
                    "to_status", "actor", "actor_role")
    list_filter = ("action", "actor_role")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in WorkflowTransition._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
