"""Contract records, parties and lifecycle state.

Design note — Country is a `TextChoices` enum rather than a table, while
ContractType is a table. Jurisdictions are stable ISO codes and are the key
every standards lookup resolves on; adding one is a deliberate legal event that
also needs new standards written, so a migration is appropriate. Contract types
genuinely vary by business and are managed from the admin (§50).
"""
from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Country
from apps.common.models import BilingualNameMixin, SoftDeleteModel, TimeStampedModel, UUIDPrimaryKeyModel


class ContractStatus(models.TextChoices):
    """Lifecycle states (§27), including the error and uncertainty branches."""

    # -- pipeline -------------------------------------------------------
    UPLOADED = "uploaded", _("Uploaded")
    EXTRACTING = "extracting", _("Extracting")
    OCR_VERIFICATION = "ocr_verification", _("OCR / Verification")
    STRUCTURING = "structuring", _("Structuring")
    AI_ANALYSIS = "ai_analysis", _("AI Analysis")
    # -- human review ---------------------------------------------------
    PENDING_CONTRACT_REVIEW = "pending_contract_review", _("Pending Contract Review")
    LEGAL_REVIEW = "legal_review", _("Legal Review")
    MANAGEMENT_REVIEW = "management_review", _("Management Review")
    APPROVED = "approved", _("Approved")
    ARCHIVED = "archived", _("Archived")
    # -- alternate / failure --------------------------------------------
    EXTRACTION_FAILED = "extraction_failed", _("Extraction Failed")
    ANALYSIS_FAILED = "analysis_failed", _("Analysis Failed")
    RETRYING = "retrying", _("Retrying")
    NEEDS_SOURCE_REVIEW = "needs_source_review", _("Needs Source Review")
    NEEDS_HUMAN_REVIEW = "needs_human_review", _("Needs Human Review")
    REJECTED = "rejected", _("Rejected")
    RETURNED_FOR_REVISION = "returned_for_revision", _("Returned for Revision")

    @classmethod
    def terminal(cls) -> set[str]:
        return {cls.ARCHIVED, cls.REJECTED}

    @classmethod
    def in_pipeline(cls) -> set[str]:
        return {
            cls.UPLOADED, cls.EXTRACTING, cls.OCR_VERIFICATION,
            cls.STRUCTURING, cls.AI_ANALYSIS, cls.RETRYING,
        }


class RiskLevel(models.TextChoices):
    """Risk classification.

    UNKNOWN is a real state and must never be silently rendered as LOW — the
    previous system did exactly that and it hid genuine gaps (§54).
    """

    HIGH = "high", _("High")
    MEDIUM = "medium", _("Medium")
    LOW = "low", _("Low")
    UNKNOWN = "unknown", _("Unknown")


def risk_badge_label(level) -> str:
    """A risk level as every risk badge says it -- "مخاطر مرتفعة", the words
    of templates/includes/risk_badge.html -- where the choice label alone
    ("مرتفعة") reads as a count's unit ("3 مرتفعة")."""
    return str({
        RiskLevel.HIGH: _("مخاطر مرتفعة"),
        RiskLevel.MEDIUM: _("مخاطر متوسطة"),
        RiskLevel.LOW: _("مخاطر منخفضة"),
    }.get(level, _("غير محدد")))


class PartyRole(models.TextChoices):
    """Which side of the contract a party sits on.

    `analysis_perspective` derives from this. If AJi is the Consultant an
    uncapped delay-damages clause is dangerous; if AJi is the Client the same
    clause is protective. Get this wrong and every finding inverts.
    """

    CONSULTANT = "consultant", _("Consultant")
    CLIENT = "client", _("Client / Employer")
    SUB_CONSULTANT = "sub_consultant", _("Sub-consultant")
    JV_PARTNER = "jv_partner", _("Joint Venture Partner")
    OTHER = "other", _("Other")


class CounterpartyType(models.TextChoices):
    GOVERNMENT = "government", _("Government")
    SEMI_GOVERNMENT = "semi_government", _("Semi-government")
    PRIVATE = "private", _("Private")
    JV_PARTNER = "jv_partner", _("Joint Venture Partner")
    SUB_CONSULTANT = "sub_consultant", _("Sub-consultant")


class ContractType(BilingualNameMixin, TimeStampedModel):
    """Business-configurable contract classification (§50)."""

    code = models.SlugField(max_length=48, unique=True)
    name = models.CharField(max_length=160)
    name_ar = models.CharField(max_length=160, blank=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


class Currency(models.TextChoices):
    SAR = "SAR", "SAR"
    USD = "USD", "USD"
    EUR = "EUR", "EUR"
    JOD = "JOD", "JOD"
    AED = "AED", "AED"
    OMR = "OMR", "OMR"


class ContractLanguage(models.TextChoices):
    ARABIC = "ar", _("Arabic")
    ENGLISH = "en", _("English")
    BILINGUAL = "bi", _("Bilingual")


class Contract(UUIDPrimaryKeyModel, TimeStampedModel, SoftDeleteModel):
    """A client contract under review."""

    # ── identity ─────────────────────────────────────────────────────────
    reference = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text=_("Internal or client reference / PO number."),
    )
    project_name = models.CharField(max_length=255, db_index=True)
    contract_title = models.CharField(max_length=255)
    client_name = models.CharField(max_length=255, db_index=True)

    # ── classification ───────────────────────────────────────────────────
    # Country is REQUIRED: standards resolution cannot run without it.
    # The v2 design omitted this field; it is added because the system is
    # non-functional otherwise (§4, §14, §15).
    country = models.CharField(
        max_length=2, choices=Country.choices, db_index=True,
        help_text=_("Jurisdiction. Selects which AJi standards apply."),
    )
    entity = models.ForeignKey(
        "accounts.Entity", on_delete=models.PROTECT, related_name="contracts",
        help_text=_("The AJi company that is party to this contract."),
    )
    contract_type = models.ForeignKey(
        ContractType, on_delete=models.PROTECT, related_name="contracts",
    )
    analysis_perspective = models.CharField(
        max_length=32, choices=PartyRole.choices, default=PartyRole.CONSULTANT,
        help_text=_("Which side AJi is on. Determines the direction of risk."),
    )
    counterparty_type = models.CharField(
        max_length=32, choices=CounterpartyType.choices,
        default=CounterpartyType.PRIVATE, db_index=True,
    )

    # ── commercial ───────────────────────────────────────────────────────
    contract_value = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)],
        help_text=_("Declared value. Decimal — never float, for money."),
    )
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.SAR)
    language = models.CharField(
        max_length=2, choices=ContractLanguage.choices, default=ContractLanguage.ARABIC
    )
    effective_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)

    # ── ownership ────────────────────────────────────────────────────────
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="uploaded_contracts",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="assigned_contracts",
    )
    department = models.CharField(max_length=120, blank=True)

    # ── analysis inputs ──────────────────────────────────────────────────
    reference_template = models.CharField(
        max_length=64, blank=True, default="aji_standard",
        help_text=_("Which standards profile to compare against, e.g. aji_standard / fidic."),
    )

    @property
    def reference_template_label(self) -> str:
        """The profile in the reader's words, as the upload form offered it;
        the contract page printed the stored code ("aji_standard")."""
        return {"aji_standard": _("مراجعة AJi القياسية"), "fidic": "FIDIC"}.get(
            self.reference_template, self.reference_template or "—")
    analysis_instructions = models.TextField(
        blank=True, help_text=_("Optional reviewer focus points passed to the analyser."),
    )

    # ── state ────────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=32, choices=ContractStatus.choices,
        default=ContractStatus.UPLOADED, db_index=True,
    )
    risk_level = models.CharField(
        max_length=16, choices=RiskLevel.choices,
        default=RiskLevel.UNKNOWN, db_index=True,
    )
    current_analysis = models.ForeignKey(
        "analysis.AnalysisRun", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
        help_text=_("Pointer to the latest analysis. History is never overwritten."),
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["country", "status"]),
            models.Index(fields=["risk_level", "status"]),
            models.Index(fields=["expiry_date", "status"]),
            models.Index(fields=["entity", "-created_at"]),
        ]

    @property
    def short_ref(self) -> str:
        """The reference number, or the first eight characters of the id.

        Every picker on the site listed contracts as "reference — project",
        and a contract with no reference printed "—". Four uploads of the same
        project therefore read "— — Coastal District Ma…" four times over,
        with nothing to tell them apart. The id prefix is what `__str__` and
        the export filenames already fall back to.
        """
        return self.reference or str(self.id)[:8]

    def __str__(self) -> str:
        return f"{self.short_ref} — {self.project_name}"

    @property
    def is_in_pipeline(self) -> bool:
        return self.status in ContractStatus.in_pipeline()

    @property
    def is_editable(self) -> bool:
        return self.status not in ContractStatus.terminal()

    @property
    def days_to_expiry(self) -> int | None:
        if not self.expiry_date:
            return None
        from django.utils import timezone

        return (self.expiry_date - timezone.localdate()).days


class ContractParty(TimeStampedModel):
    """A named party to the contract.

    Modelled as a list rather than two fixed columns so joint ventures and
    multi-party appointments do not need a migration later.
    """

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="parties")
    name = models.CharField(max_length=255)
    name_ar = models.CharField(max_length=255, blank=True)
    role = models.CharField(max_length=32, choices=PartyRole.choices)
    is_aji = models.BooleanField(
        default=False, help_text=_("True for the AJi entity side of the contract."),
    )
    registration_number = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "contract parties"
        ordering = ["-is_aji", "name"]
        indexes = [models.Index(fields=["contract", "is_aji"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_role_display()})"
