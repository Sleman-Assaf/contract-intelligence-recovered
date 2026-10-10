"""Users, roles and AJi entities.

RBAC is enforced in the backend (§34). Hiding a button in a template is
presentation, never protection.
"""
from __future__ import annotations

import re

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.models import BilingualNameMixin, TimeStampedModel


class Country(models.TextChoices):
    """Jurisdictions, not entities.

    Standards resolve by jurisdiction; several AJi entities may share one.
    """

    SAUDI_ARABIA = "SA", _("Saudi Arabia")
    JORDAN = "JO", _("Jordan")
    UAE = "AE", _("United Arab Emirates")
    OMAN = "OM", _("Oman")


class Role(models.TextChoices):
    ADMIN = "admin", _("Administrator")
    CONTRACT_REVIEWER = "contract_reviewer", _("Contract Reviewer")
    LEGAL_REVIEWER = "legal_reviewer", _("Legal Reviewer")
    MANAGEMENT = "management", _("Management / Executive")
    STANDARD_MANAGER = "standard_manager", _("Standard Manager")
    USER = "user", _("Regular User")


class Entity(BilingualNameMixin, TimeStampedModel):
    """An AJi operating company (AJi - Jordan, HDP - UAE, Limana, ...).

    Each entity maps to exactly one jurisdiction, which is what standards
    resolution keys on.
    """

    name = models.CharField(max_length=120, unique=True)
    name_ar = models.CharField(max_length=120, blank=True)
    code = models.SlugField(max_length=32, unique=True)
    country = models.CharField(max_length=2, choices=Country.choices, db_index=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "entities"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_country_display()})"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        extra.setdefault("role", Role.USER)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", Role.ADMIN)
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """Email-identified user carrying an application role.

    `role` is deliberately separate from `is_superuser`: a Django superuser is
    an infrastructure concern and must not silently confer the authority to
    approve contracts (§34).
    """

    email = models.EmailField(_("email address"), unique=True)

    #: What this person is called on screen. A label and nothing else: it is
    #: not unique, it is not normalised, and it cannot be signed in with --
    #: `USERNAME_FIELD` is the email above, and it stays that way. Two people
    #: may both be "أحمد"; the account they each own is still their address.
    #:
    #: Blank is allowed because accounts predate this field and a migration
    #: cannot invent names for them; the signup form requires it, so every
    #: account created from now on has one.
    username = models.CharField(
        _("username"), max_length=80, blank=True,
        help_text=_("الاسم الذي يظهر في الموقع. لا يُستخدم لتسجيل الدخول."),
    )

    full_name_ar = models.CharField(max_length=150, blank=True)
    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.USER,
        db_index=True,
        help_text=_("Application role. Governs what this user may do."),
    )
    entity = models.ForeignKey(
        Entity,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="users",
    )
    department = models.CharField(max_length=120, blank=True)
    preferred_language = models.CharField(
        max_length=2, choices=[("ar", "العربية"), ("en", "English")], default="ar"
    )
    preferred_theme = models.CharField(
        max_length=8, choices=[("light", "Light"), ("dark", "Dark")], default="light"
    )
    sidebar_collapsed = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        ordering = ["email"]
        indexes = [models.Index(fields=["role", "is_active"])]

    def __str__(self) -> str:
        return self.email

    @property
    def short_name(self) -> str:
        """The one word to greet somebody by.

        `display_name` is the whole label and falls back to the address, which
        is right in a directory and wrong in a greeting: "أهلاً،
        suleiman_assaf@ajigroup.com" is not how anybody is spoken to. This
        takes the first word of the name, and for an account that has none it
        makes one out of the address rather than showing the address.
        """
        for source in (self.username, self.get_full_name(), self.full_name_ar):
            first = (source or "").strip().split()
            if first:
                return first[0]
        local = self.email.split("@")[0]
        part = re.split(r"[._+-]", local)[0] or local
        return part[:1].upper() + part[1:]

    # ── role helpers (read-only convenience, not the permission check) ────
    @property
    def display_name(self) -> str:
        """What to call this person anywhere on screen.

        The chosen name first: it is the one they gave. Then the older ways an
        account could carry a name, and only then the address -- a screen full
        of email addresses reads as a database rather than as colleagues.
        """
        return (
            self.username.strip()
            or self.get_full_name().strip()
            or self.full_name_ar
            or self.email
        )

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_contract_reviewer(self) -> bool:
        return self.role == Role.CONTRACT_REVIEWER

    @property
    def is_legal_reviewer(self) -> bool:
        return self.role == Role.LEGAL_REVIEWER

    @property
    def is_management(self) -> bool:
        return self.role == Role.MANAGEMENT

    @property
    def is_standard_manager(self) -> bool:
        return self.role == Role.STANDARD_MANAGER

    @property
    def jurisdiction(self) -> str | None:
        return self.entity.country if self.entity_id else None
