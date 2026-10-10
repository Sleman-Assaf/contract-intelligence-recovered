"""The front end parses, and nothing is silently discarded.

These exist because of a real defect: a stray `}` left in `aji.css` by an
edit made the parser throw away the rule that followed it. There was no
error anywhere -- no console warning, no failing test, no visible breakage
beyond one element losing its margin. It cost a long hunt to find, and the
check that would have caught it takes milliseconds.

Everything asserted here fails quietly in production, which is the only
reason it is worth a test at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.template import TemplateSyntaxError
from django.template.loader import get_template

ROOT = Path(settings.BASE_DIR)
CSS = sorted(p for p in (ROOT / "static").rglob("*.css") if "vendor" not in p.parts)
TEMPLATES = sorted((ROOT / "templates").rglob("*.html"))


def _blank_comments(text: str, start: str, end: str) -> str:
    """Blank comment bodies, keeping newlines so line numbers stay true."""
    pattern = re.escape(start) + r".*?" + re.escape(end)
    return re.sub(
        pattern, lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S
    )


@pytest.mark.parametrize("path", CSS, ids=lambda p: p.name)
def test_stylesheet_braces_balance(path: Path):
    """An unmatched `}` makes the parser discard rules until it recovers.

    The rules simply do not exist afterwards: `getComputedStyle` returns the
    inherited value and nothing reports a problem.
    """
    clean = _blank_comments(path.read_text(encoding="utf-8"), "/*", "*/")

    depth, orphans = 0, []
    for number, line in enumerate(clean.split("\n"), 1):
        for char in line:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    orphans.append(number)
                    depth = 0

    assert not orphans, (
        f"{path.name}: stray '}}' on line(s) {orphans} -- every rule after it, "
        f"up to the parser's recovery point, is silently dropped"
    )
    assert depth == 0, f"{path.name}: {depth} block(s) never closed"


#: Block tags and their closers.
_PAIRS = {
    "if": "endif", "for": "endfor", "block": "endblock", "with": "endwith",
    "comment": "endcomment", "spaceless": "endspaceless",
    "blocktrans": "endblocktrans", "blocktranslate": "endblocktranslate",
    "autoescape": "endautoescape", "filter": "endfilter",
    "ifchanged": "endifchanged", "verbatim": "endverbatim",
}
_CLOSERS = set(_PAIRS.values())


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_blocks_are_matched(path: Path):
    """`{% endif %}` closing a `{% for %}` is a crossed pair.

    Django raises on some of these and quietly accepts others, so this checks
    the pairing rather than relying on the engine to object.
    """
    raw = path.read_text(encoding="utf-8")
    # A {% comment %} may legally hold anything, including stray tags.
    raw = re.sub(
        r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}",
        lambda m: re.sub(r"[^\n]", " ", m.group(0)), raw, flags=re.S,
    )

    stack, problems = [], []
    for number, line in enumerate(raw.split("\n"), 1):
        for tag in re.findall(r"\{%\s*(\w+)", line):
            if tag in _PAIRS:
                stack.append((tag, number))
            elif tag in _CLOSERS:
                if not stack:
                    problems.append(f"line {number}: {{% {tag} %}} with nothing open")
                    continue
                opened, at = stack.pop()
                if _PAIRS[opened] != tag:
                    problems.append(
                        f"line {number}: {{% {tag} %}} closes "
                        f"{{% {opened} %}} from line {at}"
                    )
    problems += [f"line {at}: {{% {tag} %}} never closed" for tag, at in stack]

    assert not problems, f"{path.name}: " + "; ".join(problems)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_compiles(path: Path):
    """Every template parses. A page nobody visited in testing should not be
    the one that discovers a typo in a tag."""
    name = path.relative_to(ROOT / "templates").as_posix()
    try:
        get_template(name)
    except TemplateSyntaxError as exc:
        pytest.fail(f"{name} does not parse: {exc}")
