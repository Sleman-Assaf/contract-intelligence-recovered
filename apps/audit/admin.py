"""Audit log - readable, never writable (S35)."""
from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor_email", "actor_role",
                    "entity_type", "previous_state", "new_state")
    list_filter = ("action", "actor_role", "entity_type")
    search_fields = ("actor_email", "entity_id", "contract_id", "request_id")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
