"""Known OCR misreadings, repaired by rule and recorded (§8, §53).

Nothing here guesses. Each rule repairs one misreading measured on real
scanned contracts, only in the context where it was seen, and every repair is
returned as a record -- what was read, what it became, and why -- so the
original reading stays the evidence and the repaired value is shown to a
person for confirmation before anything relies on it.

The rules:

* Arabic percentages: Tesseract's Arabic model reads "%" as "96". "10%"
  (written "%10" right to left) comes back as "9610", "(30%)" as "(30 )96".
  Repaired only beside the words that introduce a percentage (بنسبة، تعادل،
  سقف ...) or before "من" ("... من قيمة العقد").
* Names the engine breaks: "AJi" read as "Adi"/"AJI"/"Aji", "WAJ" as
  "WAd"/"WAv"/"WAN", "KfW" as "KIW", "EURO" as "FURO".
* Digits a second reading saw differently (see pipeline.second_reading).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ARABIC = re.compile(r"[؀-ۿ]")

#: Words that introduce a percentage in Arabic contracts.
_PCT_BEFORE = r"(?:نسبة|بنسبة|تعادل|يعادل|بمعدل|بواقع|سقف|سقفها|حده الأعلى|مقدارها|قدرها)"
_PCT_NUMBER = r"(?:96(?P<a>\d{1,2})|(?P<b>\d{1,2})96|(?P<c>\d{1,3})\s*[()]\s*96)"


@dataclass(frozen=True)
class Correction:
    kind: str           # "arabic_percent" | "known_name" | "second_reading"
    original: str
    corrected: str
    note: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "from": self.original, "to": self.corrected, "note": self.note}


@dataclass
class Corrected:
    text: str
    corrections: list[Correction] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.corrections)


def _percent(match) -> str:
    return (match.group("a") or match.group("b") or match.group("c")) + "%"


def fix_arabic_percent(text: str) -> Corrected:
    """"بنسبة 9610 من" -> "بنسبة 10% من"; only on Arabic lines, only in context."""
    out_lines, found = [], []
    for line in text.split("\n"):
        if not ARABIC.search(line):
            out_lines.append(line)
            continue

        def after_word(m):
            fixed = m.group("lead") + _percent(m)
            found.append(Correction("arabic_percent", m.group(0), fixed,
                                    "'%' read as '96' by the Arabic OCR model"))
            return fixed

        def before_min(m):
            fixed = _percent(m) + m.group("tail")
            found.append(Correction("arabic_percent", m.group(0), fixed,
                                    "'%' read as '96' by the Arabic OCR model"))
            return fixed

        line = re.sub(rf"(?P<lead>{_PCT_BEFORE}\s*[()]?\s*){_PCT_NUMBER}(?!\d)", after_word, line)
        line = re.sub(rf"(?<![\d.,]){_PCT_NUMBER}(?P<tail>\s*\)?\s*من\s)", before_min, line)
        out_lines.append(line)
    return Corrected("\n".join(out_lines), found)


#: (pattern, replacement, note). Word-bounded; case-sensitive on purpose:
#: "Adi" as a person's name is rare in AJi contracts, "ADI" is not touched.
_NAME_RULES = [
    (r"\b(?:Adi|AJI|Aji|AjJi|Ajl|AJl)(?=\s+(?:WE\b|W&E\b|Water\b|Built\b|Group\b))", "AJi", "company name"),
    (r"\bWA[dvNJ1l](?=\s*[)\-–]|-Tender)", "WAJ", "Water Authority of Jordan"),
    (r"\bKIW\b", "KfW", "KfW development bank"),
    (r"\bFURO\b", "EURO", "currency"),
]


def fix_known_names(text: str, known_terms: tuple[str, ...] = ()) -> Corrected:
    found: list[Correction] = []

    def apply(pattern, replacement, note, value):
        def sub(m):
            if m.group(0) != replacement:
                found.append(Correction("known_name", m.group(0), replacement, note))
            return replacement
        return re.sub(pattern, sub, value)

    for pattern, replacement, note in _NAME_RULES:
        if replacement == "WAJ" and "Water Authority" not in text and "WAJ" not in " ".join(known_terms):
            continue
        text = apply(pattern, replacement, note, text)
    return Corrected(text, found)


def correct(text: str, known_terms: tuple[str, ...] = ()) -> Corrected:
    """Every rule, in order; the records of all of them."""
    first = fix_arabic_percent(text)
    second = fix_known_names(first.text, known_terms)
    return Corrected(second.text, first.corrections + second.corrections)


# ── figures checked against each other ───────────────────────────────────

_EN_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_EN_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_EN_NUMBER_WORDS = r"(?:(?:%s)(?:[\s-]+(?:and\s+)?(?:%s))*)" % (
    "|".join(list(_EN_UNITS) + list(_EN_SCALES)), "|".join(list(_EN_UNITS) + list(_EN_SCALES)))


def english_words_to_number(words: str) -> int | None:
    total, current, seen = 0, 0, False
    for token in re.split(r"[\s-]+", words.lower()):
        if token in ("and", ""):
            continue
        if token in _EN_UNITS:
            current += _EN_UNITS[token]
            seen = True
        elif token == "hundred":
            current = max(current, 1) * 100
            seen = True
        elif token in _EN_SCALES:
            total += max(current, 1) * _EN_SCALES[token]
            current = 0
            seen = True
        else:
            return None
    return total + current if seen else None


@dataclass(frozen=True)
class FigureMismatch:
    words: str
    figure: str
    words_value: int
    figure_value: str

    def as_dict(self) -> dict:
        return {"kind": "words_vs_figure", "words": self.words, "figure": self.figure,
                "words_value": self.words_value, "figure_value": self.figure_value}


def words_vs_figures(text: str) -> list[FigureMismatch]:
    """"Fifty (44) months": the number in words and in brackets disagree."""
    out = []
    for m in re.finditer(rf"\b(?P<words>{_EN_NUMBER_WORDS})\s*\(\s*(?P<fig>\d[\d,]*)\s*\)", text, re.I):
        value = english_words_to_number(m.group("words"))
        figure = m.group("fig").replace(",", "")
        if value is not None and figure.isdigit() and int(figure) != value:
            out.append(FigureMismatch(m.group("words"), m.group("fig"), value, figure))
    return out


#: Arabic number words, after removing a leading "و" and the case endings.
_AR_UNITS = {
    "صفر": 0, "واحد": 1, "واحدة": 1, "احد": 1, "أحد": 1, "إحدى": 1, "احدى": 1,
    "اثنان": 2, "اثنين": 2, "إثنان": 2, "إثنين": 2, "اثنتان": 2, "اثنتين": 2,
    "ثلاث": 3, "ثلاثة": 3, "ثلاثه": 3, "اربع": 4, "أربع": 4, "اربعة": 4, "أربعة": 4, "اربعه": 4,
    "خمس": 5, "خمسة": 5, "خمسه": 5, "ست": 6, "ستة": 6, "سته": 6,
    "سبع": 7, "سبعة": 7, "سبعه": 7, "ثمان": 8, "ثماني": 8, "ثمانية": 8, "ثمانيه": 8,
    "تسع": 9, "تسعة": 9, "تسعه": 9, "عشر": 10, "عشرة": 10, "عشره": 10,
    "عشرون": 20, "عشرين": 20, "ثلاثون": 30, "ثلاثين": 30, "اربعون": 40, "أربعون": 40,
    "اربعين": 40, "أربعين": 40, "خمسون": 50, "خمسين": 50, "ستون": 60, "ستين": 60,
    "سبعون": 70, "سبعين": 70, "ثمانون": 80, "ثمانين": 80, "تسعون": 90, "تسعين": 90,
}
_AR_HUNDREDS = {
    "مائة": 100, "مئة": 100, "مائه": 100, "مئه": 100, "مائتان": 200, "مائتين": 200, "مئتان": 200,
    "مئتين": 200, "ثلاثمائة": 300, "ثلاثمئة": 300, "ثلاثمائه": 300, "اربعمائة": 400, "أربعمائة": 400,
    "اربعمائه": 400, "خمسمائة": 500, "خمسمئة": 500, "خمسمائه": 500, "ستمائة": 600, "ستمئة": 600,
    "ستمائه": 600, "سبعمائة": 700, "سبعمئة": 700, "سبعمائه": 700, "ثمانمائة": 800, "ثمانمئة": 800,
    "ثمانمائه": 800, "تسعمائة": 900, "تسعمئة": 900, "تسعمائه": 900,
}
_AR_SCALES = {
    "ألف": 1000, "الف": 1000, "آلاف": 1000, "الاف": 1000, "ألفان": 2000, "ألفين": 2000,
    "الفان": 2000, "الفين": 2000, "مليون": 1_000_000, "ملايين": 1_000_000, "مليونان": 2_000_000,
    "مليونين": 2_000_000,
}


_AR_KNOWN = {**_AR_UNITS, **_AR_HUNDREDS, **_AR_SCALES}
_AR_HUNDRED_WORDS = {"مائة", "مئة", "مائه", "مئه"}


def _ar_token(token: str) -> str:
    """A number word as listed above: no diacritics, no leading "و", no
    accusative ending ("ألفاً" -> "ألف")."""
    token = re.sub(r"[ً-ْـ]", "", token).strip("،,.:؛")

    def base(t: str) -> str:
        if t in _AR_KNOWN:
            return t
        for ending in ("ا", "اً"):
            if t.endswith(ending) and t[: -len(ending)] in _AR_KNOWN:
                return t[: -len(ending)]
        return ""

    return base(token) or (base(token[1:]) if token.startswith("و") else "") or token


def arabic_words_to_number(words: str) -> int | None:
    """"مائة وسبعة وخمسون ألفاً وثلاثمائة واثنان وسبعون" -> 157372."""
    total, current, seen = 0, 0, False
    for raw in words.split():
        token = _ar_token(raw)
        if token in _AR_UNITS:
            current += _AR_UNITS[token]
        elif token in _AR_HUNDREDS:
            hundreds = _AR_HUNDREDS[token]
            # "ثلاث مائة" written apart: the unit multiplies the hundred.
            if token in _AR_HUNDRED_WORDS and 3 <= current <= 9:
                current = current * 100
            else:
                current += hundreds
        elif token in _AR_SCALES:
            scale = _AR_SCALES[token]
            if scale in (2000, 2_000_000):          # the dual: ألفان، مليونان
                total += scale
            else:
                total += max(current, 1) * scale
            current = 0
        else:
            break
        seen = True
    return total + current if seen else None


_AR_NUMBER_WORD = "|".join(sorted(map(re.escape, list(_AR_UNITS) + list(_AR_HUNDREDS) + list(_AR_SCALES)),
                                  key=len, reverse=True))
_AR_PHRASE = re.compile(rf"(?:و?(?:{_AR_NUMBER_WORD})(?:اً|ا|ً)?(?:\s+|$))+")


def arabic_words_vs_figures(text: str) -> list[FigureMismatch]:
    """An amount in figures followed (within a line or two) by the same amount
    in Arabic words, where the two disagree: "مبلغ 0 ثمانية آلاف وسبعمائة
    وثمانون دينار" -- the figure was misread, the words were not."""
    out = []
    flat = re.sub(r"\s+", " ", text)
    for m in re.finditer(r"(?<![\d.,])(\d[\d,]*)(?:\.\d+)?(?![\d,])", flat):
        tail = flat[m.end(): m.end() + 160]
        phrase = _AR_PHRASE.search(tail)
        if not phrase or phrase.start() > 40:
            continue
        # A real amount in words -- at least two number words, at least a
        # hundred -- against a figure that is itself an amount (three digits
        # or more) or the lone "0" OCR leaves when it drops a figure. A bare
        # "ألف" after "38" in a sentence is not an amount being restated.
        if len(phrase.group(0).split()) < 2:
            continue
        value = arabic_words_to_number(phrase.group(0))
        if value is None or value < 100:
            continue
        figure = m.group(1).replace(",", "")
        if not (len(figure) >= 3 or figure == "0"):
            continue
        if figure.isdigit() and int(figure) != value:
            out.append(FigureMismatch(phrase.group(0).strip(), m.group(0), value, figure))
    return out


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check."""
    s = re.sub(r"\s+", "", iban).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    moved = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in moved)
    return int(digits) % 97 == 1


def bad_ibans(text: str) -> list[str]:
    """IBAN-shaped strings that fail the checksum (a digit was misread)."""
    found = []
    for m in re.finditer(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b", text):
        candidate = m.group(0)
        if not iban_is_valid(candidate):
            found.append(candidate)
    return found
