"""The AJi Contract Standards Library (§14, §15, §33).

Two rules govern this module:

1. Nothing here is invented. Standards are authored and approved by AJi Legal
   through the admin; the code ships no company policy of its own. Development
   fixtures exist but are explicitly labelled as not-approved (§49).
2. Approved versions are immutable. Editing a standard creates a new version so
   a contract analysed under v3 remains traceable to v3 forever.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Country
from apps.common.models import BilingualNameMixin, TimeStampedModel, UUIDPrimaryKeyModel


class ClauseCategory(BilingualNameMixin, TimeStampedModel):
    """Configurable clause taxonomy (§16) — managed, not hard-coded."""

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=160)
    name_ar = models.CharField(max_length=160, blank=True)
    description = models.TextField(blank=True)
    default_department = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name_plural = "clause categories"
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


class StandardScope(models.TextChoices):
    """Resolution precedence — most specific wins."""

    GLOBAL = "global", _("AJi Global Standard")
    COUNTRY = "country", _("Country Override")
    CONTRACT_TYPE = "contract_type", _("Contract-Type Override")


class StandardStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PENDING_APPROVAL = "pending_approval", _("Pending approval")
    APPROVED = "approved", _("Approved")
    SUPERSEDED = "superseded", _("Superseded")
    WITHDRAWN = "withdrawn", _("Withdrawn")


class Standard(TimeStampedModel):
    """The identity of a rule — *which* position, not its wording.

    Content lives in StandardVersion so that history is preserved.
    """

    category = models.ForeignKey(
        ClauseCategory, on_delete=models.PROTECT, related_name="standards"
    )
    scope = models.CharField(max_length=16, choices=StandardScope.choices, db_index=True)
    country = models.CharField(
        max_length=2, choices=Country.choices, blank=True, db_index=True,
        help_text=_("Blank for a global standard."),
    )
    contract_type = models.ForeignKey(
        "contracts.ContractType", null=True, blank=True,
        on_delete=models.PROTECT, related_name="standards",
        help_text=_("Set only for a contract-type override."),
    )
    reference_template = models.CharField(
        max_length=64, default="aji_standard",
        help_text=_("Profile this belongs to, e.g. aji_standard / fidic."),
    )
    #: Which side AJi is on for this standard to apply (Contract.
    #: analysis_perspective); blank applies to every side. A sub-consultancy
    #: in which AJi hires, a JV agreement and an association need positions
    #: of their own -- what protects AJi as consultant can expose it as the
    #: hiring party.
    perspective = models.CharField(
        max_length=32, blank=True, db_index=True,
        choices=[("consultant", _("Consultant")), ("client", _("Client / Employer")),
                 ("sub_consultant", _("Sub-consultant")), ("jv_partner", _("Joint Venture Partner")),
                 ("other", _("Other"))],
        help_text=_("AJi's side for this standard to apply. Blank for every side."),
    )
    title = models.CharField(max_length=255)
    title_ar = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category__sort_order", "scope"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "scope", "country", "contract_type", "reference_template",
                        "perspective"],
                name="uniq_standard_scope",
            )
        ]
        indexes = [
            models.Index(fields=["category", "scope", "country"]),
            models.Index(fields=["reference_template", "is_active"]),
        ]

    def __str__(self) -> str:
        bits = [self.category.code, self.scope]
        if self.country:
            bits.append(self.country)
        if self.contract_type_id:
            bits.append(self.contract_type.code)
        return " / ".join(bits)

    @property
    def specificity(self) -> int:
        """Higher wins during resolution."""
        return {
            StandardScope.GLOBAL: 0,
            StandardScope.COUNTRY: 1,
            StandardScope.CONTRACT_TYPE: 2,
        }[self.scope]

    def current_version(self, on_date=None):
        """The approved version in force on a date (defaults to today)."""
        on_date = on_date or timezone.localdate()
        return (
            self.versions.filter(
                status=StandardStatus.APPROVED,
                effective_date__lte=on_date,
            )
            .filter(models.Q(expiry_date__isnull=True) | models.Q(expiry_date__gte=on_date))
            .order_by("-version", "-effective_date")
            .first()
        )


class StandardVersion(UUIDPrimaryKeyModel, TimeStampedModel):
    """An immutable, approved statement of an AJi position."""

    standard = models.ForeignKey(Standard, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()

    # ── the position ─────────────────────────────────────────────────────
    preferred_position = models.TextField(help_text=_("What AJi asks for."))
    preferred_position_ar = models.TextField(blank=True)
    acceptable_range = models.TextField(
        blank=True, help_text=_("What AJi will settle for, and under what conditions.")
    )
    acceptable_range_ar = models.TextField(blank=True)
    red_flags = models.TextField(
        blank=True, help_text=_("The walk-away point — do not sign beyond this.")
    )
    red_flags_ar = models.TextField(blank=True)
    approved_fallback_wording = models.TextField(
        blank=True, help_text=_("Pre-approved replacement clause text.")
    )
    approved_fallback_wording_ar = models.TextField(blank=True)
    exceptions = models.TextField(blank=True)
    approval_authority = models.CharField(
        max_length=160, blank=True,
        help_text=_("Who may approve a deviation, e.g. 'Head of Risk Management'."),
    )
    responsible_department = models.CharField(max_length=64, blank=True)

    # ── deterministic comparison inputs (§17) ────────────────────────────
    rule_definition = models.JSONField(
        default=dict, blank=True,
        help_text=_(
            "Machine-checkable rule, e.g. "
            '{"operator": "lte", "value": 10, "unit": "percent_of_contract_value"}. '
            "Evaluated in Python — never delegated to a language model."
        ),
    )
    impact = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text=_("Impact 1–5 set by Legal. Feeds risk scoring; the AI does not set it."),
    )

    # ── provenance ───────────────────────────────────────────────────────
    source_reference = models.CharField(
        max_length=255, blank=True,
        help_text=_("Where this came from, e.g. 'AJi Standard Positions v3, §7.2'."),
    )
    status = models.CharField(
        max_length=24, choices=StandardStatus.choices,
        default=StandardStatus.DRAFT, db_index=True,
    )
    effective_date = models.DateField(null=True, blank=True, db_index=True)
    expiry_date = models.DateField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="approved_standard_versions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    change_note = models.TextField(blank=True)

    is_demo_fixture = models.BooleanField(
        default=False, db_index=True,
        help_text=_("TEST DATA — NOT APPROVED AJi POLICY. Must be visible in the UI (§49)."),
    )

    class Meta:
        ordering = ["standard", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["standard", "version"], name="uniq_standard_version"
            )
        ]
        indexes = [
            models.Index(fields=["standard", "status", "effective_date"]),
            models.Index(fields=["status", "is_demo_fixture"]),
        ]

    def __str__(self) -> str:
        return f"{self.standard} v{self.version} ({self.status})"

    @property
    def is_baseline(self) -> bool:
        """From the FIDIC baseline rather than an AJi position (§ default profile)."""
        return self.standard.reference_template == StandardProfile.FIDIC

    @property
    def is_pending(self) -> bool:
        return self.status in {StandardStatus.DRAFT, StandardStatus.PENDING_APPROVAL}

    @property
    def is_locked(self) -> bool:
        """Approved content is frozen; corrections create a new version (§33)."""
        return self.status in {StandardStatus.APPROVED, StandardStatus.SUPERSEDED}

    def save(self, *args, **kwargs):
        # `_state.adding`, not `self.pk`: a UUID primary key is assigned at
        # instantiation, so `self.pk` is already truthy on the very first save.
        # Judged by what is stored, not what is in memory: a draft being
        # approved already reads "approved" here, and approving it (status,
        # approver, time) must go through -- that is the one moment it is
        # written with those values. Once stored as approved, it is frozen.
        stored = None
        if not self._state.adding and self.is_locked:
            stored = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
        if stored in {StandardStatus.APPROVED, StandardStatus.SUPERSEDED}:
            # Allow only the transition that supersedes it.
            dirty = kwargs.get("update_fields") or []
            allowed = {"status", "expiry_date", "updated_at"}
            if not dirty or not set(dirty).issubset(allowed):
                raise ValueError(
                    "Approved standard versions are immutable. Create a new version "
                    "instead of editing this one."
                )
        return super().save(*args, **kwargs)


class StandardProfile(models.TextChoices):
    """Which body of positions a standard belongs to (`reference_template`).

    AJi's own positions, uploaded and approved by AJi, come first. The FIDIC
    baseline is the default beneath them: a category AJi has not set a
    position for is resolved against it (resolution.resolve_for_category).
    """

    AJI = "aji_standard", _("AJi standard")
    FIDIC = "fidic", _("FIDIC baseline")


class StandardUpload(UUIDPrimaryKeyModel, TimeStampedModel):
    """A standards document uploaded in Settings, read and normalised.

    Every organisation writes its positions differently -- a Word memo, a
    spreadsheet, a scanned policy. The upload is read (normalization.py) and
    each position found is restated in one uniform shape: the category it
    belongs to, what AJi asks for, what it will accept, where it walks away,
    and the sentence of the document it came from. A person reviews that and
    saves it; the positions then arrive as drafts and drive no analysis until
    someone with approval rights approves them.
    """

    class Status(models.TextChoices):
        PROCESSING = "processing", _("Reading")
        REVIEW = "review", _("Ready for review")
        SAVED = "saved", _("Saved as drafts")
        FAILED = "failed", _("Failed")

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="standard_uploads",
    )
    original_filename = models.CharField(max_length=255)
    stored_path = models.CharField(max_length=512, help_text=_("Under PRIVATE_MEDIA_ROOT."))
    sha256 = models.CharField(max_length=64, db_index=True)
    size_bytes = models.BigIntegerField(default=0)
    profile = models.CharField(max_length=64, choices=StandardProfile.choices,
                               default=StandardProfile.AJI)
    #: Blank for a standard that applies in every country.
    country = models.CharField(max_length=2, choices=Country.choices, blank=True)
    effective_date = models.DateField(default=timezone.localdate)
    note = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PROCESSING, db_index=True)
    #: How the positions were found: "table" (a spreadsheet, CSV or JSON in
    #: the Legal template's columns, read directly) or "ai" (a document,
    #: read by the model and checked against its own text).
    method = models.CharField(max_length=8, blank=True)
    extracted_text = models.TextField(blank=True)
    #: The normalised positions, one dict per position (normalization.ITEM_FIELDS).
    items = models.JSONField(default=list, blank=True)
    ai_calls = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    #: StandardVersion ids created from this upload.
    created_versions = models.JSONField(default=list, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.status})"
