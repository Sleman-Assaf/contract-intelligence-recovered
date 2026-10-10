"""Counted phrases, in both languages.

Arabic counts in six shapes where English has two: nothing, one, a pair, a few
(3-10), many (11-99) and round hundreds. The pair is its own word -- "عقدان",
not "2 عقود" -- and from eleven upwards the noun goes back to the singular.

Everything here was found rendering the real pages. A two-form catalogue made
every Arabic count read "2 عقود", and seven English entries carried "{{ n }}"
where gettext substitutes "%(n)s", so the English UI printed the characters
"{{ n }}" instead of the number. Both are invisible to Django: an unmatched
msgid falls back to the source text and a stray placeholder is just text.

The counted phrases are discovered by scanning the templates rather than
listed, so a phrase added next month is covered the day it is written.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.template import Context, Template
from django.utils import translation

ROOT = Path(settings.BASE_DIR)

#: `{% blocktrans count n=… %}one{% plural %}many{% endblocktrans %}`, over the
#: whole file because the block is often wrapped across lines.
_COUNTED = re.compile(
    r"\{%\s*blocktrans(?:late)?\s+count\s+(\w+)=.*?%\}"
    r"(.*?)\{%\s*plural\s*%\}(.*?)\{%\s*endblocktrans(?:late)?\s*%\}",
    re.S,
)

_ARABIC = re.compile(r"[ء-ي]")


def _sites():
    """Every counted phrase in the templates, as (where, var, one, many)."""
    found = []
    for path in sorted((ROOT / "templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in _COUNTED.finditer(text):
            var, one, many = match.groups()
            where = "%s:%s" % (
                path.relative_to(ROOT).as_posix(),
                text[: match.start()].count("\n") + 1,
            )
            found.append((where, var, one.strip(), many.strip()))
    return found


SITES = _sites()


def _render(var, one, many, n, language):
    """The phrase as a reader sees it, through the real template path.

    Rendered rather than looked up: `ngettext` picks the form, but it is
    `blocktrans` that substitutes the number, and the bug that printed
    "{{ n }}" lived in the substitution rather than the choice.
    """
    source = (
        "{%% load i18n %%}{%% blocktrans count %(v)s=%(v)s %%}%(one)s"
        "{%% plural %%}%(many)s{%% endblocktrans %%}"
        % {"v": var, "one": one, "many": many}
    )
    with translation.override(language):
        return Template(source).render(Context({var: n})).strip()


def test_the_templates_still_have_counted_phrases():
    """A scan that silently finds nothing would pass every test below.

    A floor, not a tally. Counted phrases come and go with the features that
    carry them -- the assistant's "من N محادثات سابقة لك" went when that panel
    was taken out -- and a number tracking the exact count fails on every
    honest removal without ever catching a broken scan.
    """
    assert len(SITES) >= 8, f"only found {len(SITES)} counted phrases"


@pytest.mark.parametrize("where,var,one,many", SITES, ids=lambda v: str(v)[:40])
def test_arabic_has_a_separate_word_for_two(where, var, one, many):
    """Two is not "a few". If the dual is missing from the catalogue, gettext
    falls back to the English rule and n=2 renders as the 3-10 form."""
    two = _render(var, one, many, 2, "ar")
    few = _render(var, one, many, 3, "ar")
    assert two != few, (
        f"{where}: n=2 and n=3 render identically ({two!r}) -- the Arabic dual "
        f"is missing from locale/ar, so gettext is using the English rule"
    )
    assert "2" not in two, (
        f"{where}: the dual should name the pair in words, not print the "
        f"digit 2 ({two!r})"
    )


@pytest.mark.parametrize("where,var,one,many", SITES, ids=lambda v: str(v)[:40])
def test_eleven_and_up_returns_to_the_singular(where, var, one, many):
    """Arabic takes the noun back to the singular after ten. Catching this
    needs its own case: a catalogue can carry the dual and still stop there."""
    few = _render(var, one, many, 3, "ar")
    many_form = _render(var, one, many, 11, "ar")
    assert few != many_form, (
        f"{where}: n=3 and n=11 render the same shape ({few!r}) -- locale/ar "
        f"is missing the 11-and-up form"
    )


@pytest.mark.parametrize("where,var,one,many", SITES, ids=lambda v: str(v)[:40])
@pytest.mark.parametrize("language", ["ar", "en"])
@pytest.mark.parametrize("n", [1, 2, 3, 11, 100])
def test_the_number_is_substituted(where, var, one, many, language, n):
    """A template placeholder written into a .po is never substituted; it is
    printed. "{{ n }} contracts excluded" is what the English page showed."""
    out = _render(var, one, many, n, language)
    assert "{{" not in out and "}}" not in out, (
        f"{where} [{language}, n={n}]: {out!r} contains a template placeholder "
        f"-- a .po msgstr must use %(n)s, which gettext substitutes"
    )
    assert "%(" not in out, (
        f"{where} [{language}, n={n}]: {out!r} has an unsubstituted placeholder"
    )


@pytest.mark.parametrize("where,var,one,many", SITES, ids=lambda v: str(v)[:40])
@pytest.mark.parametrize("n", [1, 2, 3, 11])
def test_english_is_english(where, var, one, many, n):
    """The source strings are Arabic, so an English reader who hits a missing
    entry gets Arabic -- no error, no warning, just the wrong language."""
    out = _render(var, one, many, n, "en")
    assert not _ARABIC.search(out), (
        f"{where} [n={n}]: {out!r} is still Arabic on the English page -- "
        f"locale/en has no entry for this phrase"
    )


class TestTheCatalogueItself:
    def test_arabic_declares_six_forms(self):
        """Without the rule in the .mo header, the six translations are there
        and gettext still picks between the first two."""
        import gettext

        path = ROOT / "locale" / "ar" / "LC_MESSAGES" / "django.mo"
        with path.open("rb") as handle:
            catalogue = gettext.GNUTranslations(handle)
        picked = [catalogue.plural(n) for n in (0, 1, 2, 3, 11, 100)]
        assert picked == [0, 1, 2, 3, 4, 5], (
            f"locale/ar plural rule selects {picked} -- expected one distinct "
            f"form per Arabic number shape"
        )

    def test_both_catalogues_declare_their_charset(self):
        """A header gettext cannot read leaves the charset at ASCII, and the
        catalogue fails to load at all -- which takes Django down with it."""
        import gettext

        for language in ("ar", "en"):
            path = ROOT / "locale" / language / "LC_MESSAGES" / "django.mo"
            with path.open("rb") as handle:
                gettext.GNUTranslations(handle)   # raises if the header is bad
