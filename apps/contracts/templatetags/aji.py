"""Small display helpers shared across templates."""
import os
from decimal import Decimal, InvalidOperation

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

from apps.accounts.models import Role
from apps.common.models import DEPARTMENT_LABELS

register = template.Library()


@register.filter
def department(value: str) -> str:
    """The owning department in the reader's language.

    Stored in English because that is how Legal wrote the standards; shown in
    Arabic because that is the language the page is in.
    """
    return DEPARTMENT_LABELS.get(value, value or "—")

@register.filter
def role_label(value: str) -> str:
    """The role in the reader's language.

    The audit trail stores the code it was written with. A reader of the
    report should not have to know that `legal_reviewer` means المراجع
    القانوني, and an unknown code is shown as-is rather than hidden.
    """
    try:
        return Role(value).label
    except ValueError:
        return value or ""


@register.filter
def amount(value, places=0) -> str:
    """A money figure with its thousands grouped: 4850000 -> 4,850,000.

    `floatformat` printed the bare digits, which nobody reads at a glance in
    a contract worth millions. Grouped with commas in both languages, as the
    contracts themselves write it.
    """
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    return f"{number:,.{int(places)}f}"


@register.simple_tag
def static_v(path: str) -> str:
    """`{% static %}` with the file's modification time appended.

    Django's dev server sends `Last-Modified` but no `Cache-Control`, so a
    browser is free to reuse a stylesheet it fetched minutes ago without
    revalidating. That turns an edit to the CSS or JS into a change nobody can
    see, which is indistinguishable from a bug. Keying the URL to the file's
    mtime means a changed file is always a new URL.

    Falls back to the plain URL if the file cannot be found on disk, e.g. when
    assets are served from somewhere other than the local filesystem.
    """
    url = static(path)
    try:
        found = finders.find(path)
        if found:
            return f"{url}?v={int(os.path.getmtime(found))}"
    except OSError:
        pass
    return url



# ── chat answers ─────────────────────────────────────────────────────────────
import re as _re

from django.utils.html import escape as _escape
from django.utils.safestring import mark_safe as _mark_safe

_MD_BOLD = _re.compile(r"\*\*(.+?)\*\*")
_MD_BULLET = _re.compile(r"^\s*[-*•]\s+(.*)$")
_MD_NUMBERED = _re.compile(r"^\s*(\d{1,3})[.)]\s+(.*)$")
_MD_HEADING = _re.compile(r"^\s*#{1,4}\s+(.*)$")


_MD_STRONG_AR = _re.compile(r"[֐-ࣿיִ-﷿ﹰ-﻿]")
_MD_STRONG_LATIN = _re.compile(r"[A-Za-zÀ-ɏ]")


def _md_dir(text: str) -> str:
    """The direction of a block, from its first strong letter.

    Decided here rather than with `dir="auto"` because auto skips any child
    that carries its own `dir`: a list whose items each said `dir="auto"`
    found no letters of its own, fell back to left-to-right, and put the
    numbers of an Arabic list on the wrong side.
    """
    ar, la = _MD_STRONG_AR.search(text or ""), _MD_STRONG_LATIN.search(text or "")
    if ar and (not la or ar.start() < la.start()):
        return "rtl"
    if la:
        return "ltr"
    return "auto"


def _md_inline(text: str) -> str:
    # Escaped first, so nothing the model writes can become markup; only the
    # bold marker is turned back into a tag afterwards.
    return _MD_BOLD.sub(r"<strong>\1</strong>", _escape(text))


@register.filter
def text_dir(text) -> str:
    """"rtl" for text whose first strong letter is Arabic, else "".

    Put on a whole chat turn, so an Arabic question or answer is laid out
    right to left -- its card, mark and labels -- whichever interface it is
    read in. Anything else returns "" and keeps the page's own direction.
    """
    return "rtl" if _md_dir(str(text or "")) == "rtl" else ""


@register.filter
def chat_markdown(text) -> str:
    """An assistant answer as paragraphs and lists, each in its own direction.

    Answers arrived as one run of text with the model's markdown left in it,
    and inherited the page's direction. An Arabic answer on the English page
    -- or an English one on the Arabic page, now that answers follow the
    language of the question -- was laid out backwards: list numbers and
    punctuation at the wrong end, and "(Red Line)" inside an Arabic sentence
    reordered around it until the sentence no longer read.

    Every paragraph and list carries its own `dir`, taken from its first
    letter, whatever the page is set to (see `_md_dir`). The
    markdown the model actually uses -- bold, numbered and bulleted lists,
    headings -- becomes real markup; anything else stays as text. The input
    is escaped before any tag is added.
    """
    if not text:
        return ""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    kind = {"tag": None, "start": 1}

    def flush_para():
        if para:
            text = " ".join(para)
            out.append(f'<p dir="{_md_dir(text)}">' + "<br>".join(_md_inline(x) for x in para) + "</p>")
            para.clear()

    def flush_list():
        if items:
            tag = kind["tag"]
            start = f' start="{kind["start"]}"' if tag == "ol" and kind["start"] != 1 else ""
            list_dir = _md_dir(items[0])
            body = "".join(
                # An item in the other script says so; the rest follow the list.
                f'<li dir="{d}">{_md_inline(x)}</li>' if (d := _md_dir(x)) not in (list_dir, "auto")
                else f"<li>{_md_inline(x)}</li>"
                for x in items)
            out.append(f'<{tag} dir="{list_dir}"{start}>{body}</{tag}>')
            items.clear()
        kind["tag"] = None

    for line in str(text).replace("\r\n", "\n").split("\n"):
        if not line.strip():
            flush_para()
            flush_list()
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            flush_para(); flush_list()
            out.append(f'<p class="md-h" dir="{_md_dir(heading.group(1))}"><strong>{_md_inline(heading.group(1))}</strong></p>')
            continue
        numbered, bullet = _MD_NUMBERED.match(line), _MD_BULLET.match(line)
        if numbered or bullet:
            flush_para()
            this = "ol" if numbered else "ul"
            if kind["tag"] != this:
                flush_list()
                kind["tag"] = this
                kind["start"] = int(numbered.group(1)) if numbered else 1
            items.append(numbered.group(2) if numbered else bullet.group(1))
            continue
        if items and line[:1] in (" ", "\t"):
            items[-1] += " " + line.strip()          # an item wrapped onto the next line
            continue
        flush_list()
        para.append(line.strip())
    flush_para()
    flush_list()
    return _mark_safe("".join(out))
