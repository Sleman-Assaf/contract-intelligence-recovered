"""Strict schemas for model output (§44).

Nothing a model returns is written to a business record until it has passed
through one of these. Unvalidated output is treated as a failed call, not as
data with some fields missing.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Compliance(str, Enum):
    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class VerifierVerdict(str, Enum):
    AGREE = "agree"
    DISAGREE = "disagree"
    AMBIGUOUS = "ambiguous"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class _Strict(BaseModel):
    """Reject unexpected keys — a model inventing fields is a signal, not noise."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClauseClassification(_Strict):
    """§16 — uncertainty is expressed, not forced into false confidence."""

    primary: str = Field(..., min_length=1, max_length=64)
    alternatives: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field("", max_length=2000)

    @field_validator("alternatives", mode="before")
    @classmethod
    def _dedupe(cls, v):
        # The most likely five, in the model's order: six alternatives failed
        # a whole classification on a real run, for the least likely topic.
        if isinstance(v, list):
            return list(dict.fromkeys(v))[:5]
        return v


class BatchedClassification(ClauseClassification):
    """One clause's classification inside a batch, tied to it by id."""

    id: int


class ClauseClassificationBatch(_Strict):
    results: list[BatchedClassification] = Field(..., max_length=20)


class TextRegionVerification(_Strict):
    """§9 — what vision read in a cropped region, for comparison against OCR."""

    text: str = Field(..., max_length=8000)
    # No longer asked for: the model is not shown the OCR reading, so it has
    # nothing to match against. Tolerated if a model volunteers it.
    matches_ocr: bool | None = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    notes: str = Field("", max_length=1000)


class ClauseProse(_Strict):
    """One language's wording of a finding. Same judgement, different words."""

    title: str = Field("", max_length=255)
    deviation: str = Field("", max_length=4000)
    explanation: str = Field("", max_length=4000)
    business_impact: str = Field("", max_length=4000)
    recommended_action: str = Field("", max_length=4000)


class SemanticAssessment(_Strict):
    """§19 — the model's reading of one clause against one standard.

    It reports a deviation; it does not set the risk level. Risk is computed
    from the standard's impact and the deterministic result (§17, §25).
    """

    ai_result: Compliance
    deviation: str = Field("", max_length=4000)
    explanation: str = Field("", max_length=4000)
    business_impact: str = Field("", max_length=4000)
    recommended_action: str = Field("", max_length=4000)
    suggested_wording: str = Field("", max_length=8000)
    exclusions_found: list[str] = Field(
        default_factory=list, max_length=20,
        description="Clauses that weaken the protection, e.g. carve-outs from a liability cap.",
    )
    referenced_clauses: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(..., ge=0.0, le=1.0)

    #: The same finding written for each reader, keyed by language code. Not
    #: required: a provider that ignores it leaves the finding monolingual
    #: rather than failing the whole run.
    prose: dict[str, ClauseProse] = Field(default_factory=dict)

    #: A reading aid for the quoted clause, keyed by language code. Never used
    #: as contract wording -- see Finding.wording_translations.
    clause_translation: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _drop_language_copies(cls, data):
        """A known field repeated per language (`recommended_action_en`) is a
        translation the prompt no longer asks for, not a new judgement: drop
        it rather than fail the clause. Any other unknown key still fails."""
        if not isinstance(data, dict):
            return data
        fields = cls.model_fields
        return {key: value for key, value in data.items()
                if key in fields or not (isinstance(key, str) and "_" in key
                                         and key.rsplit("_", 1)[0] in fields
                                         and key.rsplit("_", 1)[1] in {"ar", "en"})}


class ProseTranslation(_Strict):
    """An existing finding restated in another language. No new judgement."""

    title: str = Field("", max_length=255)
    deviation: str = Field("", max_length=4000)
    explanation: str = Field("", max_length=4000)
    business_impact: str = Field("", max_length=4000)
    recommended_action: str = Field("", max_length=4000)

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_strays(cls, data):
        """An unknown key with nothing in it -- measured: a garbled
        "business_impayment": "" -- carries no translation; drop it rather
        than lose the finding's. An unknown key with content still fails."""
        if not isinstance(data, dict):
            return data
        return {k: v for k, v in data.items()
                if k in cls.model_fields or (v not in ("", None, [], {}))}

    @field_validator("title", mode="before")
    @classmethod
    def _fit_title(cls, value):
        """A title is the deviation cut to 255 characters; translated, it can
        run longer. It is a label and is cut on save anyway -- shorten it
        rather than lose the whole translation."""
        return value.strip()[:255] if isinstance(value, str) else value


class ClauseTranslation(_Strict):
    """A clause rendered in another language, for reading only."""

    text: str = Field("", max_length=8000)


class VerificationResult(_Strict):
    """§21 — an independent second pass, given a different task to the first."""

    verdict: VerifierVerdict
    evidence: str = Field("", max_length=4000)
    issues_found: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(..., ge=0.0, le=1.0)


#: Citations kept under one answer; more are cut, not refused.
MAX_REFERENCES = 30


class AdvisorAnswer(_Strict):
    """§38 — the assistant must separate source, interpretation and standard."""

    # Room for a translated section (~1,500 words of Arabic is ~9,000
    # characters); an answer past this is refused, not truncated.
    answer: str = Field(..., max_length=30000)
    source_references: list[str] = Field(default_factory=list, max_length=MAX_REFERENCES)
    standards_referenced: list[str] = Field(default_factory=list, max_length=MAX_REFERENCES)
    is_interpretation: bool = True

    @field_validator("source_references", "standards_referenced", mode="before")
    @classmethod
    def _fit_references(cls, value):
        """De-duplicated and cut to MAX_REFERENCES, never refused. Measured: a
        page-by-page summary of a whole attached contract cited all 55
        excerpts, and the answer -- which was fine -- was thrown away over the
        length of its citation list."""
        if not isinstance(value, list):
            return value
        return list(dict.fromkeys(str(v) for v in value))[:MAX_REFERENCES]


class NormalizedPosition(_Strict):
    """One position of an uploaded standards document, in the uniform shape.

    Restated, not invented: `source_quote` is copied from the document and
    checked against it before a person ever sees the position.
    """

    category: str = Field(..., min_length=1, max_length=64)
    preferred_position: str = Field("", max_length=4000)
    preferred_position_ar: str = Field("", max_length=4000)
    acceptable_range: str = Field("", max_length=4000)
    red_flags: str = Field("", max_length=4000)
    approved_fallback_wording: str = Field("", max_length=6000)
    exceptions: str = Field("", max_length=2000)
    approval_authority: str = Field("", max_length=160)
    responsible_department: str = Field("", max_length=64)
    #: Only when the document states it. Impact is Legal's to set.
    impact: int | None = Field(None, ge=1, le=5)
    #: A machine-checkable threshold the document states outright, or {}.
    rule: dict = Field(default_factory=dict)
    source_quote: str = Field("", max_length=2000)
    source_section: str = Field("", max_length=120)


class StandardExtraction(_Strict):
    positions: list[NormalizedPosition] = Field(default_factory=list, max_length=60)
    notes: str = Field("", max_length=2000)
