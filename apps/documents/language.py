"""Which language a piece of contract text is written in.

This matters more than it looks. The analysis prompts are built in the
document's language, and the fallback was `"ar"` — so an English contract
extracted from a native PDF was being sent to the model with Arabic
instructions, because nothing on the native-text path ever set a language.

Detection is by script, counting characters. That is deterministic, needs no
model and no extra dependency, and for the question actually being asked --
Arabic or Latin script -- it is reliable. It is not a general-purpose language
identifier and does not pretend to be: it will not tell Arabic from Persian,
or English from French, and it does not need to.

AJi contracts are frequently bilingual, with an Arabic body and English
technical terms, or two columns. `BILINGUAL` is a real answer here, not a
failure to decide.
"""
from __future__ import annotations

from apps.contracts.models import ContractLanguage

#: Unicode ranges holding Arabic script, including the presentation forms that
#: some PDF producers emit instead of the standard block.
_ARABIC_RANGES = (
    (0x0600, 0x06FF),   # Arabic
    (0x0750, 0x077F),   # Arabic Supplement
    (0x08A0, 0x08FF),   # Arabic Extended-A
    (0xFB50, 0xFDFF),   # Arabic Presentation Forms-A
    (0xFE70, 0xFEFF),   # Arabic Presentation Forms-B
)

#: Below this many letters there is not enough evidence to call it. A page of
#: nothing but figures and a stamp should report "unknown", not guess.
MIN_LETTERS = 12

#: Share of letters in one script needed to call the text that language alone.
#: Well short of 1.0: an Arabic contract routinely carries English party names,
#: standards references and units without ceasing to be an Arabic contract.
DOMINANT_SHARE = 0.85


def _is_arabic(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _ARABIC_RANGES)


def count_scripts(text: str) -> tuple[int, int]:
    """(arabic letters, latin letters) in `text`."""
    arabic = latin = 0
    for char in text or "":
        if not char.isalpha():
            continue
        if _is_arabic(char):
            arabic += 1
        elif char.isascii() or "a" <= char.lower() <= "z":
            latin += 1
    return arabic, latin


def detect_language(text: str) -> str:
    """One of `ar`, `en`, `bi`, or `""` when there is not enough text.

    An empty string is a real answer meaning "not determined", and callers must
    not silently turn it into a default.
    """
    arabic, latin = count_scripts(text)
    total = arabic + latin
    if total < MIN_LETTERS:
        return ""
    if arabic / total >= DOMINANT_SHARE:
        return ContractLanguage.ARABIC
    if latin / total >= DOMINANT_SHARE:
        return ContractLanguage.ENGLISH
    return ContractLanguage.BILINGUAL


def mostly(text: str) -> str:
    """`ar` or `en` by whichever script has more letters ("" when too short).

    For prose that quotes the other language -- an Arabic recommendation
    naming the "Special Conditions" -- which `detect_language` rightly calls
    bilingual, but whose reader is plainly one of the two.
    """
    arabic, latin = count_scripts(text)
    if arabic + latin < MIN_LETTERS:
        return ""
    return ContractLanguage.ARABIC if arabic >= latin else ContractLanguage.ENGLISH


def combine(languages: list[str]) -> str:
    """Roll page languages up to a document language.

    Two pages of Arabic and two of English make a bilingual document, which is
    a common shape for AJi contracts -- not a tie to be broken arbitrarily.
    """
    seen = {lang for lang in languages if lang}
    if not seen:
        return ""
    if seen == {ContractLanguage.ARABIC}:
        return ContractLanguage.ARABIC
    if seen == {ContractLanguage.ENGLISH}:
        return ContractLanguage.ENGLISH
    return ContractLanguage.BILINGUAL


def prompt_language(*candidates: str) -> str:
    """The language to write model instructions in.

    Takes the first candidate that was actually determined. Arabic is the last
    resort because AJi's contracts are predominantly Arabic -- but it is a
    documented fallback, not an accident of a blank field.
    """
    for candidate in candidates:
        if candidate:
            # A bilingual document is prompted in Arabic: the instructions need
            # one language, and the Arabic text is the part that carries the
            # legal obligations in AJi's contracts.
            if candidate == ContractLanguage.BILINGUAL:
                return ContractLanguage.ARABIC
            return candidate
    return ContractLanguage.ARABIC
