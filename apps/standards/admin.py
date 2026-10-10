"""Standards management (§50).

Legal must be able to author, approve and supersede standards without a
developer. Three guarantees are enforced here rather than left to discipline:

1. An approved version cannot be edited — it becomes read-only in the form.
2. Approving is an explicit action that records who approved and when.
3. Anything marked as a development fixture is loudly labelled, so test data
   can never be mistaken for approved AJi policy (§49).
"""
from __future__ import annotations

from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html

from apps.audit import services as audit
from apps.audit.models import AuditAction

from .models import ClauseCategory, Standard, StandardStatus, StandardVersion


@admin.register(ClauseCategory)
class ClauseCategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "name_ar", "default_department", "is_active", "sort_order")
    list_editable = ("is_active", "sort_order")
    search_fields = ("code", "name", "name_ar")
    ordering = ("sort_order", "name")


class StandardVersionInline(admin.TabularInline):
    model = StandardVersion
    extra = 0
    fields = ("version", "status", "effective_date", "expiry_date",
              "impact", "is_demo_fixture", "approved_by")
    readonly_fields = ("approved_by",)
    ordering = ("-version",)
    show_change_link = True

    def has_delete_permission(self, request, obj=None):
        return False          # history is never removed


@admin.register(Standard)
class StandardAdmin(admin.ModelAdmin):
    list_display = ("category", "scope", "country", "contract_type",
                    "reference_template", "current_version_label", "is_active")
    list_filter = ("scope", "country", "reference_template", "is_active", "category")
    search_fields = ("title", "title_ar", "category__code")
    autocomplete_fields = ("category", "contract_type")
    inlines = [StandardVersionInline]

    @admin.display(description="In force")
    def current_version_label(self, obj):
        version = obj.current_version()
        if version is None:
            return format_html('<span style="color:#A63A2B">none approved</span>')
        label = f"v{version.version}"
        if version.is_demo_fixture:
            return format_html(
                '{} <b style="color:#A63A2B">· TEST DATA</b>', label
            )
        return label


@admin.register(StandardVersion)
class StandardVersionAdmin(admin.ModelAdmin):
    list_display = ("standard", "version", "status_label", "effective_date",
                    "expiry_date", "impact", "fixture_label")
    list_filter = ("status", "is_demo_fixture", "standard__scope", "standard__country")
    search_fields = ("standard__category__code", "preferred_position", "source_reference")
    autocomplete_fields = ("standard",)
    actions = ("approve_versions", "supersede_versions")

    fieldsets = (
        (None, {"fields": ("standard", "version", "status", "is_demo_fixture")}),
        ("Position", {
            "fields": ("preferred_position", "preferred_position_ar",
                       "acceptable_range", "acceptable_range_ar", "red_flags", "red_flags_ar",
                       "approved_fallback_wording", "approved_fallback_wording_ar",
                       "exceptions"),
        }),
        ("Deterministic rule", {
            "description": (
                "Machine-checkable comparison, evaluated in Python — never by a "
                "language model. Example: "
                '{"key": "ld_cap_percent", "extract": "percentage", '
                '"operator": "lte", "value": 10}'
            ),
            "fields": ("rule_definition", "impact"),
        }),
        ("Governance", {
            "fields": ("approval_authority", "responsible_department",
                       "source_reference", "effective_date", "expiry_date",
                       "change_note", "approved_by", "approved_at"),
        }),
    )
    readonly_fields = ("approved_by", "approved_at")

    @admin.display(description="Status")
    def status_label(self, obj):
        colours = {
            StandardStatus.APPROVED: "#0a7a3d",
            StandardStatus.DRAFT: "#626262",
            StandardStatus.SUPERSEDED: "#8a6d00",
            StandardStatus.WITHDRAWN: "#A63A2B",
        }
        return format_html(
            '<b style="color:{}">{}</b>',
            colours.get(obj.status, "#000"), obj.get_status_display(),
        )

    @admin.display(description="Source")
    def fixture_label(self, obj):
        if obj.is_demo_fixture:
            return format_html(
                '<b style="color:#A63A2B">TEST — NOT APPROVED POLICY</b>'
            )
        return "AJi policy"

    def get_readonly_fields(self, request, obj=None):
        """An approved version is read-only. Corrections create a new version."""
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.is_locked:
            editable = {"status", "expiry_date"}
            for field_set in self.fieldsets:
                readonly.extend(
                    name for name in field_set[1]["fields"] if name not in editable
                )
        return tuple(dict.fromkeys(readonly))

    def has_delete_permission(self, request, obj=None):
        # Deleting an approved standard would orphan the findings that cite it.
        if obj is not None and obj.is_locked:
            return False
        return super().has_delete_permission(request, obj)

    # ── actions ──────────────────────────────────────────────────────────
    @admin.action(description="Approve selected versions")
    def approve_versions(self, request, queryset):
        approved = skipped = 0
        for version in queryset:
            if version.status == StandardStatus.APPROVED:
                skipped += 1
                continue
            if not version.effective_date:
                self.message_user(
                    request,
                    f"{version} has no effective date — it cannot be approved.",
                    level=messages.ERROR,
                )
                skipped += 1
                continue

            version.status = StandardStatus.APPROVED
            version.approved_by = request.user
            version.approved_at = timezone.now()
            version.save(update_fields=[
                "status", "approved_by", "approved_at", "updated_at",
            ])
            audit.record(
                AuditAction.STANDARD_APPROVED,
                actor=request.user,
                entity_type="StandardVersion",
                entity_id=version.pk,
                new_state=StandardStatus.APPROVED,
                details={
                    "standard": str(version.standard),
                    "version": version.version,
                    "is_demo_fixture": version.is_demo_fixture,
                },
            )
            approved += 1

        if approved:
            self.message_user(request, f"Approved {approved} version(s).")
        if skipped:
            self.message_user(
                request, f"Skipped {skipped}.", level=messages.WARNING
            )

    @admin.action(description="Supersede selected versions")
    def supersede_versions(self, request, queryset):
        count = 0
        for version in queryset.filter(status=StandardStatus.APPROVED):
            version.status = StandardStatus.SUPERSEDED
            version.expiry_date = version.expiry_date or timezone.localdate()
            version.save(update_fields=["status", "expiry_date", "updated_at"])
            audit.record(
                AuditAction.STANDARD_SUPERSEDED,
                actor=request.user,
                entity_type="StandardVersion",
                entity_id=version.pk,
                previous_state=StandardStatus.APPROVED,
                new_state=StandardStatus.SUPERSEDED,
                details={"standard": str(version.standard), "version": version.version},
            )
            count += 1
        self.message_user(request, f"Superseded {count} version(s).")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            audit.record(
                AuditAction.STANDARD_CREATED,
                actor=request.user,
                entity_type="StandardVersion",
                entity_id=obj.pk,
                details={"standard": str(obj.standard), "version": obj.version},
            )
