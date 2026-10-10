"""Abstract base models shared across domains.

These are abstract only — `apps.common` is a plain package, not an installed
app, so nothing here creates a table of its own.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils import timezone


class TimeStampedModel(models.Model):
    """created_at / updated_at on every business record (§2)."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDPrimaryKeyModel(models.Model):
    """Opaque public identifier.

    Contract identifiers appear in URLs; sequential integers would leak volume
    and allow enumeration of other departments' contracts.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        return self.filter(deleted_at__isnull=False)

    def delete(self):
        """Soft-delete in bulk. Use `hard_delete()` for a real DELETE."""
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        return super().delete()


class SoftDeleteManager(models.Manager):
    """Default manager hides soft-deleted rows.

    `all_objects` on each model exposes them for audit and recovery.
    """

    def get_queryset(self) -> SoftDeleteQuerySet:
        return SoftDeleteQuerySet(self.model, using=self._db).alive()


class AllObjectsManager(models.Manager):
    def get_queryset(self) -> SoftDeleteQuerySet:
        return SoftDeleteQuerySet(self.model, using=self._db)


class SoftDeleteModel(models.Model):
    """Contract evidence is never destroyed — only withdrawn from view (§31)."""

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False, actor=None):
        self.deleted_at = timezone.now()
        self.deleted_by = actor
        self.save(update_fields=["deleted_at", "deleted_by", "updated_at"])

    def hard_delete(self, using=None, keep_parents=False):
        return super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["deleted_at", "deleted_by", "updated_at"])

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class BilingualNameMixin(models.Model):
    """`name` / `name_ar`, shown according to the reader's language.

    Reference data is stored bilingually, but templates were rendering `name`
    unconditionally, so an Arabic page showed English contract types and clause
    categories next to fully translated chrome.

    Falls back to `name` when the Arabic is missing, because a blank label is
    worse than one in the wrong language.
    """

    class Meta:
        abstract = True

    @property
    def display_name(self) -> str:
        from django.utils.translation import get_language

        if (get_language() or "").startswith("ar") and getattr(self, "name_ar", ""):
            return self.name_ar
        return self.name


#: The four owning departments, as stored on a clause category. A closed set
#: written by AJi, so it is translated rather than given another column.
DEPARTMENT_LABELS = {
    "Legal Affairs": _("الشؤون القانونية"),
    "Finance": _("الإدارة المالية"),
    "Project Management": _("إدارة المشاريع"),
    "Risk Management": _("إدارة المخاطر"),
}
