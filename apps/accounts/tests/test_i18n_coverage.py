"""Nothing Arabic reaches the English UI.

The source strings of this project are Arabic and the catalogue translates
them into English, which makes the failure silent and one-directional: a
string added without an English entry renders in Arabic on the English page,
with no error, no warning and nothing in the log. Arabic readers -- who are
most of the people building this -- never see it.

That is what happened. An audit of the whole codebase found 28 strings with no
English at all, among them every word of the assistant's memory panel, the
"delete account" and "deactivate" buttons, the pager that says which rows are
showing, and six messages a reader is shown after acting: what happens when
you try to delete the last administrator, or an account that has uploaded
contracts. All of them rendered Arabic on an English page.

This finds the next one on the commit that introduces it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import translation

ROOT = Path(settings.BASE_DIR)
#: Letters and the Arabic punctuation marks. A msgid that is only "؟" has no
#: letter in it, and one slipped past this scan for exactly that reason.
ARABIC = re.compile(r"[ء-ي؟،؛]")

#: Calls whose first argument is a msgid.
_MARKERS = {"_", "gettext", "gettext_lazy", "ugettext", "ugettext_lazy"}

#: `{% trans "x" %}` and `{% translate 'x' %}`.
_TAG = re.compile(r"""\{%\s*trans(?:late)?\s+(["'])(.+?)\1""", re.S)
#: `_("x")` in a template expression.
_CALL = re.compile(r"""\b_\(\s*(["'])(.+?)\1\s*\)""", re.S)
#: A blocktrans body. Plural ones are covered by test_i18n_plurals instead,
#: which has to go through ngettext to see them.
_BLOCK = re.compile(
    r"\{%\s*blocktrans(?:late)?[^%]*%\}(.*?)\{%\s*endblocktrans(?:late)?\s*%\}",
    re.S,
)
_PLURAL = re.compile(r"\{%\s*plural\s*%\}")


def _python_msgids():
    """Every marked string in the application code.

    Parsed rather than matched: Python joins adjacent string literals, so a
    message wrapped across two lines is one msgid, and a regex reading the
    source sees two fragments and a quote in the middle.
    """
    out = []
    for path in sorted((ROOT / "apps").rglob("*.py")):
        where = path.as_posix()
        if "/tests/" in where or "/migrations/" in where:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in _MARKERS:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if ARABIC.search(first.value):
                    out.append((
                        "%s:%d" % (path.relative_to(ROOT).as_posix(), node.lineno),
                        first.value,
                    ))
    return out


def _template_msgids():
    out = []
    for path in sorted((ROOT / "templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(ROOT).as_posix()

        def at(offset):
            return "%s:%d" % (name, text[:offset].count("\n") + 1)

        for pattern in (_TAG, _CALL):
            for match in pattern.finditer(text):
                msgid = match.group(2).strip()
                if ARABIC.search(msgid):
                    out.append((at(match.start()), msgid))
        for match in _BLOCK.finditer(text):
            body = match.group(1)
            if _PLURAL.search(body):
                continue
            # blocktrans renders a variable as %(name)s in the msgid.
            msgid = re.sub(r"\{\{\s*(\w+)[^}]*\}\}", r"%(\1)s", body).strip()
            if ARABIC.search(msgid):
                out.append((at(match.start()), msgid))
    return out


PYTHON = _python_msgids()
TEMPLATES = _template_msgids()


def _labels(cases):
    """One readable id per case: a failure names the file and line itself."""
    return ["%s %s" % (where, msgid[:28]) for where, msgid in cases]


PYTHON_IDS = _labels(PYTHON)
TEMPLATE_IDS = _labels(TEMPLATES)


def test_the_scan_finds_the_strings_it_is_meant_to_check():
    """A scanner that quietly matches nothing passes every test below.

    A floor rather than a tally: strings come and go as features do, and a
    number tracking the exact count would fail on every honest removal
    without ever catching a broken scan.
    """
    assert len(PYTHON) > 50, f"only {len(PYTHON)} marked strings in apps/"
    assert len(TEMPLATES) > 250, f"only {len(TEMPLATES)} marked strings in templates/"


@pytest.mark.parametrize("where,msgid", PYTHON, ids=PYTHON_IDS)
def test_every_message_has_english(where, msgid):
    with translation.override("en"):
        english = translation.gettext(msgid)
    assert english != msgid, (
        f"{where}: no English for {msgid[:60]!r} -- it renders in Arabic on "
        f"the English UI. Add it to locale/en/LC_MESSAGES/django.po and run "
        f"manage.py compilemessages_py."
    )
    assert not ARABIC.search(english), (
        f"{where}: the English entry for {msgid[:40]!r} is still Arabic"
    )


@pytest.mark.parametrize("where,msgid", TEMPLATES, ids=TEMPLATE_IDS)
def test_every_template_string_has_english(where, msgid):
    with translation.override("en"):
        english = translation.gettext(msgid)
    assert english != msgid, (
        f"{where}: no English for {msgid[:60]!r} -- it renders in Arabic on "
        f"the English UI. Add it to locale/en/LC_MESSAGES/django.po and run "
        f"manage.py compilemessages_py."
    )
    assert not ARABIC.search(english), (
        f"{where}: the English entry for {msgid[:40]!r} is still Arabic"
    )


def test_arabic_is_never_written_straight_into_a_template():
    """A string outside {% trans %} cannot be translated at all, so no
    catalogue entry will ever fix it."""
    marked = (_TAG, _CALL, _BLOCK,
              re.compile(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.S),
              re.compile(r"<!--.*?-->", re.S))
    # The language toggle names the language it switches *to*, so the Arabic
    # one is a label rather than a message.
    allowed = {"ع"}

    problems = []
    for path in sorted((ROOT / "templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for pattern in marked:
            text = pattern.sub(
                lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
        for number, line in enumerate(text.split("\n"), 1):
            if not ARABIC.search(line):
                continue
            if all(word in allowed
                   for word in re.findall(r"[ء-ي]+", line)):
                continue
            problems.append(
                "%s:%d %s" % (path.relative_to(ROOT).as_posix(),
                              number, line.strip()[:70])
            )
    assert not problems, (
        "Arabic written straight into a template, outside {% trans %}:\n  "
        + "\n  ".join(problems)
    )
