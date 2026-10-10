"""Source evidence: the uploaded file and its canonical extracted form (§12).

The canonical text is legal evidence. Nothing in this module may be rewritten
by a language model — an LLM's output belongs in `apps.analysis`, never here
(§53).
"""
from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class ExtractionMethod(models.TextChoices):
    NATIVE_PDF = "native_pdf", _("Native PDF text")
    OCR = "ocr", _("OCR")
    OCR_VERIFIED_BY_VISION = "ocr_verified_by_vision", _("OCR verified by vision")
    VISION = "vision", _("Vision")
    DOCX = "docx", _("DOCX text")
    HUMAN_VERIFIED = "human_verified", _("Human verified")


class VerificationStatus(models.TextChoices):
    """Uncertainty is a first-class state (§54) — never coerced into a risk band."""

    UNVERIFIED = "unverified", _("Unverified")
    ACCEPTED = "accepted", _("Accepted")
    AUTO_VALIDATED = "auto_validated", _("Accepted with automatic validation")
    VERIFIED = "verified", _("Verified — OCR and vision agree")
    #: The readings differ only in non-critical characters (spacing, a
    #: diacritic, punctuation) and every critical value is identical. The
    #: difference is logged on the verification record.
    AUTO_RESOLVED = "auto_resolved", _("Auto-resolved — minor non-critical difference")
    #: OCR and the first vision reading disagreed on a critical value; a
    #: second, stricter vision reading confirmed one of them where OCR itself
    #: was unsure. The whole dispute stays on record.
    VISION_REVERIFIED = "vision_reverified", _("Re-verified by a second vision reading")
    #: Kept for rows written before the three-tier flow; new disagreements on
    #: a critical value are NEEDS_HUMAN_REVIEW.
    SOURCE_CONFLICT = "source_conflict", _("Source conflict — OCR and vision disagree")
    NEEDS_HUMAN_REVIEW = "needs_human_review", _("Needs human review")
    FAILED = "failed", _("Extraction failed")


class SourceFile(UUIDPrimaryKeyModel, TimeStampedModel):
    """The uploaded artefact, stored privately and never served directly."""

    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="source_files"
    )
    original_filename = models.CharField(max_length=255)
    stored_path = models.CharField(
        max_length=512,
        help_text=_("Path relative to PRIVATE_MEDIA_ROOT. Never a public URL."),
    )
    content_type = models.CharField(max_length=128)
    size_bytes = models.BigIntegerField()
    sha256 = models.CharField(
        max_length=64, db_index=True,
        help_text=_("Integrity proof that the evidence has not changed."),
    )
    page_count = models.PositiveIntegerField(null=True, blank=True)
    is_encrypted = models.BooleanField(
        default=False, help_text=_("Password-protected PDFs cannot be extracted.")
    )

    # ── negotiation rounds ───────────────────────────────────────────────
    # A contract is rarely settled in one pass: AJi comments, the counterparty
    # returns a revised draft, and the whole thing is analysed again. Each
    # upload is its own round, and every earlier round stays exactly as it was
    # — you cannot argue about what changed if the previous draft was
    # overwritten.
    round_number = models.PositiveIntegerField(
        default=1, help_text=_("Negotiation round. 1 is the first draft received.")
    )
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="superseded_by",
        help_text=_("The round this draft replaces."),
    )
    round_note = models.CharField(
        max_length=255, blank=True,
        help_text=_("What changed in this round, in the uploader's words."),
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["contract", "-created_at"]),
            models.Index(fields=["contract", "round_number"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "round_number"],
                name="unique_round_per_contract",
            ),
        ]

    def __str__(self) -> str:
        return self.original_filename


class Document(UUIDPrimaryKeyModel, TimeStampedModel):
    """Canonical representation of one source file."""

    source_file = models.OneToOneField(
        SourceFile, on_delete=models.CASCADE, related_name="document"
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.CASCADE, related_name="documents"
    )
    detected_language = models.CharField(max_length=8, blank=True)
    page_count = models.PositiveIntegerField(default=0)

    native_text_coverage = models.FloatField(
        default=0.0,
        help_text=_("Share of pages carrying a usable native text layer (0–1)."),
    )
    requires_ocr = models.BooleanField(default=False)

    overall_confidence = models.FloatField(
        null=True, blank=True, help_text=_("Aggregate source-extraction quality (0–100).")
    )
    verification_status = models.CharField(
        max_length=32, choices=VerificationStatus.choices,
        default=VerificationStatus.UNVERIFIED, db_index=True,
    )
    extraction_started_at = models.DateTimeField(null=True, blank=True)
    extraction_finished_at = models.DateTimeField(null=True, blank=True)
    extraction_error = models.TextField(blank=True)
    #: Problems found in the file itself, for the reviewer: annexes that
    #: are placeholders, figures whose words disagree, IBANs that fail their
    #: checksum. [{"kind", "page", "detail"}, ...]
    warnings = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [models.Index(fields=["contract", "-created_at"])]

    def __str__(self) -> str:
        return f"Document for {self.source_file.original_filename}"


class PageKind(models.TextChoices):
    """What a page is, decided from its text (apps/documents/page_kinds.py).

    Only the contract's own pages are analysed by default. A signed contract
    file often carries far more than the contract: one of 179 pages had about
    45 pages of agreement and conditions, and the rest was the bid copy, CVs,
    registration certificates, a repeated set of addenda and an older
    contract whose "not to exceed" figure looked like this one's value.
    """

    CONTRACT = "contract", _("Contract text")
    SCHEDULE = "schedule", _("Schedule / annex")
    ADDENDUM = "addendum", _("Addendum")
    SIGNATURE = "signature", _("Signatures / stamps")
    COVER = "cover", _("Cover / index")
    #: Every article title in a row: read as text, each became an empty clause.
    CONTENTS = "contents", _("Contents page")
    CV = "cv", _("CV / staff document")
    REGISTRATION = "registration", _("Certificate / registration")
    BID = "bid", _("Bid / tender submission")
    DRAWING = "drawing", _("Drawing / map")
    PLACEHOLDER = "placeholder", _("Placeholder (content not in the file)")
    BLANK = "blank", _("Blank or unreadable")
    DUPLICATE = "duplicate", _("Repeats an earlier page")
    OTHER = "other", _("Other")


class Page(TimeStampedModel):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="pages")
    page_number = models.PositiveIntegerField(db_index=True)
    width = models.FloatField(null=True, blank=True)
    height = models.FloatField(null=True, blank=True)
    rotation = models.IntegerField(default=0)

    has_native_text = models.BooleanField(default=False)
    extraction_method = models.CharField(max_length=32, choices=ExtractionMethod.choices)
    ocr_confidence = models.FloatField(
        null=True, blank=True, help_text=_("Mean OCR confidence for the page (0–100).")
    )
    verification_status = models.CharField(
        max_length=32, choices=VerificationStatus.choices,
        default=VerificationStatus.UNVERIFIED, db_index=True,
    )
    language = models.CharField(max_length=8, blank=True)
    # Quality-gate signals (§8) — richer than a single average confidence.
    quality_signals = models.JSONField(
        default=dict, blank=True,
        help_text=_("malformed chars, broken words, spacing, numeric corruption, etc."),
    )
    rendered_image_path = models.CharField(
        max_length=512, blank=True,
        help_text=_("Rasterised page, kept only when vision work needed it."),
    )
    #: Degrees clockwise the scan was turned to read it (0/90/180/270).
    #: Boxes are stored on the page as it is in the file; a crop of this
    #: page is turned the same way before anyone reads it.
    ocr_rotation = models.IntegerField(default=0)
    kind = models.CharField(max_length=16, choices=PageKind.choices, default=PageKind.CONTRACT,
                            db_index=True)
    #: Why the page was given its kind, in a few words.
    kind_reason = models.CharField(max_length=255, blank=True)
    include_in_analysis = models.BooleanField(
        default=True, help_text=_("Analysed with the contract. The reviewer may change it."))
    #: The page this one repeats, when it is a duplicate.
    duplicate_of = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["page_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "page_number"], name="uniq_page_per_document"
            )
        ]
        indexes = [models.Index(fields=["document", "page_number"])]

    def __str__(self) -> str:
        return f"p.{self.page_number}"


class Block(TimeStampedModel):
    """A contiguous region of source text with its coordinates.

    `original_source_text` is immutable evidence. A vision repair writes to
    `vision_text` and flips `verification_status`; it never edits the original.
    """

    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="blocks")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="blocks")

    block_index = models.PositiveIntegerField(help_text=_("Reading order within the page."))
    original_source_text = models.TextField()

    # bounding box in page coordinates
    bbox_x0 = models.FloatField(null=True, blank=True)
    bbox_y0 = models.FloatField(null=True, blank=True)
    bbox_x1 = models.FloatField(null=True, blank=True)
    bbox_y1 = models.FloatField(null=True, blank=True)

    extraction_method = models.CharField(max_length=32, choices=ExtractionMethod.choices)
    ocr_confidence = models.FloatField(null=True, blank=True)
    min_word_confidence = models.FloatField(
        null=True, blank=True,
        help_text=_("Worst word in the block. A 96%% page can still hide a 72%% '10%%'."),
    )
    language = models.CharField(max_length=8, blank=True)

    verification_status = models.CharField(
        max_length=32, choices=VerificationStatus.choices,
        default=VerificationStatus.UNVERIFIED, db_index=True,
    )
    vision_text = models.TextField(
        blank=True, help_text=_("Vision reading of this region. Never overwrites the original.")
    )
    #: The text that stands after verification. Empty until verification has
    #: run (the original applies); for NEEDS_HUMAN_REVIEW it stays empty --
    #: no reading is chosen for a value nobody could confirm.
    final_text = models.TextField(blank=True)
    #: Why the readings disagreed, when they did ("critical_value: 0.5% vs 5%").
    mismatch_reason = models.CharField(max_length=255, blank=True)
    vision_confidence = models.FloatField(null=True, blank=True)
    #: [[word, confidence], ...] from OCR, so the confidence of the very word
    #: in dispute can be looked up later, not only the block's worst word.
    word_confidences = models.JSONField(default=list, blank=True)
    contains_critical_value = models.BooleanField(
        default=False, db_index=True,
        help_text=_("Amounts, percentages, dates, caps — verified to a stricter bar (§9)."),
    )
    #: The text after recorded rule repairs of known OCR misreadings
    #: (apps/documents/ocr/corrections.py) and second readings of figures.
    #: Empty when nothing was repaired. The original stays the evidence.
    corrected_text = models.TextField(blank=True)
    #: [{"kind", "from", "to", "note"}, ...] -- every repair, for the reviewer.
    corrections = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["page__page_number", "block_index"]
        indexes = [
            models.Index(fields=["document", "page"]),
            models.Index(fields=["verification_status"]),
            models.Index(fields=["contains_critical_value", "verification_status"]),
        ]

    def __str__(self) -> str:
        return f"block {self.block_index} p.{self.page.page_number}"

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        vals = (self.bbox_x0, self.bbox_y0, self.bbox_x1, self.bbox_y1)
        return vals if all(v is not None for v in vals) else None

    @property
    def usable_text(self) -> str:
        """What analysis reads: the verified text, else the original.

        For NEEDS_HUMAN_REVIEW `final_text` is empty, so the original OCR text
        is used -- and the block's status travels with it, so a finding built
        on it is marked as resting on an unresolved source. A block whose
        figures were repaired by rule uses the repaired text; the repair is
        recorded and shown for confirmation (KeyFact).
        """
        return self.final_text or self.corrected_text or self.original_source_text

    @property
    def unresolved_critical(self) -> bool:
        return (self.contains_critical_value
                and self.verification_status in (VerificationStatus.NEEDS_HUMAN_REVIEW,
                                                 VerificationStatus.SOURCE_CONFLICT))


class KeyFact(UUIDPrimaryKeyModel, TimeStampedModel):
    """One of the figures a contract review turns on, with every place the
    file states it -- and, once a person has looked, the confirmed value.

    Found by rule (apps/documents/key_facts.py), never by a language model.
    A scanned contract can misread exactly these figures; one stated two
    different delay-damage rates on two pages, another gave its duration as
    "Fifty (44) months". So each fact keeps all its readings, and a fact
    whose readings disagree, or rest on a repaired or uncertain reading, is
    shown to a reviewer before the analysis runs.
    """

    class Name(models.TextChoices):
        CONTRACT_VALUE = "contract_value", _("قيمة العقد")
        DURATION = "duration", _("مدة العقد")
        DELAY_DAMAGES_RATE = "delay_damages_rate", _("غرامة التأخير")
        DELAY_DAMAGES_CAP = "delay_damages_cap", _("سقف غرامات التأخير")
        PERFORMANCE_BOND = "performance_bond", _("كفالة حسن التنفيذ")
        ADVANCE_PAYMENT = "advance_payment", _("الدفعة المقدمة")
        RETENTION = "retention", _("المحتجزات")
        PAYMENT_DAYS = "payment_days", _("مدة الدفع")
        LIABILITY_CAP = "liability_cap", _("سقف المسؤولية")

    class Status(models.TextChoices):
        FOUND = "found", _("وُجدت قراءة واحدة")
        UNCERTAIN = "uncertain", _("قراءة غير مؤكدة")
        CONFLICT = "conflict", _("قراءات متعارضة")
        NOT_FOUND = "not_found", _("غير موجودة في الملف")
        CONFIRMED = "confirmed", _("أكّدها المراجع")
        #: Chosen by rule after the OCR + vision figure check, without asking
        #: anyone (apps/documents/figure_check.py); `note` says why.
        SETTLED = "settled", _("حسمها النظام")
        #: Not clear in the file -- handwritten, or unsure and not confirmed
        #: by a second reading -- so the user types it in before the analysis.
        NEEDS_INPUT = "needs_input", _("تحتاج إدخال القيمة")

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="key_facts")
    name = models.CharField(max_length=32, choices=Name.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NOT_FOUND,
                              db_index=True)
    #: [{"value", "display", "unit", "page", "bbox", "block", "confidence",
    #:   "repaired", "quote", "source"}, ...] -- every reading, in page order.
    candidates = models.JSONField(default=list, blank=True)
    #: The value a person confirmed (or typed), as it should be read.
    value = models.CharField(max_length=255, blank=True)
    unit = models.CharField(max_length=32, blank=True)
    note = models.CharField(max_length=500, blank=True)
    #: The settled value was read the same way by OCR and by vision.
    vision_checked = models.BooleanField(default=False)
    confirmed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["document", "name"]
        constraints = [
            models.UniqueConstraint(fields=["document", "name"], name="uniq_key_fact_per_document"),
        ]

    def __str__(self) -> str:
        return f"{self.get_name_display()}: {self.value or self.get_status_display()}"

    @property
    def needs_attention(self) -> bool:
        return self.status in (self.Status.CONFLICT, self.Status.UNCERTAIN, self.Status.NEEDS_INPUT)

    @property
    def distinct_values(self) -> list[str]:
        return list(dict.fromkeys(c.get("value", "") for c in self.candidates if c.get("value")))


class VisionVerification(UUIDPrimaryKeyModel, TimeStampedModel):
    """A vision check of one region, kept as evidence in its own right (§9)."""

    class Outcome(models.TextChoices):
        AGREE = "agree", _("Agree")
        DISAGREE = "disagree", _("Disagree")
        INCONCLUSIVE = "inconclusive", _("Inconclusive")
        ERROR = "error", _("Provider error")

    block = models.ForeignKey(
        Block, on_delete=models.CASCADE, related_name="vision_verifications"
    )
    reason = models.CharField(
        max_length=64,
        help_text=_("low_confidence | critical_value | table | bad_scan | retry"),
    )
    ocr_text = models.TextField(blank=True)
    vision_text = models.TextField(blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, db_index=True)
    provider = models.CharField(max_length=32, blank=True)
    model = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True)
    # ── the three-tier record: one row per vision pass ───────────────────
    #: 1 for the first reading, 2 for the stricter re-reading of a disputed
    #: critical value. 0 when no pass could be made (invalid crop, limit).
    pass_number = models.PositiveSmallIntegerField(default=1)
    vision_confidence = models.FloatField(null=True, blank=True)
    #: The status the block ended in after this pass.
    status = models.CharField(max_length=32, choices=VerificationStatus.choices, blank=True)
    final_text = models.TextField(blank=True)
    mismatch_reason = models.CharField(max_length=255, blank=True)
    #: Normalised similarity of this reading to OCR, 0-1.
    similarity = models.FloatField(null=True, blank=True)
    page_number = models.PositiveIntegerField(null=True, blank=True)
    #: [x0, y0, x1, y1] in PDF points (pixels for a photo), as cropped.
    bbox = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["block", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.reason} → {self.outcome}"



class ExtractedFile(UUIDPrimaryKeyModel, TimeStampedModel):
    """A file attached to an assistant chat, read once and kept.

    Separate from SourceFile/Document on purpose: those belong to a contract,
    and a chat attachment belongs to a person. Keyed by the owner and the
    SHA-256 of the bytes, so the same file attached again -- under any name,
    in any of that person's chats -- is never read twice. Never shared across
    owners: another user's extraction of the same bytes is not reused.
    """

    class Status(models.TextChoices):
        PROCESSING = "processing", _("Processing")
        READY = "ready", _("Ready")
        NEEDS_REVIEW = "needs_review", _("Ready — some values need review")
        FAILED = "failed", _("Failed")

    class Kind(models.TextChoices):
        PDF = "pdf", _("PDF")
        IMAGE = "image", _("Image")

    owner = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="extracted_files"
    )
    sha256 = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    original_filename = models.CharField(max_length=255)
    stored_path = models.CharField(
        max_length=512, help_text=_("Path relative to PRIVATE_MEDIA_ROOT. Never a public URL.")
    )
    size_bytes = models.BigIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PROCESSING, db_index=True)
    #: The final text of the whole file, page by page.
    text = models.TextField(blank=True)
    #: True when a page had a usable text layer and needed no OCR.
    native_text = models.BooleanField(default=False)
    vision_calls = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "sha256"], name="uniq_extracted_file_owner_hash"),
        ]

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.status})"


class ExtractedRegion(TimeStampedModel):
    """One region of an ExtractedFile, with every reading of it (§9).

    The same evidence a contract Block keeps: the source text, both vision
    passes when they ran, what was finally used, and why.
    """

    file = models.ForeignKey(ExtractedFile, on_delete=models.CASCADE, related_name="regions")
    page_number = models.PositiveIntegerField()
    block_index = models.PositiveIntegerField()
    bbox_x0 = models.FloatField(null=True, blank=True)
    bbox_y0 = models.FloatField(null=True, blank=True)
    bbox_x1 = models.FloatField(null=True, blank=True)
    bbox_y1 = models.FloatField(null=True, blank=True)
    #: "pt" for a PDF page, "px" for a photo.
    bbox_units = models.CharField(max_length=2, default="pt")
    extraction_method = models.CharField(max_length=32, choices=ExtractionMethod.choices)
    #: Exactly what the source gave: the PDF's own text, or Tesseract's reading.
    source_text = models.TextField()
    ocr_confidence = models.FloatField(null=True, blank=True)
    min_word_confidence = models.FloatField(null=True, blank=True)
    word_confidences = models.JSONField(default=list, blank=True)
    contains_critical_value = models.BooleanField(default=False, db_index=True)
    vision_pass1_text = models.TextField(blank=True)
    vision_pass1_confidence = models.FloatField(null=True, blank=True)
    vision_pass2_text = models.TextField(blank=True)
    vision_pass2_confidence = models.FloatField(null=True, blank=True)
    final_text = models.TextField(blank=True)
    status = models.CharField(max_length=32, choices=VerificationStatus.choices,
                              default=VerificationStatus.ACCEPTED, db_index=True)
    mismatch_reason = models.CharField(max_length=255, blank=True)
    similarity = models.FloatField(null=True, blank=True)
    provider = models.CharField(max_length=32, blank=True)
    model = models.CharField(max_length=64, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["page_number", "block_index"]
        indexes = [models.Index(fields=["file", "page_number"]),
                   models.Index(fields=["file", "status"])]

    @property
    def bbox(self):
        vals = (self.bbox_x0, self.bbox_y0, self.bbox_x1, self.bbox_y1)
        return vals if all(v is not None for v in vals) else None

    @property
    def usable_text(self) -> str:
        """What a question is answered from: the verified text, or the source
        text when verification did not choose one."""
        return self.final_text or self.source_text

    @property
    def unresolved(self) -> bool:
        return self.status == VerificationStatus.NEEDS_HUMAN_REVIEW


class ExtractedChunk(TimeStampedModel):
    """A clause (or, without clause numbers, a passage) of an ExtractedFile:
    the unit a question retrieves, so a follow-up is sent a few relevant
    sections rather than the whole file."""

    file = models.ForeignKey(ExtractedFile, on_delete=models.CASCADE, related_name="chunks")
    ordinal = models.PositiveIntegerField()
    clause_number = models.CharField(max_length=32, blank=True)
    heading = models.CharField(max_length=512, blank=True)
    page_start = models.PositiveIntegerField()
    page_end = models.PositiveIntegerField()
    text = models.TextField()
    #: Regions in this chunk still NEEDS_HUMAN_REVIEW.
    unresolved_region_ids = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["file", "ordinal"], name="uniq_extracted_chunk_ordinal"),
        ]
