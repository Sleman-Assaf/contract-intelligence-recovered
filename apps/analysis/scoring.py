"""Risk scoring and system confidence (§24, §25).

Two things are deliberately kept apart:

* **Impact** is set by Legal on the standard. How much this clause can cost AJi
  is a policy judgement, not something a model infers from a sentence.
* **Probability** is derived from how far the contract actually deviates.

The previous system scored `high → 4×4`, `medium → 3×3`, `low → 2×2`, which
made the risk matrix decorative: only the diagonal was ever used and the score
carried no information the label didn't already. Here the two axes move
independently (§25).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ComplianceResult, VerificationOutcome

#: Impact and probability are 1–5 (§25).
MIN_SCALE, MAX_SCALE = 1, 5
DEFAULT_IMPACT = 3          # used only when Legal has not rated the standard

#: Score bands over impact × probability (1–25).
HIGH_THRESHOLD = 12
MEDIUM_THRESHOLD = 6


@dataclass
class RiskScore:
    impact: int
    probability: int
    score: int
    level: str
    rationale: list[str] = field(default_factory=list)


def _clamp(value: int) -> int:
    return max(MIN_SCALE, min(MAX_SCALE, value))


def probability_from(
    *,
    system_result: str,
    ai_result: str,
    verification: str,
    ai_confidence: float | None = None,
) -> tuple[int, list[str]]:
    """How likely is it that this deviation is real and will bite?

    Driven by evidence strength, not by the risk label — that is what stops the
    matrix collapsing onto its diagonal.
    """
    rationale: list[str] = []

    if system_result == ComplianceResult.NON_COMPLIANT:
        if ai_result == ComplianceResult.NON_COMPLIANT:
            probability = 5
            rationale.append("rules and AI both find non-compliance")
        elif verification == VerificationOutcome.SYSTEM_AI_CONFLICT:
            # Arithmetic says it breaches; the model disagrees. The breach is
            # still probable — the conflict is what routes it to a human.
            probability = 4
            rationale.append("deterministic breach, AI disagrees — conflict raised")
        else:
            probability = 4
            rationale.append("deterministic breach")

    elif system_result == ComplianceResult.COMPLIANT:
        if ai_result == ComplianceResult.NON_COMPLIANT:
            # The classic case: a cap exists but exclusions hollow it out.
            probability = 3
            rationale.append("rules pass but AI found a semantic weakness")
        else:
            probability = 1
            rationale.append("compliant on both readings")

    elif system_result in {ComplianceResult.UNKNOWN, ComplianceResult.AMBIGUOUS}:
        probability = 3
        rationale.append("evidence insufficient for a deterministic verdict")

    elif system_result == ComplianceResult.NO_STANDARD_FOUND:
        probability = 2
        rationale.append("no applicable AJi standard")

    else:                                    # NOT_APPLICABLE — AI only
        probability = 3 if ai_result == ComplianceResult.NON_COMPLIANT else 1
        rationale.append("no machine-checkable rule; AI assessment only")

    # A model that is unsure should not push probability to the top.
    if ai_confidence is not None and ai_confidence < 0.5 and probability > 2:
        probability -= 1
        rationale.append(f"reduced for low AI confidence ({ai_confidence:.2f})")

    return _clamp(probability), rationale


def score_finding(
    *,
    standard_impact: int | None,
    system_result: str,
    ai_result: str,
    verification: str,
    ai_confidence: float | None = None,
) -> RiskScore:
    """Impact × probability, with the reasoning kept for the audit trail."""
    rationale: list[str] = []

    if standard_impact is None:
        impact = DEFAULT_IMPACT
        rationale.append(
            f"standard has no impact rating; defaulted to {DEFAULT_IMPACT} — "
            "ask Legal to rate it"
        )
    else:
        impact = _clamp(int(standard_impact))
        rationale.append(f"impact {impact} set by Legal on the standard")

    probability, why = probability_from(
        system_result=system_result,
        ai_result=ai_result,
        verification=verification,
        ai_confidence=ai_confidence,
    )
    rationale.extend(why)

    score = impact * probability
    if score >= HIGH_THRESHOLD:
        level = "high"
    elif score >= MEDIUM_THRESHOLD:
        level = "medium"
    else:
        level = "low"

    # Uncertainty must not masquerade as a low risk. The previous system
    # displayed unknown as low, which quietly hid real gaps (§54).
    if system_result in {ComplianceResult.UNKNOWN, ComplianceResult.AMBIGUOUS} or \
            verification in {
                VerificationOutcome.SOURCE_CONFLICT,
                VerificationOutcome.NEEDS_HUMAN_REVIEW,
                VerificationOutcome.AMBIGUOUS,
            }:
        if level == "low":
            level = "medium"
            rationale.append("raised from low: unresolved uncertainty must stay visible")

    return RiskScore(
        impact=impact, probability=probability, score=score,
        level=level, rationale=rationale,
    )


# ── system confidence (§24) ──────────────────────────────────────────────

def system_confidence(
    *,
    source_confidence: float | None,
    standard_confidence: float | None,
    rule_confidence: float | None,
    ai_confidence: float | None,
    analyzer_verifier_agree: bool | None = None,
) -> tuple[float, dict]:
    """Confidence computed from measurable signals, not self-reported by a model.

    A model's own confidence is one input among several and is deliberately the
    least weighted: it is the only one that cannot be independently checked.
    """
    weights = {
        "source": 0.30,        # how well the text was extracted
        "standard": 0.20,      # how certain the standard match is
        "rule": 0.30,          # whether arithmetic settled it
        "ai": 0.20,            # what the model says about itself
    }
    values = {
        "source": source_confidence,
        "standard": standard_confidence,
        "rule": rule_confidence,
        "ai": ai_confidence,
    }

    total_weight = 0.0
    accumulated = 0.0
    breakdown: dict[str, float | None] = {}

    for key, weight in weights.items():
        value = values[key]
        breakdown[key] = value
        if value is None:
            continue
        normalised = value / 100.0 if value > 1.0 else value
        accumulated += normalised * weight
        total_weight += weight

    confidence = (accumulated / total_weight) if total_weight else 0.0

    if analyzer_verifier_agree is False:
        # Two passes disagreeing is strong evidence against trusting either.
        confidence *= 0.5
        breakdown["verifier_disagreement_penalty"] = 0.5

    breakdown["weights"] = weights
    return round(min(max(confidence, 0.0), 1.0), 4), breakdown
