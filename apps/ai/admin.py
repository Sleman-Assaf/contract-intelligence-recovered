"""AI call telemetry (S45). Read-only - these are records, not settings."""
from django.contrib import admin

from .models import AIRequestLog


@admin.register(AIRequestLog)
class AIRequestLogAdmin(admin.ModelAdmin):
    list_display = ("started_at", "provider", "model", "request_type",
                    "status", "attempts", "latency_ms",
                    "prompt_tokens", "completion_tokens")
    list_filter = ("provider", "status", "request_type")
    search_fields = ("contract_id", "model")
    date_hierarchy = "started_at"
    readonly_fields = [f.name for f in AIRequestLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
