"""User and entity administration (S50)."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.translation import gettext_lazy as _

from apps.audit import services as audit
from apps.audit.models import AuditAction

from .models import Entity, User


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "country", "is_active")
    list_filter = ("country", "is_active")
    search_fields = ("name", "name_ar", "code")


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("email", "display_name", "role", "entity", "department", "is_active")
    list_filter = ("role", "entity", "is_active", "is_staff")
    search_fields = ("email", "first_name", "last_name", "full_name_ar")
    ordering = ("email",)

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal"), {"fields": ("first_name", "last_name", "full_name_ar")}),
        (_("AJi"), {
            "fields": ("role", "entity", "department"),
            "description": _(
                "Role governs what this user may do. It is deliberately separate "
                "from Django superuser status: administering the system is not "
                "authority to approve a contract."
            ),
        }),
        (_("Preferences"), {"fields": ("preferred_language", "preferred_theme",
                                       "sidebar_collapsed")}),
        (_("Permissions"), {"fields": ("is_active", "is_staff", "is_superuser",
                                       "groups", "user_permissions")}),
        (_("Dates"), {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2", "role", "entity"),
        }),
    )

    def save_model(self, request, obj, form, change):
        role_changed = change and "role" in form.changed_data
        previous = form.initial.get("role", "") if role_changed else ""
        super().save_model(request, obj, form, change)
        if role_changed:
            audit.record(
                AuditAction.PERMISSION_CHANGED,
                actor=request.user, entity_type="User", entity_id=obj.pk,
                previous_state=previous, new_state=obj.role,
                details={"email": obj.email},
            )
