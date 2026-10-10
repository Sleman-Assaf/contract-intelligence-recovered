"""Contract administration."""
from django.contrib import admin

from .models import Contract, ContractParty, ContractType


@admin.register(ContractType)
class ContractTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "name_ar", "is_active", "sort_order")
    list_editable = ("is_active", "sort_order")
    search_fields = ("code", "name", "name_ar")
    ordering = ("sort_order", "name")


class ContractPartyInline(admin.TabularInline):
    model = ContractParty
    extra = 0


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ("reference", "project_name", "client_name", "country",
                    "status", "risk_level", "created_at")
    list_filter = ("status", "risk_level", "country", "contract_type", "entity")
    search_fields = ("reference", "project_name", "client_name", "contract_title")
    date_hierarchy = "created_at"
    autocomplete_fields = ("contract_type", "uploaded_by", "reviewer")
    inlines = [ContractPartyInline]
    readonly_fields = ("submitted_at", "approved_at", "archived_at", "current_analysis")

    def get_queryset(self, request):
        # Include soft-deleted rows so an administrator can recover one.
        return Contract.all_objects.all()
