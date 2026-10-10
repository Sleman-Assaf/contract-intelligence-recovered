"""Detection of contract-critical values (§9).

A page can score 96% overall while the one token that decides AJi's liability —
"10%" — sits at 72%. These values therefore face a stricter bar than the rest
of the page and, when they fall short, get cropped and sent for vision
verification rather than accepted.

Arabic-Indic digits (٠١٢٣٤٥٦٧٨٩) are handled throughout: Arabic contracts use
them freely, and a detector that only understands ASCII digits would silently
miss every number in the document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# ── digit handling ───────────────────────────────────────────────────────
ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"
EASTERN_ARABIC_INDIC = "۰۱۲۳۴۵۶۷۸۹"   # Persian/Urdu forms also appear
_DIGIT_MAP = {
    **{ch: str(i) for i, ch in enumerate(ARABIC_INDIC)},
    **{ch: str(i) for i, ch in enumerate(EASTERN_ARABIC_INDIC)},
}
_ANY_DIGIT = r"[0-9" + ARABIC_INDIC + EASTERN_ARABIC_INDIC + r"]"


# Arabic typography uses its own symbols for percent, decimal point and
# thousands separator. Comparing "١٠٪" with "10%" must succeed.
_SYMBOL_MAP = {
    "٪": "%",   # ARABIC PERCENT SIGN
    "٫": ".",   # ARABIC DECIMAL SEPARATOR
    "٬": ",",   # ARABIC THOUSANDS SEPARATOR
    "،": ",",   # ARABIC COMMA
}


def canonical_value(text: str) -> str:
    """Normalise digits AND symbols, then strip whitespace.

    Used only for comparing two readings of the same region. The stored source
    text keeps its original glyphs (§53).
    """
    out = normalise_digits(text)
    for src, dst in _SYMBOL_MAP.items():
        out = out.replace(src, dst)
    return "".join(out.split())


def normalise_digits(text: str) -> str:
    """Convert Arabic-Indic digits to ASCII for comparison.

    Used only for *analysis*. The stored source text keeps the original glyphs —
    normalising the evidence would be rewriting it (§53).
    """
    return "".join(_DIGIT_MAP.get(ch, ch) for ch in text)


class CriticalKind(str, Enum):
    PERCENTAGE = "percentage"
    MONEY = "money"
    DURATION = "duration"
    DATE = "date"
    CLAUSE_REFERENCE = "clause_reference"
    #: TDT-AJI-2026-041 · Agreement No. 118902 · رقم العقد ٤٥/٢٠٢٦
    CONTRACT_NUMBER = "contract_number"
    #: An amount that is a guarantee, bond, security or retention. The amount
    #: itself is also money; the kind says what it secures.
    GUARANTEE = "guarantee"
    #: A contracting party or company named in the text.
    PARTY = "party"


@dataclass(frozen=True)
class CriticalMatch:
    kind: CriticalKind
    raw: str
    start: int
    end: int
    normalised: str


_CURRENCY_TOKENS = r"(?:SAR|USD|EUR|JOD|AED|OMR|ر\.س|ريال|ريالاً|دينار|درهم|دولار|يورو)"
_MULTIPLIER_WORDS = r"(?:million|billion|thousand|مليون|مليار|ألف|الف)"

_PATTERNS: list[tuple[CriticalKind, re.Pattern[str]]] = [
    # 10%  ·  ١٠٪  ·  10 في المائة
    (CriticalKind.PERCENTAGE, re.compile(
        rf"{_ANY_DIGIT}{{1,3}}(?:[.,]{_ANY_DIGIT}{{1,2}})?\s*(?:%|٪|في\s*المائة|بالمائة|percent)",
        re.IGNORECASE,
    )),
    # SAR 25,000,000 · 25.000.000 ر.س · ٢٥ مليون ريال · SAR 10 million
    # The multiplier must sit INSIDE the match: "SAR 10 million" read as
    # "SAR 10" is wrong by six orders of magnitude.
    (CriticalKind.MONEY, re.compile(
        rf"(?:{_CURRENCY_TOKENS}\s*{_ANY_DIGIT}[\d{ARABIC_INDIC}.,]*(?:\s*{_MULTIPLIER_WORDS})?"
        rf"|{_ANY_DIGIT}[\d{ARABIC_INDIC}.,]*(?:\s*{_MULTIPLIER_WORDS})?\s*{_CURRENCY_TOKENS})",
        re.IGNORECASE,
    )),
    # 90 days · ٣٠ يوماً · 12 months · خمس سنوات (numeric forms only)
    (CriticalKind.DURATION, re.compile(
        rf"{_ANY_DIGIT}{{1,4}}\s*(?:day|days|month|months|year|years|week|weeks"
        rf"|يوم|يوماً|أيام|شهر|شهراً|أشهر|سنة|سنوات|أسبوع|أسابيع)(?!\w)",
        re.IGNORECASE,
    )),
    # 01/09/2026 · 2026-09-01 · ١٤٤٧/٠٣/٠١
    (CriticalKind.DATE, re.compile(
        rf"{_ANY_DIGIT}{{1,4}}\s*[/\-.]\s*{_ANY_DIGIT}{{1,2}}\s*[/\-.]\s*{_ANY_DIGIT}{{2,4}}"
    )),
    # Clause 12.3 · البند ١٢.٣ · Article 7
    (CriticalKind.CLAUSE_REFERENCE, re.compile(
        rf"(?:clause|article|section|البند|المادة|الفقرة)\s*{_ANY_DIGIT}+(?:\.{_ANY_DIGIT}+)*",
        re.IGNORECASE,
    )),
    # A numbered clause at the start of a line: "4.3 Delay Damages". Dotted
    # numbers only -- a bare "1." opens every list.
    (CriticalKind.CLAUSE_REFERENCE, re.compile(
        rf"^[ \t]*{_ANY_DIGIT}{{1,2}}(?:\.{_ANY_DIGIT}{{1,2}}){{1,3}}(?=[ \t\)\-–:])",
        re.MULTILINE,
    )),
    # "Agreement No. TDT-AJI-2026-041", "Contract No: 118902", "رقم العقد: ٤٥/٢٠٢٦"
    (CriticalKind.CONTRACT_NUMBER, re.compile(
        r"(?:(?:contract|agreement|tender|reference|ref|purchase\s+order|po)\.?\s*"
        r"(?:no\.?|number|#)|رقم\s+(?:العقد|الاتفاقية|المناقصة|العطاء|أمر\s+الشراء))"
        # The code must hold a digit: "contract number was ..." is prose.
        rf"\s*[:#]?\s*(?=[A-Za-z0-9{ARABIC_INDIC}/\-_.]*[0-9{ARABIC_INDIC}])"
        rf"[A-Za-z0-9{ARABIC_INDIC}][A-Za-z0-9{ARABIC_INDIC}/\-_.]{{2,}}",
        re.IGNORECASE,
    )),
    # A reference code on its own: capitals and digits joined by hyphens or
    # slashes, with at least one digit -- TDT-AJI-2026-041, AJI-7429.
    (CriticalKind.CONTRACT_NUMBER, re.compile(
        r"\b(?=[A-Z0-9/\-]*\d)[A-Z]{2,}(?:[-/][A-Z0-9]{2,}){1,5}\b"
    )),
    # All digits, at least three hyphenated groups -- 707-401-2026-041, as
    # a reference often reads after OCR. (A date of the same shape is caught
    # as a date first.)
    (CriticalKind.CONTRACT_NUMBER, re.compile(r"\b\d{2,}(?:-\d{2,}){2,5}\b")),
]

#: Words that make an amount a guarantee value when they come just before it.
_GUARANTEE_WORDS = re.compile(
    r"(?:guarantee|bond|performance\s+security|security\s+deposit|retention|surety"
    r"|ضمان|كفالة|خطاب\s+ضمان|تأمين\s+نهائي|تأمين\s+ابتدائي|محتجزات)",
    re.IGNORECASE,
)

# Legal-entity markers. A name is only taken for a party when one of these
# closes it: "Consultant" or "شركة استشارية" alone is a role, not a name.
_ENTITY_SUFFIX_LATIN = (
    r"(?:L\.?L\.?C\.?|Ltd\.?|Limited|Inc\.?|PLC|Co\.|Company|Corporation|Corp\.?"
    r"|W\.?L\.?L\.?|FZE|FZCO|FZ-LLC|GmbH|S\.A\.|Group|Holding|Consultants|Engineers|Technologies|Contracting|Trading|Partners|Associates"
    r"|Establishment|Est\.)"
)
# Name words: capitalised, no sentence punctuation inside, on one line.
_PARTY_LATIN = re.compile(
    rf"\b((?:[A-Z][A-Za-z0-9&'\-]*[ \t]+){{0,5}}?[A-Z][A-Za-z0-9&'\-]*)[ \t]+{_ENTITY_SUFFIX_LATIN}(?![A-Za-z])"
)
#: Words that open, or make up, a phrase that is a role or a reference and
#: not a name: "The Company", "Each Party Ltd." name nobody.
_NOT_A_NAME = {
    "the", "this", "that", "such", "each", "any", "other", "said", "every", "a", "an",
    "our", "its", "their", "which", "whose", "and", "or", "of", "for", "to", "by",
    "consultant", "client", "employer", "contractor", "subcontractor", "party", "parties",
    "company", "supplier", "vendor", "owner", "engineer", "purchaser", "buyer", "seller",
    # Descriptive words: "Engineering Consultants" is a kind of firm, not a
    # firm. A name needs one word that is not one of these.
    "engineering", "design", "consulting", "consultancy", "technical", "professional",
    "services", "project", "general", "international", "local", "specialist", "specialised",
    "specialized", "management", "construction",
}
_PARTY_ARABIC = re.compile(
    r"(شركة\s+(?:[^\s،,.:؛()]+\s+){0,4}?(?:ذ\.م\.م|ش\.م\.ع|ش\.م\.م|ش\.م\.ك|المحدودة|القابضة))"
)


def _party_key(text: str) -> str:
    """A party name compared without case, spacing or punctuation noise."""
    return re.sub(r"[\W_]+", "", normalise_digits(text), flags=re.UNICODE).casefold()


def find_critical_values(text: str, known_parties=()) -> list[CriticalMatch]:
    """Every contract-critical value in a piece of text.

    `known_parties`: names of the contract's parties from its metadata. They
    are matched first and exactly; the legal-entity heuristics only add names
    that carry an entity marker ("... L.L.C.", "شركة ... ذ.م.م"), so ordinary
    words are not classified as critical.
    """
    if not text:
        return []

    matches: list[CriticalMatch] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            matched_kind = kind
            if kind is CriticalKind.MONEY and _GUARANTEE_WORDS.search(
                    text[max(0, m.start() - 60):m.start()]):
                matched_kind = CriticalKind.GUARANTEE
            matches.append(
                CriticalMatch(
                    kind=matched_kind,
                    raw=m.group(0).strip(),
                    start=m.start(),
                    end=m.end(),
                    normalised=normalise_digits(m.group(0)).strip(),
                )
            )

    # Parties: the ones the contract names, then cautious heuristics.
    folded = text.casefold()
    for name in known_parties or ():
        name = (name or "").strip()
        if len(name) < 3:
            continue
        start = folded.find(name.casefold())
        if start != -1:
            matches.append(CriticalMatch(CriticalKind.PARTY, text[start:start + len(name)],
                                         start, start + len(name), name))
    for m in _PARTY_LATIN.finditer(text):
        words = m.group(1).split()
        # Drop an opening "The"/"Each"/... ; a name must be left, or this is
        # "The Company", not a company.
        while words and words[0].casefold() in _NOT_A_NAME:
            words = words[1:]
        if not words or all(w.casefold() in _NOT_A_NAME for w in words):
            continue
        start = m.start(1) + m.group(1).index(words[0])
        raw = text[start:m.end()].strip()
        matches.append(CriticalMatch(CriticalKind.PARTY, raw, start, m.end(), raw))
    for m in _PARTY_ARABIC.finditer(text):
        matches.append(CriticalMatch(CriticalKind.PARTY, m.group(0).strip(),
                                     m.start(), m.end(), m.group(0).strip()))

    # Overlapping matches: keep the longest at each position — "SAR 25,000,000"
    # beats the bare "25,000,000" inside it.
    matches.sort(key=lambda m: (m.start, -(m.end - m.start)))
    kept: list[CriticalMatch] = []
    for match in matches:
        if kept and match.start < kept[-1].end:
            continue
        kept.append(match)
    return kept


def contains_critical_value(text: str, known_parties=()) -> bool:
    return bool(find_critical_values(text, known_parties))


def critical_signature(text: str, known_parties=()) -> set[str]:
    """The critical values of a reading, each normalised for comparison."""
    out = set()
    for m in find_critical_values(text, known_parties):
        if m.kind is CriticalKind.PARTY:
            out.add("party:" + _party_key(m.raw))
        elif m.kind is CriticalKind.CONTRACT_NUMBER:
            # The code itself: "Agreement No. X-1" and "Agreement No X-1" agree.
            code = re.split(r"[\s:#]+", m.raw.strip())[-1]
            out.add("ref:" + canonical_value(code).upper())
        else:
            out.add(canonical_value(m.raw))
    return out


def values_agree(ocr_text: str, vision_text: str, known_parties=()) -> bool:
    """Do two readings carry the same critical values?

    Compares the normalised values rather than whole strings: differences in
    whitespace or letter-level OCR noise must not mask agreement on the
    numbers, and agreement on prose must not hide a disagreement on "10%"
    versus "15%".
    """
    return critical_signature(ocr_text, known_parties) == critical_signature(vision_text, known_parties)
