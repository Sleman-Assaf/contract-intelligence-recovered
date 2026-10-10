"""Deterministic extraction of structured values from clause text (§17).

Turning "لا تتجاوز ١٠٪ من قيمة العقد" into `Decimal("10")` is parsing, not
interpretation, so it is done in Python. Asking a model to read a number back
out of a sentence introduces a failure mode for no benefit.

Decimal throughout — money and percentages must never touch binary floats.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from apps.documents.critical_values import (
    CriticalKind,
    canonical_value,
    find_critical_values,
    normalise_digits,
)

# Duration units normalised to days so a "90 days" standard can be compared
# with a contract that says "3 months".
_DURATION_DAYS = {
    "day": 1, "days": 1, "يوم": 1, "يوما": 1, "يوماً": 1, "أيام": 1, "ايام": 1,
    "week": 7, "weeks": 7, "أسبوع": 7, "اسبوع": 7, "أسابيع": 7, "اسابيع": 7,
    "month": 30, "months": 30, "شهر": 30, "شهرا": 30, "شهراً": 30, "أشهر": 30, "اشهر": 30,
    "year": 365, "years": 365, "سنة": 365, "سنوات": 365, "عام": 365, "أعوام": 365,
}

_MULTIPLIERS = {
    "thousand": Decimal(1_000), "ألف": Decimal(1_000), "الف": Decimal(1_000),
    "million": Decimal(1_000_000), "مليون": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000), "مليار": Decimal(1_000_000_000),
}

_CURRENCY_CODES = {
    "SAR": "SAR", "ر.س": "SAR", "ريال": "SAR", "ريالا": "SAR", "ريالاً": "SAR",
    "USD": "USD", "دولار": "USD",
    "EUR": "EUR", "يورو": "EUR",
    "JOD": "JOD", "دينار": "JOD",
    "AED": "AED", "درهم": "AED",
    "OMR": "OMR",
}

_NUMBER = re.compile(r"\d[\d,\.\s]*")


@dataclass(frozen=True)
class ParsedValue:
    key: str
    numeric: Decimal | None
    unit: str = ""
    currency: str = ""
    raw: str = ""

    @property
    def is_known(self) -> bool:
        return self.numeric is not None


def _to_decimal(text: str) -> Decimal | None:
    """Parse a number that may use Arabic digits and mixed separators."""
    cleaned = canonical_value(text)
    match = _NUMBER.search(cleaned)
    if not match:
        return None
    token = match.group(0).replace(" ", "")

    # Disambiguate separators: "25,000,000" is thousands; "10,5" is decimal.
    if "," in token and "." in token:
        token = token.replace(",", "")          # comma = thousands
    elif token.count(",") == 1 and len(token.split(",")[-1]) in (1, 2):
        token = token.replace(",", ".")         # comma = decimal
    else:
        token = token.replace(",", "")

    if token.count(".") > 1:                     # "25.000.000" → thousands
        token = token.replace(".", "")

    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def parse_percentage(text: str) -> Decimal | None:
    for match in find_critical_values(text):
        if match.kind is CriticalKind.PERCENTAGE:
            return _to_decimal(match.raw)
    return None


def parse_money(text: str) -> tuple[Decimal | None, str]:
    """Return (amount, currency_code). Multipliers like 'million' are applied."""
    for match in find_critical_values(text):
        if match.kind is not CriticalKind.MONEY:
            continue
        amount = _to_decimal(match.raw)
        if amount is None:
            continue

        normalised = normalise_digits(match.raw)
        lowered = normalised.lower()
        for word, factor in _MULTIPLIERS.items():
            if word in lowered:
                amount *= factor
                break

        currency = ""
        for token, code in _CURRENCY_CODES.items():
            if token.lower() in lowered:
                currency = code
                break
        return amount, currency
    return None, ""


_BARE_NUMBER = re.compile(r"\d[\d,\.\s]{3,}")


def _bare_number(text: str) -> Decimal | None:
    """A sizeable unlabelled figure, used only as a money fallback."""
    match = _BARE_NUMBER.search(canonical_value(text))
    return _to_decimal(match.group(0)) if match else None


def parse_duration_days(text: str) -> Decimal | None:
    """Normalise any duration to days."""
    for match in find_critical_values(text):
        if match.kind is not CriticalKind.DURATION:
            continue
        amount = _to_decimal(match.raw)
        if amount is None:
            continue
        lowered = normalise_digits(match.raw).lower()
        for unit, days in _DURATION_DAYS.items():
            if unit in lowered:
                return amount * Decimal(days)
        return amount          # bare number: assume days
    return None


#: What a rule can ask to be extracted.
EXTRACTORS = {
    "percentage": "percent",
    "money": "currency",
    "duration_days": "days",
}


def extract_for_rule(clause_text: str, rule: dict) -> ParsedValue:
    """Extract the single value a rule needs from a clause.

    Returns a ParsedValue whose `numeric` is None when the clause simply does
    not state the value. That is UNKNOWN, not zero, and not non-compliance.
    """
    key = rule.get("key", "value")
    kind = rule.get("extract", "")

    if kind == "percentage":
        return ParsedValue(key=key, numeric=parse_percentage(clause_text),
                           unit="percent", raw=clause_text[:200])
    if kind == "money":
        amount, currency = parse_money(clause_text)
        if amount is None:
            # No currency-tagged amount. A bare figure may still be the value —
            # report it with an empty currency so the engine returns AMBIGUOUS
            # rather than UNKNOWN. "30,000,000" under a SAR rule is a number we
            # found but cannot safely compare, which is a different problem
            # from finding nothing at all.
            amount = _bare_number(clause_text)
        return ParsedValue(key=key, numeric=amount, unit="currency",
                           currency=currency, raw=clause_text[:200])
    if kind == "duration_days":
        return ParsedValue(key=key, numeric=parse_duration_days(clause_text),
                           unit="days", raw=clause_text[:200])

    return ParsedValue(key=key, numeric=None, raw=clause_text[:200])
