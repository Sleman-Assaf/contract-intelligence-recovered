"""Clause structure, deterministic rule results and evidence-backed findings.

The hybrid principle (§18, §55) is encoded in the field names:

    system_result  — the deterministic rules engine's verdict
    ai_result      — the model's semantic verdict
    verification   — what happens when they agree, or don't

A model can never silently overwrite `system_result`. Disagreement produces
SYSTEM_AI_CONFLICT and routes to a human.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class AnalysisStatus(models.TextChoices):
    QUEUED = "queued", _("Queued")
    RUNNING = "running", _("Running")
    SUCCEEDED = "succeeded", _("Succeeded")
    DEGRADED = "degraded", _("Completed with gaps")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")


class ComplianceResult(models.TextChoices):
    """Deliberately includes the uncertainty states (§54)."""

    COMPLIANT = "compliant", _("Compliant")
    NON_COMPLIANT = "non_compliant", _("Non-compliant")
    PARTIAL = "partial", _("Partially compliant")
    AMBIGUOUS = "ambiguous", _("Ambiguous")
    UNKNOWN = "unknown", _("Unknown")
    NO_STANDARD_FOUND = "no_standard_found", _("No applicable standard")
    NOT_APPLICABLE = "not_applicable", _("Not applicable")


class VerificationOutcome(models.TextChoices):
    PENDING = "pending", _("Pending")
    VERIFIED = "verified", _("Verified — rules and AI agree")
    SYSTEM_AI_CONFLICT = "system_ai_conflict", _("System/AI conflict")
    SOURCE_CONFLICT = "source_conflict", _("Source conflict")
    AMBIGUOUS = "ambiguous", _("Ambiguous")
    NEEDS_HUMAN_REVIEW = "needs_human_review", _("Needs human review")


class AnalysisRun(UUIDPrimaryKeyModel, TimeStampedModel):
    """One complete pass over a contract. Never overwritten — re-analysis
    creates a new run so every finding stays traceable to what produced it (§32).
    """

    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="analysis_runs"
    )
    version = models.PositiveIntegerField(help_text=_("1, 2, 3 … per contract."))
    document = models.ForeignKey(
        "documents.Document", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="analysis_runs",
    )

    status = models.CharField(
        max_length=16, choices=AnalysisStatus.choices,
        default=AnalysisStatus.QUEUED, db_index=True,
    )
    task_id = models.CharField(max_length=64, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=64, blank=True, db_index=True)
    retry_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="retries"
    )

    # ── provenance: what produced this result (§31, §32, §45) ────────────
    ai_provider = models.CharField(max_length=32, blank=True)
    ai_text_model = models.CharField(max_length=128, blank=True)
    ai_vision_model = models.CharField(max_length=128, blank=True)
    prompt_template_version = models.CharField(max_length=32, blank=True)
    analysis_code_version = models.CharField(max_length=32, blank=True)
    standards_snapshot = models.JSONField(
        default=dict, blank=True,
        help_text=_("standard_id → version actually applied, frozen at run time."),
    )

    # ── outcome ──────────────────────────────────────────────────────────
    overall_risk = models.CharField(max_length=16, blank=True, db_index=True)

    @property
    def overall_risk_label(self) -> str:
        """The overall risk in the badge's words ("مخاطر مرتفعة"). The field has
        no choices, so the analysis page's `get_overall_risk_display` did not
        exist and its headline read "غير محدد" on every contract."""
        from apps.contracts.models import risk_badge_label

        return risk_badge_label(self.overall_risk)

    executive_summary = models.TextField(blank=True)
    critical_obligations = models.JSONField(default=list, blank=True)
    critical_dates = models.JSONField(default=list, blank=True)
    management_decisions = models.JSONField(default=list, blank=True)
    next_action = models.TextField(blank=True)

    #: The run-level prose in every language produced, same shape as
    #: Finding.prose: {"ar": {"executive_summary": ..., "next_action": ...,
    #: "critical_obligations": [...], ...}, "en": {...}}.
    prose = models.JSONField(default=dict, blank=True)

    PROSE_FIELDS = (
        "executive_summary", "next_action", "critical_obligations",
        "critical_dates", "management_decisions",
    )

    def prose_in(self, field, language=None):
        """One summary field in the reader's language, else the stored one."""
        from django.utils.translation import get_language

        language = language or (get_language() or "")
        short = language.split("-")[0]
        for key in (language, short):
            value = (self.prose.get(key) or {}).get(field)
            if value:
                return value
        return getattr(self, field, None) or ("" if field in
                                              ("executive_summary", "next_action") else [])

    high_count = models.PositiveIntegerField(default=0)
    medium_count = models.PositiveIntegerField(default=0)
    low_count = models.PositiveIntegerField(default=0)
    unresolved_count = models.PositiveIntegerField(
        default=0, help_text=_("Findings in an uncertainty state awaiting a human.")
    )

    system_confidence = models.FloatField(
        null=True, blank=True,
        help_text=_("Computed from measurable signals — not a self-reported LLM score (§24)."),
    )
    confidence_breakdown = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    #: Clauses assessed so far, of how many: what the progress bar on the
    #: contract page is measured against while the run is going.
    progress_done = models.PositiveIntegerField(default=0)
    progress_total = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "version"], name="uniq_analysis_version_per_contract"
            )
        ]
        indexes = [
            models.Index(fields=["contract", "-version"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Analysis v{self.version} of {self.contract_id}"


class Clause(UUIDPrimaryKeyModel, TimeStampedModel):
    """A clause detected in the document, always traceable to source blocks (§13)."""

    document = models.ForeignKey(
        "documents.Document", on_delete=models.CASCADE, related_name="clauses"
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="clauses"
    )

    clause_number = models.CharField(max_length=64, blank=True, db_index=True)
    heading = models.CharField(max_length=512, blank=True)
    text = models.TextField(help_text=_("Assembled from source blocks. Not model output."))
    page_number = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    ordinal = models.PositiveIntegerField(default=0, help_text=_("Document reading order."))

    source_blocks = models.ManyToManyField(
        "documents.Block", related_name="clauses", blank=True,
        help_text=_("The evidence this clause was assembled from."),
    )
    is_inferred = models.BooleanField(
        default=False,
        help_text=_("True only when a clause could not be tied to source blocks (§13)."),
    )

    # ── classification (§16) — uncertainty allowed, not forced ───────────
    primary_category = models.ForeignKey(
        "standards.ClauseCategory", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="clauses",
    )
    alternative_categories = models.JSONField(
        default=list, blank=True,
        help_text=_("Other plausible classifications when confidence is low."),
    )
    classification_confidence = models.FloatField(null=True, blank=True)

    # ── cross references (§20) ───────────────────────────────────────────
    referenced_clause_numbers = models.JSONField(
        default=list, blank=True,
        help_text=_('e.g. ["18.4"] when the clause says "subject to Clause 18.4".'),
    )

    class Meta:
        ordering = ["ordinal"]
        indexes = [
            models.Index(fields=["contract", "ordinal"]),
            models.Index(fields=["document", "clause_number"]),
            models.Index(fields=["primary_category"]),
        ]

    def __str__(self) -> str:
        return f"{self.clause_number or '—'} {self.heading[:40]}"


class ExtractedValue(TimeStampedModel):
    """A structured value pulled out of a clause for the rules engine (§17).

    Extraction is deterministic where possible; the engine then compares
    numbers in Python rather than asking a model whether 15 > 10.
    """

    clause = models.ForeignKey(Clause, on_delete=models.CASCADE, related_name="values")
    key = models.CharField(
        max_length=64, db_index=True,
        help_text=_("ld_cap_percent, payment_days, pi_insurance_amount, …"),
    )
    numeric_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    text_value = models.CharField(max_length=255, blank=True)
    unit = models.CharField(max_length=32, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    source_block = models.ForeignKey(
        "documents.Block", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="extracted_values",
    )
    extraction_confidence = models.FloatField(null=True, blank=True)
    is_critical = models.BooleanField(default=False, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["clause", "key"])]

    def __str__(self) -> str:
        return f"{self.key}={self.numeric_value or self.text_value}{self.unit}"


class FindingQuerySet(models.QuerySet):
    def current(self):
        """Only the findings of each contract's current analysis.

        Every analysis run stores its own findings, and a re-analysis
        supersedes the last. Anything that counts findings across contracts
        -- the risk matrix, the dashboard, search -- must use this, or a
        contract analysed six times is counted six times over.
        """
        return self.filter(analysis_id=models.F("contract__current_analysis_id"))


class Finding(UUIDPrimaryKeyModel, TimeStampedModel):
    """An evidence-backed deviation from an AJi standard (§22).

    `original_wording` is copied from stored source blocks — never authored by
    a model (§53).
    """

    objects = FindingQuerySet.as_manager()

    analysis = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, related_name="findings"
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="findings"
    )
    clause = models.ForeignKey(
        Clause, null=True, blank=True, on_delete=models.SET_NULL, related_name="findings"
    )

    # ── identity & classification ────────────────────────────────────────
    risk_level = models.CharField(max_length=16, db_index=True)
    impact = models.PositiveSmallIntegerField(null=True, blank=True, help_text=_("1–5"))
    probability = models.PositiveSmallIntegerField(null=True, blank=True, help_text=_("1–5"))
    risk_score = models.PositiveSmallIntegerField(null=True, blank=True, help_text=_("impact × probability"))
    category = models.ForeignKey(
        "standards.ClauseCategory", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="findings",
    )
    title = models.CharField(max_length=255)
    #: True when no model deviation text was available and the title was
    #: composed from the category. Composed titles are written in English at
    #: analysis time, so they are re-rendered in the reader's language rather
    #: than replayed; a model's own wording is left exactly as it came.
    title_is_generated = models.BooleanField(default=False)

    @property
    def display_title(self) -> str:
        from django.utils.translation import gettext as _

        if self.title_is_generated and self.category_id:
            if self.standard_version_id and self.standard_version.is_baseline:
                return _("%(category)s: deviation from the FIDIC baseline") % {
                    "category": self.category.display_name,
                }
            return _("%(category)s: deviation from the AJi standard") % {
                "category": self.category.display_name,
            }
        return self.prose_in("title")

    @property
    def display_rule_statement(self) -> str:
        """The rules engine's arithmetic, in the reader's language.

        `system_rule_detail["statement"]` is written in English for the audit
        trail ("12000000 must be at least 25000000") and was shown as-is on
        the Arabic page. It is rebuilt from the stored numbers here; the stored
        sentence is the fallback for detail shapes this does not know.
        """
        from django.utils.translation import gettext as _

        detail = self.system_rule_detail or {}
        operator = detail.get("operator")
        if operator in ("present", "absent"):
            found = bool(detail.get("markers_found"))
            return {
                ("present", True): _("الصياغة المطلوبة موجودة"),
                ("present", False): _("الصياغة المطلوبة غير موجودة"),
                ("absent", True): _("الصياغة المحظورة موجودة"),
                ("absent", False): _("الصياغة المحظورة غير موجودة"),
            }[(operator, found)]
        phrasing = {
            "lte": _("القيمة %(actual)s، والمطلوب ألا تتجاوز %(expected)s"),
            "gte": _("القيمة %(actual)s، والمطلوب ألا تقل عن %(expected)s"),
            "lt": _("القيمة %(actual)s، والمطلوب أن تكون أقل من %(expected)s"),
            "gt": _("القيمة %(actual)s، والمطلوب أن تكون أكثر من %(expected)s"),
            "eq": _("القيمة %(actual)s، والمطلوب أن تساوي %(expected)s"),
        }.get(operator)
        if phrasing is None or "actual" not in detail or "expected" not in detail:
            return detail.get("statement", "")

        unit = detail.get("unit")
        currency = detail.get("currency") or ""

        def figure(raw):
            try:
                number = Decimal(str(raw))
            except (ArithmeticError, ValueError):
                return str(raw)
            text = f"{number:,.2f}".rstrip("0").rstrip(".")
            if unit == "percent":
                return f"{text}%"
            if unit == "days":
                return _("%(n)s يوماً") % {"n": text}
            if unit == "currency" and currency:
                return f"{text} {currency}"
            return text

        statement = phrasing % {"actual": figure(detail["actual"]), "expected": figure(detail["expected"])}
        if detail.get("within_fallback") and "fallback" in detail:
            statement += _(" (الموقف المفضّل)، لكنها ضمن البديل المعتمد %(fallback)s وتتطلب اعتماداً") % {
                "fallback": figure(detail["fallback"])}
        return statement

    # ── evidence ─────────────────────────────────────────────────────────
    clause_number = models.CharField(max_length=64, blank=True)
    page_number = models.PositiveIntegerField(null=True, blank=True)
    source_blocks = models.ManyToManyField(
        "documents.Block", related_name="findings", blank=True
    )
    original_wording = models.TextField(
        blank=True, help_text=_("Verbatim from source evidence.")
    )

    # ── the standard applied ─────────────────────────────────────────────
    standard_version = models.ForeignKey(
        "standards.StandardVersion", null=True, blank=True,
        on_delete=models.PROTECT, related_name="findings",
    )
    standard_position = models.TextField(blank=True)

    # ── interpretation ───────────────────────────────────────────────────
    deviation = models.TextField(blank=True)
    explanation = models.TextField(blank=True)
    business_impact = models.TextField(blank=True)
    recommended_action = models.TextField(blank=True)
    fallback_wording = models.TextField(blank=True)
    responsible_department = models.CharField(max_length=64, blank=True)

    #: Interpretation in every language the run produced, keyed by language
    #: code: {"ar": {"title": ..., "explanation": ...}, "en": {...}}. The
    #: columns above stay the canonical copy so anything reading the model
    #: directly still works; `prose_in()` prefers the reader's language.
    prose = models.JSONField(default=dict, blank=True)

    #: A translation OF the quoted clause, never a replacement for it.
    #: `original_wording` remains the verbatim evidence and is what the
    #: redline export uses; this is shown beside it, labelled as a
    #: translation, so nobody mistakes it for the contract's own words.
    wording_translations = models.JSONField(default=dict, blank=True)

    PROSE_FIELDS = (
        "title", "deviation", "explanation", "business_impact",
        "recommended_action",
    )

    def prose_in(self, field: str, language: str | None = None) -> str:
        """One interpretation field in the reader's language.

        Falls back to the stored column rather than showing nothing: a
        finding that predates bilingual output still reads, just in whatever
        language it was written in.
        """
        from django.utils.translation import get_language

        language = language or (get_language() or "")
        short = language.split("-")[0]
        for key in (language, short):
            value = (self.prose.get(key) or {}).get(field)
            if value:
                return value
        return getattr(self, field, "") or ""

    # Templates cannot pass arguments, so each readable field gets a property.
    @property
    def display_deviation(self) -> str:
        return self.prose_in("deviation")

    @property
    def display_explanation(self) -> str:
        return self.prose_in("explanation")

    @property
    def display_business_impact(self) -> str:
        return self.prose_in("business_impact")

    @property
    def usable_fallback_wording(self) -> str:
        """The replacement wording, if it can go into this contract.

        Wording Legal approved on the standard is always shown. Wording the
        model suggested must be in the clause's own language -- it may be
        pasted into the contract and the redline export writes it there -- and
        the model sometimes wrote it in the finding's language instead (Arabic
        wording for an English clause). That is withheld, not translated.
        """
        wording = self.fallback_wording or ""
        if not wording.strip():
            return ""
        approved = getattr(self.standard_version, "approved_fallback_wording", "") if self.standard_version_id else ""
        if approved and " ".join(approved.split()) == " ".join(wording.split()):
            return wording
        from apps.documents.language import detect_language

        clause, suggested = detect_language(self.original_wording or ""), detect_language(wording)
        if clause in ("ar", "en") and suggested in ("ar", "en") and clause != suggested:
            return ""
        return wording

    @property
    def display_recommended_action(self) -> str:
        """The recommended action, unless it is just the approved wording.

        On a standards-based finding both fields resolve to the standard's
        approved fallback, so printing them one after the other shows the same
        paragraph twice under two different headings.
        """
        action = self.prose_in("recommended_action")
        if self._same_text(action, self.fallback_wording):
            return ""
        return action

    @property
    def display_wording_translation(self) -> str:
        return self.wording_translation()

    def wording_translation(self, language: str | None = None) -> str:
        """The clause translation for this reader, or "" if there is none.

        Returns nothing when the clause is already in the reader's language.
        A provider asked for "every language" will hand back the clause
        unchanged for the one it is already written in, and showing that under
        a "translation" heading is just the same paragraph twice -- worse,
        it implies a translation was made where none was needed.
        """
        from django.utils.translation import get_language

        language = language or (get_language() or "")
        short = language.split("-")[0]
        for key in (language, short):
            value = self.wording_translations.get(key)
            if value and not self._same_text(value, self.original_wording):
                return value
        return ""

    @staticmethod
    def _same_text(a: str, b: str) -> bool:
        """Whitespace-insensitive comparison; the model may reflow a quote."""
        return " ".join((a or "").split()) == " ".join((b or "").split())

    # ── hybrid decision record (§18) ─────────────────────────────────────
    system_result = models.CharField(
        max_length=24, choices=ComplianceResult.choices,
        default=ComplianceResult.UNKNOWN, db_index=True,
        help_text=_("Deterministic rules engine verdict. Authoritative for facts."),
    )
    system_rule_detail = models.JSONField(default=dict, blank=True)
    ai_result = models.CharField(
        max_length=24, choices=ComplianceResult.choices,
        default=ComplianceResult.UNKNOWN, db_index=True,
    )
    ai_reasoning = models.TextField(blank=True)
    verification_status = models.CharField(
        max_length=24, choices=VerificationOutcome.choices,
        default=VerificationOutcome.PENDING, db_index=True,
    )
    verifier_notes = models.TextField(blank=True)

    # ── confidence, kept apart (§24) ─────────────────────────────────────
    source_confidence = models.FloatField(null=True, blank=True)
    standard_confidence = models.FloatField(null=True, blank=True)
    rule_confidence = models.FloatField(null=True, blank=True)
    ai_confidence = models.FloatField(
        null=True, blank=True, help_text=_("Self-reported by the model. Never the final word.")
    )
    system_confidence = models.FloatField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-risk_score", "-created_at"]
        indexes = [
            models.Index(fields=["analysis", "risk_level"]),
            models.Index(fields=["contract", "risk_level"]),
            models.Index(fields=["verification_status"]),
            models.Index(fields=["system_result", "ai_result"]),
        ]

    def __str__(self) -> str:
        return f"[{self.risk_level}] {self.title}"

    @property
    def also_in(self) -> list[str]:
        """Other clauses that make the same point (merged by tidy.py)."""
        return list((self.system_rule_detail or {}).get("also_in", []))

    @property
    def needs_human(self) -> bool:
        return self.verification_status in {
            VerificationOutcome.SYSTEM_AI_CONFLICT,
            VerificationOutcome.SOURCE_CONFLICT,
            VerificationOutcome.AMBIGUOUS,
            VerificationOutcome.NEEDS_HUMAN_REVIEW,
        }


class MissingClause(TimeStampedModel):
    """An expected standard clause with no counterpart in the contract (§23).

    Absence is not automatically high risk — the standard's own impact decides.
    """

    analysis = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, related_name="missing_clauses"
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="missing_clauses"
    )
    standard_version = models.ForeignKey(
        "standards.StandardVersion", null=True, blank=True,
        on_delete=models.PROTECT, related_name="missing_in_analyses",
    )
    category = models.ForeignKey(
        "standards.ClauseCategory", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="missing_clauses",
    )
    title = models.CharField(max_length=255)
    reason_expected = models.TextField(blank=True)
    risk_if_absent = models.CharField(max_length=16, blank=True)
    recommended_action = models.TextField(blank=True)
    #: False when some clause in the contract could not be classified, so this
    #: category may in fact be present and simply unread. Claiming a clause is
    #: absent because we failed to look at it is worse than saying nothing.
    absence_confirmed = models.BooleanField(default=True)
    #: Clause numbers that touch on this topic in passing (as a secondary
    #: topic) without being a clause of their own -- liability inside
    #: "General provisions", confidentiality inside a staff clause. Shown as
    #: "no clause of its own; mentioned in ..." rather than plain "missing".
    mentioned_in = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["title"]
        indexes = [models.Index(fields=["analysis"]), models.Index(fields=["contract"])]

    def __str__(self) -> str:
        return f"missing: {self.title}"

    @property
    def mentioned_in_display(self) -> str:
        """"16، 23" on Arabic pages, "16, 23" on English ones."""
        from django.utils.translation import get_language

        comma = "، " if (get_language() or "").startswith("ar") else ", "
        return comma.join(str(n) for n in self.mentioned_in or [])

    @property
    def display_title(self) -> str:
        """`title` is the standard's English title, copied when the analysis
        ran, so the Arabic page showed "Suspension — AJi global position".
        On Arabic pages: the standard's Arabic title, else its category's."""
        from django.utils.translation import get_language

        if (get_language() or "").startswith("ar"):
            standard = self.standard_version.standard if self.standard_version_id else None
            if standard is not None and standard.title_ar:
                return standard.title_ar
            if self.category_id and self.category.name_ar:
                return self.category.name_ar
        return self.title


class ReviewDecision(UUIDPrimaryKeyModel, TimeStampedModel):
    """A human decision on one finding. Original AI output is never overwritten (§29)."""

    class Decision(models.TextChoices):
        ACCEPT = "accept", _("Accept")
        REJECT = "reject", _("Reject")
        ESCALATE = "escalate", _("Escalate")
        REQUEST_CORRECTION = "request_correction", _("Request correction")
        POLICY_EXCEPTION = "policy_exception", _("Mark policy exception")

    finding = models.ForeignKey(
        Finding, on_delete=models.CASCADE, related_name="decisions",
        help_text=_("A real foreign key — not a reconstructed string key."),
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="review_decisions"
    )
    decision = models.CharField(max_length=24, choices=Decision.choices, db_index=True)
    stage = models.CharField(
        max_length=32, blank=True,
        help_text=_("contract_review | legal_review | management_review"),
    )
    comment = models.TextField(blank=True)
    edited_recommendation = models.TextField(
        blank=True, help_text=_("Human-edited text. The AI's original stays on the Finding.")
    )
    edited_fallback_wording = models.TextField(blank=True)

    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_decisions"
    )
    decided_by_role = models.CharField(max_length=32, blank=True)
    decided_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-decided_at"]
        indexes = [
            models.Index(fields=["finding", "-decided_at"]),
            models.Index(fields=["contract", "stage"]),
        ]

    def __str__(self) -> str:
        return f"{self.decision} on {self.finding_id}"


# ── the chat's reading of a contract (RAG) ──────────────────────────────────

class RagIndex(UUIDPrimaryKeyModel, TimeStampedModel):
    """A contract in the form the chat answers from.

    Every analysed file is kept twice: the original (a SourceFile, the PDF or
    Word document as uploaded) and this -- its clauses cut into passages, each
    with its clause number, page and category, and the terms a search matches
    on. The chat answers only from these passages (and the AJi standards the
    analysis applied), never from general knowledge. Built when the analysis
    finishes (apps.analysis.rag.build); one per analysis run, so a re-analysis
    gets a fresh one and the chat follows the contract's current run.
    """

    analysis = models.OneToOneField(
        AnalysisRun, on_delete=models.CASCADE, related_name="rag_index"
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="rag_indexes"
    )
    passages_count = models.PositiveIntegerField(default=0)
    #: Mean passage length in terms, and in how many passages each term
    #: occurs: what the BM25 ranking needs, computed once at build time.
    avg_terms = models.FloatField(default=0)
    doc_freq = models.JSONField(default=dict, blank=True)
    builder_version = models.CharField(max_length=16, blank=True)

    def __str__(self) -> str:
        return f"RAG index of {self.analysis_id} ({self.passages_count} passages)"


class RagPassage(models.Model):
    """One searchable passage of a contract: a clause, or part of a long one."""

    index = models.ForeignKey(RagIndex, on_delete=models.CASCADE, related_name="passages")
    ordinal = models.PositiveIntegerField(help_text=_("Reading order in the document."))
    clause = models.ForeignKey(
        Clause, null=True, blank=True, on_delete=models.SET_NULL, related_name="rag_passages"
    )
    clause_number = models.CharField(max_length=64, blank=True)
    heading = models.CharField(max_length=512, blank=True)
    page_number = models.PositiveIntegerField(null=True, blank=True)
    category_code = models.CharField(max_length=64, blank=True)
    text = models.TextField()
    #: {term: count}, the passage's words normalised the way questions are.
    terms = models.JSONField(default=dict, blank=True)
    length = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["ordinal"]
        indexes = [models.Index(fields=["index", "ordinal"])]

    def __str__(self) -> str:
        return f"passage {self.ordinal} (clause {self.clause_number or '—'})"
