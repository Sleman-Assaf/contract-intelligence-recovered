"""Text a PDF's text layer hands back, made into plain letters.

Typesetters join letter pairs into one ligature glyph, and some PDFs map that
glyph to a single character instead of the letters it draws: Unicode's own
ligatures ("ﬁxed"), and the private-use code points Office fonts use for
theirs ("a\\ue210empt" is "attempt"). Kept as they are, the word is broken
everywhere it travels -- the clause on screen, the chat's search over the
file, the redline export (no font has a glyph for a private-use point, so it
prints as a box).
"""
from __future__ import annotations

#: Ligature characters -> the letters they join.
LIGATURES = {
    # Unicode Alphabetic Presentation Forms
    0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi", 0xFB04: "ffl",
    0xFB05: "st", 0xFB06: "st",
    # Office fonts' private-use ligatures, as found in uploaded contracts
    # (confirmed from context: "aer" -> after, "aempt" -> attempt).
    0xE208: "ft",
    0xE210: "tt",
}


def plain_letters(text: str) -> str:
    """`text` with every known ligature character written out as its letters."""
    return (text or "").translate(LIGATURES)


def has_ligatures(text: str) -> bool:
    return any(ord(ch) in LIGATURES for ch in text or "")
