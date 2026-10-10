"""Three-tier OCR / vision verification (§9, §10).

Tesseract is the primary reader. Vision is a second, *independent* reader of
one cropped region: it is never shown what OCR produced, and the comparison
happens here, in code -- never in the model.

    readings match                      -> VERIFIED
    differ only in non-critical detail,
    every critical value identical,
    normalised similarity >= threshold  -> AUTO_RESOLVED  (difference logged)
    a critical value differs            -> a second, stricter vision pass on a
                                           tighter, higher-resolution crop, then
        pass 2 exactly confirms OCR or pass 1,
        OCR was unsure of the disputed word,
        and the crop clearly holds the value -> VISION_REVERIFIED
        anything else                        -> NEEDS_HUMAN_REVIEW
    no crop, no provider, over the limit -> NEEDS_HUMAN_REVIEW, OCR text kept

Two agreeing vision passes are NOT treated as proof: pass 2 only counts when
it confirms a reading AND OCR itself was unsure. A critical value no reader
could settle is never chosen -- the final text stays empty and the original
OCR text is what a person reviews.

This module holds the decision only. The callers (contract Blocks in
services.py, chat attachments in chat_files.py) supply the crop and the
reader, and store what comes back.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from django.conf import settings

from .critical_values import critical_signature, normalise_digits
from .models import VerificationStatus

# ── comparing two readings ───────────────────────────────────────────────

_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})
# Punctuation that never changes what a contract says. Decimal points, percent
# signs, slashes, hyphens and currency are NOT here: they carry values.
_HARMLESS = re.compile(r"[\"'`´‘’“”«»()\[\]{}؛;:,،!?؟…•·*_|]")
_SPACE = re.compile(r"\s+")
_SENTENCE_STOP = re.compile(r"\.(?!\d)")


def normalise_for_compare(text: str) -> str:
    """A reading with the differences that never matter removed.

    Diacritics, tatweel, the alef/hamza and ya/alef-maqsura variants that OCR
    swaps freely, digit scripts, case, quotes and brackets, and spacing. Used
    only for comparing; nothing stored is normalised.
    """
    out = unicodedata.normalize("NFKC", text or "")
    out = _TASHKEEL.sub("", out).replace(_TATWEEL, "")
    out = out.translate(_ALEF)
    out = normalise_digits(out)
    out = out.replace("٪", "%").replace("٫", ".")
    out = _HARMLESS.sub(" ", out)
    # A full stop that is not a decimal point ("limit." vs "limit"); "0.5"
    # and "4.3" keep theirs.
    out = _SENTENCE_STOP.sub(" ", out)
    return _SPACE.sub(" ", out).strip().casefold()


def similarity(a: str, b: str) -> float:
    """0-1 similarity of two readings after `normalise_for_compare`."""
    na, nb = normalise_for_compare(a), normalise_for_compare(b)
    if not na and not nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()


# ── what the caller supplies ─────────────────────────────────────────────

@dataclass
class Region:
    """The region being verified and what OCR said about it."""

    ocr_text: str
    page_number: int
    bbox: tuple[float, float, float, float] | None
    ocr_confidence: float | None = None
    min_word_confidence: float | None = None
    #: [[word, confidence], ...] as OCR produced them.
    word_confidences: list = field(default_factory=list)
    language: str = "ar"
    known_parties: tuple[str, ...] = ()
    #: Native PDF text is exact; it is never sent to vision.
    is_native: bool = False


@dataclass
class PassRecord:
    """One vision reading."""

    number: int
    text: str = ""
    confidence: float | None = None
    notes: str = ""
    provider: str = ""
    model: str = ""
    error: str = ""
    similarity: float | None = None

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class Resolution:
    status: str
    #: The text that stands. Empty for NEEDS_HUMAN_REVIEW: no reading chosen.
    final_text: str
    mismatch_reason: str = ""
    similarity: float | None = None
    passes: list[PassRecord] = field(default_factory=list)
    #: Vision calls actually made (0-2).
    calls: int = 0

    @property
    def unresolved(self) -> bool:
        return self.status == VerificationStatus.NEEDS_HUMAN_REVIEW


class VisionBudget:
    """The per-page and per-file limit on vision calls (§13).

    A bad scan can produce dozens of weak blocks; one upload must not occupy
    the shared AI server for minutes. Callers verify critical regions first,
    so the limit falls on non-critical leftovers.
    """

    def __init__(self, per_page: int | None = None, per_file: int | None = None):
        self.per_page = per_page if per_page is not None else settings.VISION_MAX_CALLS_PER_PAGE
        self.per_file = per_file if per_file is not None else settings.VISION_MAX_CALLS_PER_FILE
        self._pages: dict[int, int] = {}
        self.used = 0

    def can_spend(self, page_number: int, calls: int = 1) -> bool:
        return (self.used + calls <= self.per_file
                and self._pages.get(page_number, 0) + calls <= self.per_page)

    def spend(self, page_number: int, calls: int = 1) -> None:
        self.used += calls
        self._pages[page_number] = self._pages.get(page_number, 0) + calls


#: crop(tight: bool) -> PNG bytes; raises extractors.ExtractionError.
CropFn = Callable[[bool], bytes]
#: read(image, strict: bool, number: int) -> PassRecord; never raises.
ReadFn = Callable[[bytes, bool, int], PassRecord]


# ── the decision ─────────────────────────────────────────────────────────

def _disputed_confidence(region: Region, disputed: set[str]) -> float | None:
    """OCR's confidence in the very words the dispute is about.

    The lowest confidence among OCR words that carry a disputed value; when
    no word can be matched, the block's worst word -- never an optimistic
    average.
    """
    wanted = {normalise_for_compare(v.split(":", 1)[-1]) for v in disputed if not v.startswith("party:")}
    wanted = {w for w in wanted if w}
    # A party name is compared as its letters run together
    # ("party:ajiengineeringconsultants"): every word of it is part of it.
    names = {v.split(":", 1)[1] for v in disputed if v.startswith("party:")}
    hits, words = [], []
    for item in region.word_confidences or ():
        try:
            word, conf = item[0], float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        norm = normalise_for_compare(word)
        if any(ch.isalnum() for ch in norm):
            words.append(conf)
        # A word counts when it is the value, holds it, or is a substantial
        # part of it -- not a stray "5" that happens to occur in "0.5%".
        if len(norm) >= 2 and any(w in norm or (norm in w and len(norm) * 2 >= len(w))
                                  for w in wanted):
            hits.append(conf)
        key = re.sub(r"[\W_]+", "", norm)
        if len(key) >= 2 and any(key in name for name in names):
            hits.append(conf)
    if hits:
        return min(hits)
    # The value's own words cannot be told apart (OCR may have missed it
    # altogether): the worst real word, never a stray ":" read at 3%.
    if words:
        return min(words)
    return region.min_word_confidence if region.min_word_confidence is not None else region.ocr_confidence


def _describe(values: set[str]) -> str:
    return ", ".join(sorted(values)) or "none"


def reconcile(region: Region, crop: CropFn, read: ReadFn,
              budget: VisionBudget | None = None, *, reason: str = "") -> Resolution:
    """Verify one region and decide what text stands (see module docstring)."""
    if region.is_native:
        return Resolution(VerificationStatus.ACCEPTED, region.ocr_text)

    if budget is not None and not budget.can_spend(region.page_number):
        return Resolution(VerificationStatus.NEEDS_HUMAN_REVIEW, "", "vision_limit")

    from .extractors import ExtractionError

    if region.bbox is None:
        return Resolution(VerificationStatus.NEEDS_HUMAN_REVIEW, "", "no_bounding_box")
    try:
        image = crop(False)
    except ExtractionError as exc:
        return Resolution(VerificationStatus.NEEDS_HUMAN_REVIEW, "", f"invalid_crop: {exc}"[:255])

    if budget is not None:
        budget.spend(region.page_number)
    first = read(image, False, 1)
    if not first.ok:
        return Resolution(VerificationStatus.NEEDS_HUMAN_REVIEW, "",
                          f"vision_unavailable: {first.error}"[:255], passes=[first], calls=1)

    parties = region.known_parties
    ocr_values = critical_signature(region.ocr_text, parties)
    v1_values = critical_signature(first.text, parties)
    sim = similarity(region.ocr_text, first.text)
    first.similarity = sim

    # A. and B.: every critical value is the same.
    if ocr_values == v1_values:
        if normalise_for_compare(region.ocr_text) == normalise_for_compare(first.text):
            return Resolution(VerificationStatus.VERIFIED, region.ocr_text, "", sim, [first], 1)
        threshold = settings.VISION_AUTO_RESOLVE_SIMILARITY
        if sim >= threshold:
            # OCR's text stands; the difference stays on the record.
            return Resolution(VerificationStatus.AUTO_RESOLVED, region.ocr_text,
                              f"minor_difference: similarity {sim:.3f}", sim, [first], 1)
        return Resolution(VerificationStatus.NEEDS_HUMAN_REVIEW, "",
                          f"text_mismatch: similarity {sim:.3f} below {threshold}", sim, [first], 1)

    # C.: a critical value differs. Never guess; look again, closer.
    disputed = ocr_values ^ v1_values
    reason_text = (f"critical_value: OCR [{_describe(ocr_values - v1_values)}] "
                   f"vs vision [{_describe(v1_values - ocr_values)}]")[:255]
    if budget is not None and not budget.can_spend(region.page_number):
        return Resolution(VerificationStatus.NEEDS_HUMAN_REVIEW, "",
                          (reason_text + "; vision_limit")[:255], sim, [first], 1)
    try:
        tight = crop(True)
    except ExtractionError as exc:
        return Resolution(VerificationStatus.NEEDS_HUMAN_REVIEW, "",
                          (reason_text + f"; invalid_crop: {exc}")[:255], sim, [first], 1)
    if budget is not None:
        budget.spend(region.page_number)
    second = read(tight, True, 2)
    passes = [first, second]
    if not second.ok:
        return Resolution(VerificationStatus.NEEDS_HUMAN_REVIEW, "",
                          (reason_text + "; pass 2 unavailable")[:255], sim, passes, 2)
    second.similarity = similarity(region.ocr_text, second.text)
    v2_values = critical_signature(second.text, parties)

    confirms_ocr = v2_values == ocr_values
    confirms_v1 = v2_values == v1_values
    ocr_conf = _disputed_confidence(region, disputed)
    ocr_unsure = ocr_conf is not None and ocr_conf < settings.VISION_REVERIFY_OCR_CONFIDENCE
    # The tight crop must visibly hold a value of the disputed kind, and pass 2
    # must not have marked any character as unreadable.
    holds_value = bool(v2_values & disputed) and "[?]" not in second.text

    if (confirms_ocr != confirms_v1) and ocr_unsure and holds_value:
        chosen = region.ocr_text if confirms_ocr else first.text
        which = "OCR" if confirms_ocr else "vision pass 1"
        return Resolution(VerificationStatus.VISION_REVERIFIED, chosen,
                          (reason_text + f"; pass 2 confirmed {which}; "
                           f"OCR confidence {ocr_conf:.0f}")[:255], sim, passes, 2)

    why = []
    if not (confirms_ocr or confirms_v1):
        why.append("pass 2 matched neither reading")
    elif not ocr_unsure:
        why.append("OCR was confident" + (f" ({ocr_conf:.0f})" if ocr_conf is not None else ""))
    if not holds_value:
        why.append("value not clearly readable in pass 2")
    return Resolution(VerificationStatus.NEEDS_HUMAN_REVIEW, "",
                      (reason_text + "; " + "; ".join(why or ["unresolved"]))[:255], sim, passes, 2)


# ── one dispute, the whole file ──────────────────────────────────────────

def disputed_values(readings, known_parties=()) -> dict[str, int]:
    """{value: page} for every critical value OCR read that a vision reading
    of the same region contradicted and nobody could settle.

    `readings`: (ocr_text, vision_text, page_number) of the unresolved
    regions. Measured live: OCR read "AJi" as "Adi" at 92-96% in two footers.
    Vision was asked about one of them and disagreed; the other, read just as
    confidently, was never asked. The same reading elsewhere in the file is
    no more trustworthy than the one that was disputed.
    """
    out: dict[str, int] = {}
    for ocr_text, vision_text, page in readings:
        if not (vision_text or "").strip():
            continue
        for value in critical_signature(ocr_text, known_parties) - critical_signature(vision_text, known_parties):
            out.setdefault(value, page)
    return out


def shares_a_disputed_value(text: str, disputed: dict[str, int], known_parties=()) -> str:
    """The reason to hold `text` for review, or "" when it has none."""
    hits = sorted(critical_signature(text, known_parties) & disputed.keys())
    if not hits:
        return ""
    return (f"same_value_disputed: {', '.join(hits)} (vision disagreed on page "
            f"{disputed[hits[0]]})")[:255]


# ── ordering the work (§13) ──────────────────────────────────────────────

def priority(contains_critical: bool, min_word_confidence: float | None,
             ocr_confidence: float | None) -> tuple:
    """Sort key: critical values first, then the least confident."""
    conf = min_word_confidence if min_word_confidence is not None else (ocr_confidence or 0.0)
    return (0 if contains_critical else 1, conf)


def word_confidences(block) -> list:
    """[[word, confidence], ...] of an OCRBlock, in reading order."""
    return [[w.text, round(float(w.confidence), 1)]
            for line in getattr(block, "lines", ()) for w in line.words]


def vision_reader(*, language: str, page_number: int, bbox, contract_id="") -> ReadFn:
    """A ReadFn over the configured vision provider. Errors become records."""

    def read(image: bytes, strict: bool, number: int) -> PassRecord:
        from apps.ai.providers.base import AIProviderError, ImageRegion
        from apps.ai.services import ai_service

        try:
            call = ai_service.verify_text_region(
                region=ImageRegion(image_bytes=image, page_number=page_number, bbox=bbox),
                language=language or "ar", strict=strict, contract_id=contract_id,
            )
        except AIProviderError as exc:
            return PassRecord(number=number, error=str(exc)[:500] or exc.__class__.__name__)
        except Exception as exc:          # a provider bug must not stop extraction
            return PassRecord(number=number, error=f"{exc.__class__.__name__}: {exc}"[:500])
        parsed = call.parsed
        return PassRecord(number=number, text=parsed.text, confidence=parsed.confidence,
                          notes=parsed.notes, provider=call.provider, model=call.model)

    return read
