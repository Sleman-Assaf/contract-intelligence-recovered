"""The deterministic rules engine (§17, §18, §55).

If AJi's standard says the delay-damages cap must be at most 10% and the
contract says 15%, the application decides:

    15 > 10  →  NON_COMPLIANT

That is arithmetic. It is not asked of a language model, it does not vary
between runs, and it can be explained to an arbitrator.

The engine's verdict is authoritative for objective facts. A model may later
disagree — and when it does the result is SYSTEM_AI_CONFLICT and a human
decides. The model never silently overwrites this.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import ComplianceResult
from .value_extraction import ParsedValue, extract_for_rule


@dataclass
class RuleOutcome:
    """A deterministic verdict plus the arithmetic that produced it."""

    result: str                       # ComplianceResult
    detail: dict = field(default_factory=dict)
    confidence: float = 1.0           # deterministic comparisons are certain

    @property
    def is_conclusive(self) -> bool:
        return self.result in {ComplianceResult.COMPLIANT, ComplianceResult.NON_COMPLIANT}


# ── comparison operators ─────────────────────────────────────────────────
# Named rather than eval'd: a rule definition is data authored by Legal, and
# data must never become executable code.

def _lte(actual: Decimal, expected: Decimal) -> bool:
    return actual <= expected


def _gte(actual: Decimal, expected: Decimal) -> bool:
    return actual >= expected


def _lt(actual: Decimal, expected: Decimal) -> bool:
    return actual < expected


def _gt(actual: Decimal, expected: Decimal) -> bool:
    return actual > expected


def _eq(actual: Decimal, expected: Decimal) -> bool:
    return actual == expected


OPERATORS = {
    "lte": (_lte, "must be at most"),
    "gte": (_gte, "must be at least"),
    "lt": (_lt, "must be less than"),
    "gt": (_gt, "must be greater than"),
    "eq": (_eq, "must equal"),
}

#: Rules that assert the presence or absence of a clause rather than a number.
PRESENCE_OPERATORS = {"present", "absent"}


def evaluate(rule: dict | None, clause_text: str, *, contract=None) -> RuleOutcome:
    """Apply one rule definition to one clause.

    Every branch that cannot produce a defensible answer returns an explicit
    uncertainty state rather than guessing (§54).
    """
    if not rule:
        # Legal has not expressed this standard in machine-checkable form.
        # The AI may still assess it semantically; the engine simply abstains.
        return RuleOutcome(
            ComplianceResult.NOT_APPLICABLE,
            {"reason": "no_machine_checkable_rule"},
        )

    operator = rule.get("operator", "")

    if operator in PRESENCE_OPERATORS:
        return _evaluate_presence(rule, clause_text)

    if operator not in OPERATORS:
        return RuleOutcome(
            ComplianceResult.UNKNOWN,
            {"reason": "unsupported_operator", "operator": operator},
        )

    expected_raw = rule.get("value")
    if expected_raw is None:
        return RuleOutcome(
            ComplianceResult.UNKNOWN, {"reason": "rule_has_no_expected_value"}
        )
    expected = Decimal(str(expected_raw))

    parsed: ParsedValue = extract_for_rule(clause_text, rule)
    if not parsed.is_known:
        # The clause does not state the value. Silence is not compliance.
        return RuleOutcome(
            ComplianceResult.UNKNOWN,
            {
                "reason": "value_not_found_in_clause",
                "rule_key": parsed.key,
                "expected": str(expected),
            },
        )

    # ── currency safety ──────────────────────────────────────────────────
    # Comparing SAR 25M with a JOD figure requires an exchange rate the system
    # does not have. Inventing one would be worse than admitting uncertainty.
    expected_currency = (rule.get("currency") or "").upper()
    if parsed.unit == "currency" and expected_currency:
        if parsed.currency and parsed.currency != expected_currency:
            return RuleOutcome(
                ComplianceResult.AMBIGUOUS,
                {
                    "reason": "currency_mismatch",
                    "expected_currency": expected_currency,
                    "found_currency": parsed.currency,
                    "actual": str(parsed.numeric),
                    "note": "No exchange rate is available; a human must compare these.",
                },
            )
        if not parsed.currency:
            return RuleOutcome(
                ComplianceResult.AMBIGUOUS,
                {
                    "reason": "currency_not_stated",
                    "expected_currency": expected_currency,
                    "actual": str(parsed.numeric),
                },
            )

    compare, phrasing = OPERATORS[operator]
    tolerance = Decimal(str(rule.get("tolerance", 0)))
    actual = parsed.numeric

    # A standard has three positions: preferred, acceptable fallback, and a
    # walk-away. A rule that only encodes the preferred value reports anything
    # in the approved fallback band as a breach, which floods reviewers with
    # conflicts for deviations Legal has already said are tolerable.
    #
    # `fallback_value` closes that gap: inside the band the verdict is PARTIAL —
    # a real deviation that needs the named approver's sign-off, not a breach.
    fallback_raw = rule.get("fallback_value")

    # Tolerance widens the pass band in the permissive direction only.
    if tolerance:
        if operator in {"lte", "lt"}:
            passed = compare(actual - tolerance, expected)
        elif operator in {"gte", "gt"}:
            passed = compare(actual + tolerance, expected)
        else:
            passed = compare(actual, expected)
    else:
        passed = compare(actual, expected)

    detail = {
        "rule_key": parsed.key,
        "operator": operator,
        "expected": str(expected),
        "actual": str(actual),
        "unit": parsed.unit,
        "currency": parsed.currency or expected_currency,
        "tolerance": str(tolerance),
        # Human-readable arithmetic, for the finding and for the audit trail.
        "statement": f"{actual} {phrasing} {expected}"
                     f"{' ' + parsed.unit if parsed.unit != 'currency' else ''}",
        "passed": passed,
    }

    if passed:
        return RuleOutcome(ComplianceResult.COMPLIANT, detail)

    if fallback_raw is not None:
        fallback = Decimal(str(fallback_raw))
        within_fallback = compare(actual, fallback)
        detail["fallback"] = str(fallback)
        detail["within_fallback"] = within_fallback
        if within_fallback:
            detail["statement"] = (
                f"{actual} {phrasing} {expected} (preferred) but is within the "
                f"approved fallback of {fallback} - requires sign-off"
            )
            return RuleOutcome(ComplianceResult.PARTIAL, detail)

    return RuleOutcome(ComplianceResult.NON_COMPLIANT, detail)


def _evaluate_presence(rule: dict, clause_text: str) -> RuleOutcome:
    """Rules of the form "this clause must exist / must not exist"."""
    operator = rule["operator"]
    markers = [m.lower() for m in rule.get("markers", []) if m]
    if not markers:
        return RuleOutcome(
            ComplianceResult.UNKNOWN, {"reason": "presence_rule_has_no_markers"}
        )

    haystack = (clause_text or "").lower()
    found = [m for m in markers if m in haystack]

    if operator == "present":
        passed = bool(found)
    else:                                    # absent
        passed = not found

    return RuleOutcome(
        ComplianceResult.COMPLIANT if passed else ComplianceResult.NON_COMPLIANT,
        {
            "operator": operator,
            "markers_found": found,
            "statement": (
                f"required wording {'found' if found else 'not found'}"
                if operator == "present"
                else f"prohibited wording {'found' if found else 'not found'}"
            ),
            "passed": passed,
        },
    )


# ── reconciliation with the model (§18) ──────────────────────────────────

def reconcile(system_result: str, ai_result: str) -> str:
    """Combine the deterministic verdict with the model's, per §18.

    The one rule that matters: a model disagreeing with arithmetic does not win.
    It raises a conflict for a human.
    """
    from .models import VerificationOutcome

    uncertain = {
        ComplianceResult.UNKNOWN,
        ComplianceResult.AMBIGUOUS,
        ComplianceResult.NO_STANDARD_FOUND,
    }

    if system_result in uncertain or ai_result in uncertain:
        return VerificationOutcome.AMBIGUOUS

    if system_result == ComplianceResult.NOT_APPLICABLE:
        # No deterministic rule exists; the model's reading stands alone and is
        # therefore unverified rather than verified.
        return VerificationOutcome.NEEDS_HUMAN_REVIEW

    if system_result == ai_result:
        return VerificationOutcome.VERIFIED

    return VerificationOutcome.SYSTEM_AI_CONFLICT
