"""Ligatures from a PDF's text layer are written out as letters.

An uploaded contract's PDF mapped Office's "tt" and "ft" ligature glyphs to
private-use characters: "attempt" was stored as "a\\ue210empt" and broke on
screen, in the chat's search and in the Word export."""
from apps.documents.text import has_ligatures, plain_letters


def test_office_private_use_ligatures():
    assert plain_letters("The Parties shall aempt in good faith aer notice") == \
        "The Parties shall attempt in good faith after notice"


def test_unicode_ligatures():
    assert plain_letters("ﬁxed ﬂoor eﬀect") == "fixed floor effect"


def test_other_text_is_untouched():
    text = "18,000 m² · ±10% · سقف المسؤولية"
    assert plain_letters(text) == text
    assert not has_ligatures(text)
    assert has_ligatures("aempt")
