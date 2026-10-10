"""The OCR quality gate (§8).

Explicitly *not* a single average-confidence check. A mean hides exactly the
failures that matter: one corrupted number in an otherwise clean page.

Every threshold comes from settings so Legal or IT can retune them without a
developer (§50).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from django.conf import settings

from .critical_values import find_critical_values, normalise_digits
from .ocr.base import OCRBlock, OCRPageResult


class GateDecision(str, Enum):
    ACCEPT = "accept"                      # >= accept threshold
    ACCEPT_WITH_VALIDATION = "validate"    # validate band
    VISION_VERIFY = "vision_verify"        # below validate band
    VISION_VERIFY_AND_RETRY = "retry"      # below vision band — worst case


@dataclass
class QualitySignals:
    """Measurable evidence behind the decision — stored on the Page."""

    mean_confidence: float = 0.0
    min_word_confidence: float | None = None
    low_confidence_word_ratio: float = 0.0
    malformed_char_ratio: float = 0.0
    broken_word_ratio: float = 0.0
    suspicious_spacing_ratio: float = 0.0
    numeric_corruption_count: int = 0
    script_mix_ratio: float = 0.0
    word_count: int = 0
    critical_value_count: int = 0
    critical_values_below_bar: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mean_confidence": round(self.mean_confidence, 2),
            "min_word_confidence": (
                round(self.min_word_confidence, 2) if self.min_word_confidence is not None else None
            ),
            "low_confidence_word_ratio": round(self.low_confidence_word_ratio, 4),
            "malformed_char_ratio": round(self.malformed_char_ratio, 4),
            "broken_word_ratio": round(self.broken_word_ratio, 4),
            "suspicious_spacing_ratio": round(self.suspicious_spacing_ratio, 4),
            "numeric_corruption_count": self.numeric_corruption_count,
            "script_mix_ratio": round(self.script_mix_ratio, 4),
            "word_count": self.word_count,
            "critical_value_count": self.critical_value_count,
            "critical_values_below_bar": self.critical_values_below_bar,
            "reasons": self.reasons,
        }


@dataclass
class GateResult:
    decision: GateDecision
    signals: QualitySignals
    blocks_needing_vision: list[int] = field(default_factory=list)

    @property
    def needs_vision(self) -> bool:
        return self.decision in {
            GateDecision.VISION_VERIFY, GateDecision.VISION_VERIFY_AND_RETRY
        } or bool(self.blocks_needing_vision)


# Characters Tesseract emits when it is guessing at a glyph it cannot resolve.
_MALFORMED = re.compile("[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]")
_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[A-Za-z]")
# A digit stuck to a letter, e.g. "1O%" or "٥O" — a classic OCR confusion.
_NUMERIC_CORRUPTION = re.compile(r"(?<=\d)[OolIB|](?=\d)|(?<=\d)[^\d\s.,%٪/\-](?=\d)")
_SUSPICIOUS_SPACING = re.compile(r"\S\s{3,}\S")


def _thresholds() -> dict[str, int]:
    return settings.OCR_THRESHOLDS


def evaluate_page(result: OCRPageResult) -> GateResult:
    """Decide what to do with one OCR'd page."""
    t = _thresholds()
    words = [w for b in result.blocks for line in b.lines for w in line.words]
    text = result.text
    signals = QualitySignals(mean_confidence=result.mean_confidence, word_count=len(words))

    if not words:
        signals.reasons.append("no_text_recognised")
        return GateResult(GateDecision.VISION_VERIFY_AND_RETRY, signals)

    confidences = [w.confidence for w in words]
    signals.min_word_confidence = min(confidences)
    signals.low_confidence_word_ratio = sum(
        1 for c in confidences if c < t["validate"]
    ) / len(confidences)

    chars = max(len(text), 1)
    signals.malformed_char_ratio = len(_MALFORMED.findall(text)) / chars
    signals.suspicious_spacing_ratio = len(_SUSPICIOUS_SPACING.findall(text)) / chars
    signals.numeric_corruption_count = len(_NUMERIC_CORRUPTION.findall(normalise_digits(text)))

    # A single-word "word" of one character, repeated, usually means the
    # engine fragmented real words.
    singles = sum(1 for w in words if len(w.text) == 1 and w.text.isalpha())
    signals.broken_word_ratio = singles / len(words)

    arabic_chars = len(_ARABIC.findall(text))
    latin_chars = len(_LATIN.findall(text))
    if arabic_chars and latin_chars:
        # Bilingual contracts are normal; heavy interleaving is not.
        signals.script_mix_ratio = min(arabic_chars, latin_chars) / (arabic_chars + latin_chars)

    # ── critical values face a stricter bar (§9) ─────────────────────────
    blocks_needing_vision: list[int] = []
    critical_total = 0
    critical_below = 0
    for index, block in enumerate(result.blocks):
        matches = find_critical_values(block.text)
        if not matches:
            continue
        critical_total += len(matches)
        worst = _worst_confidence_for_matches(block, matches)
        if worst is not None and worst < t["critical_value"]:
            critical_below += len(matches)
            blocks_needing_vision.append(index)
            signals.reasons.append(
                f"block_{index}_critical_value_confidence_{worst:.0f}"
            )
    signals.critical_value_count = critical_total
    signals.critical_values_below_bar = critical_below

    # ── overall decision ─────────────────────────────────────────────────
    mean = result.mean_confidence
    if mean < t["vision"]:
        signals.reasons.append(f"mean_confidence_{mean:.0f}_below_{t['vision']}")
        decision = GateDecision.VISION_VERIFY_AND_RETRY
    elif mean < t["validate"]:
        signals.reasons.append(f"mean_confidence_{mean:.0f}_below_{t['validate']}")
        decision = GateDecision.VISION_VERIFY
    elif mean < t["accept"]:
        signals.reasons.append(f"mean_confidence_{mean:.0f}_below_{t['accept']}")
        decision = GateDecision.ACCEPT_WITH_VALIDATION
    else:
        decision = GateDecision.ACCEPT

    # Structural damage overrides a comfortable average — this is the whole
    # point of not trusting the mean alone.
    if signals.malformed_char_ratio > 0.02:
        signals.reasons.append("malformed_characters")
        decision = _at_least(decision, GateDecision.VISION_VERIFY)
    if signals.numeric_corruption_count:
        signals.reasons.append("numeric_corruption")
        decision = _at_least(decision, GateDecision.VISION_VERIFY)
    if signals.broken_word_ratio > 0.30:
        signals.reasons.append("fragmented_words")
        decision = _at_least(decision, GateDecision.VISION_VERIFY)
    if signals.low_confidence_word_ratio > 0.25:
        signals.reasons.append("many_low_confidence_words")
        decision = _at_least(decision, GateDecision.ACCEPT_WITH_VALIDATION)

    return GateResult(decision, signals, blocks_needing_vision)


def _worst_confidence_for_matches(block: OCRBlock, matches) -> float | None:
    """Lowest word confidence among the words forming any critical value.

    Word offsets are not tracked back to character positions, so this errs
    toward caution: any word whose text appears inside a matched value counts.
    """
    tokens: set[str] = set()
    for match in matches:
        tokens.update(part for part in match.raw.split() if part)
    if not tokens:
        return None

    relevant = [
        w.confidence
        for line in block.lines
        for w in line.words
        if any(tok in w.text or w.text in tok for tok in tokens)
    ]
    return min(relevant) if relevant else None


_ORDER = [
    GateDecision.ACCEPT,
    GateDecision.ACCEPT_WITH_VALIDATION,
    GateDecision.VISION_VERIFY,
    GateDecision.VISION_VERIFY_AND_RETRY,
]


def _at_least(current: GateDecision, minimum: GateDecision) -> GateDecision:
    """Escalate severity; never downgrade."""
    return current if _ORDER.index(current) >= _ORDER.index(minimum) else minimum
