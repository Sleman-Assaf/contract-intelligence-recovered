"""Redline export: a .docx of proposed clause changes as real tracked changes.

Word's revision marks are `w:ins` / `w:del` elements. python-docx has no API for
them, so the XML is built directly — which matters, because the point of this
export is that counsel can open it and click Accept or Reject per change. A
document that merely *looks* struck through is a picture of a redline, not one.

What this will and will not propose
-----------------------------------
Replacement wording comes from, in order:

1. the reviewer's own edit recorded on a decision,
2. Legal's pre-approved fallback wording on the standard, or
3. where the standard has none, the wording Sanad suggested for the clause
   (`Finding.fallback_wording` falls back to it).

The third is never passed off as the first two. This document goes to a
counterparty, and a reviewer skimming tracked changes cannot tell generated
language from approved language once it is in the file -- so each suggestion
says so on its own source line, and the file opens with a warning that some of
its wording is not Legal-approved. A suggestion that is a note rather than
wording ("N/A - Clause text is missing.") is not proposed at all. The model's
prose about a clause (`deviation`, `recommended_action`) is never inserted.

Findings with no approved replacement still appear, as a flagged comment with
no insertion, so nothing is silently dropped.

How it looks
------------
Dressed like the PDF report, in the AJi theme: the black Sanad strip at the
top, Noto Kufi Arabic (the site's text face) for Latin and Arabic, black, white
and yellow, numbered clause headings over a black rule, and a running footer
naming the contract on every page. The document is laid out in the reader's
language; each paragraph then takes the direction of its own text, so an
English clause inside the Arabic document still reads left to right.
"""
from __future__ import annotations

import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from django.utils.translation import get_language
from django.utils.translation import gettext as _

#: Body size. A document counsel reads and marks up on paper: a point above
#: the PDF's.
BODY_PT = 11

AUTHOR = "Sanad"

#: Word namespace, needed for every element built by hand.
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# The brand's three colours, and the PDF's two supporting neutrals: its grey
# for secondary text and the faint fills behind a quote and a notice.
BLACK, YELLOW, WHITE = "000000", "FCD700", "FFFFFF"
MUTED = "626262"
HAIRLINE = "E5E5E5"
QUOTE_FILL = "F8F9FA"
NOTICE_FILL = "FEF8D1"          # the PDF's rgba(252, 215, 0, .18) on white, exactly
BADGE_FILL = {"high": BLACK, "medium": YELLOW}
BADGE_TEXT = {"high": WHITE}

#: The site's text face, for Latin and Arabic alike (restored 2026-09-30).
LATIN_FONT = "Noto Kufi Arabic"
ARABIC_FONT = "Noto Kufi Arabic"

#: Fonts carried inside the file, as (family, slot, file in static/fonts), so
#: the redline is in Noto Kufi Arabic on a machine that does not have it.
#: Google Fonts' static TrueType files -- the same face the site loads, with
#: its Latin letters (the Noto project's own build is Arabic-only, and Word
#: set every Latin word in Calibri). SIL OFL (static/fonts/NotoKufiArabic-
#: OFL.txt); licence flags installable (fsType 0), so Word opens it editable.
EMBEDDED_FONTS: tuple = (
    (LATIN_FONT, "embedRegular", "noto-kufi-arabic-400.ttf"),
    (LATIN_FONT, "embedBold", "noto-kufi-arabic-700.ttf"),
)


@dataclass
class Change:
    """One proposed change, with where its wording came from."""

    clause_number: str
    category: str
    original: str
    replacement: str
    rationale: str
    source: str
    risk_level: str
    #: The wording is Sanad's suggestion, not Legal's approved text.
    is_suggested: bool = False
    #: Assessed against a demo (test-data) standard, whatever its wording.
    on_demo_standard: bool = False

    @property
    def is_actionable(self) -> bool:
        """False when there is approved wording missing, so the entry is a
        flag for a human rather than something to accept."""
        return bool(self.replacement)


# ── element order ────────────────────────────────────────────────────────
# Word rejects a document whose property elements are out of schema order, so
# every hand-built property is inserted before whichever of its successors is
# already present rather than appended.
_PPR_ORDER = (
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr",
    "w:widowControl", "w:numPr", "w:suppressLineNumbers", "w:pBdr", "w:shd",
    "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
    "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
    "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
    "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
    "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl",
    "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
)
_RPR_ORDER = (
    "w:ins", "w:del", "w:moveFrom", "w:moveTo", "w:rStyle", "w:rFonts", "w:b",
    "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps", "w:strike", "w:dstrike",
    "w:outline", "w:shadow", "w:emboss", "w:imprint", "w:noProof",
    "w:snapToGrid", "w:vanish", "w:webHidden", "w:color", "w:spacing", "w:w",
    "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u", "w:effect",
    "w:bdr", "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em",
    "w:lang", "w:eastAsianLayout", "w:specVanish", "w:oMath",
)
_TBLPR_ORDER = (
    "w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual",
    "w:tblStyleRowBandSize", "w:tblStyleColBandSize", "w:tblW", "w:jc",
    "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout",
    "w:tblCellMar", "w:tblLook",
)
_TCPR_ORDER = (
    "w:cnfStyle", "w:tcW", "w:gridSpan", "w:hMerge", "w:vMerge", "w:tcBorders",
    "w:shd", "w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText",
    "w:vAlign", "w:hideMark",
)
_BORDER_ORDER = ("w:top", "w:left", "w:bottom", "w:right", "w:insideH", "w:insideV", "w:between", "w:bar")


def _child(parent, tag: str, order: tuple, **attrs):
    """`parent`'s `tag` child, created in its schema position if missing,
    with `attrs` (unprefixed Word attribute names) set on it."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    element = parent.find(qn(tag))
    if element is None:
        element = OxmlElement(tag)
        successors = {qn(t) for t in order[order.index(tag) + 1:]}
        following = next((c for c in parent if c.tag in successors), None)
        if following is None:
            parent.append(element)
        else:
            following.addprevious(element)
    for name, value in attrs.items():
        element.set(qn(f"w:{name}"), value)
    return element


# ── text direction ───────────────────────────────────────────────────────
_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")


def _reads_rtl(text: str) -> bool:
    """Whether `text` reads right to left: its first strong letter is Arabic."""
    arabic, latin = _ARABIC.search(text or ""), _LATIN.search(text or "")
    return bool(arabic) and (latin is None or arabic.start() < latin.start())


def _clean(text: str) -> str:
    """Clause wording as one run of text.

    Wording lifted from a PDF keeps the source's hard line breaks, which Word
    printed as breaks mid-sentence ("third parties" / "arising in any way"),
    and may keep its ligatures ("ﬁxed"), which are written out as the letters
    they join (documents/text.py; extraction does this too, now).
    """
    from apps.documents.text import plain_letters

    return " ".join(plain_letters(text).split())


def _direction(paragraph, rtl: bool):
    """Lay `paragraph` out right to left, or left to right.

    Word takes a paragraph's direction from `w:bidi`, not from its letters.
    Without it an Arabic paragraph is laid out left to right, and the neutral
    characters -- a colon, the separator, a full stop, brackets -- land on the
    wrong side of the words they belong to. Given to Arabic paragraphs only:
    marking an English clause right to left in the Arabic document moved its
    number to the far end and its full stop to the front.
    """
    if rtl:
        _child(paragraph._p.get_or_add_pPr(), "w:bidi", _PPR_ORDER)
    return paragraph


def _add_run(paragraph, text: str, *, bold=False, italic=False, size=None,
             color=None, fill=None, rtl=None):
    """A run with its complex-script twins set too.

    Word formats Arabic letters from the complex-script properties (`w:bCs`,
    `w:szCs`), so a bold or sized run set through python-docx alone came out
    regular and body-sized in Arabic. `w:rtl` goes on runs with Arabic in
    them (or `rtl=True`: the spaces between a number and Arabic words, which
    Word otherwise sets left to right and so drops between the two). Not on
    English runs: Word sets an rtl run in the complex-script font, and that
    put the whole document in the Arabic face.
    """
    first = None
    for chunk, fallback in _segments(text):
        run = paragraph.add_run(chunk)
        first = first or run
        rPr = run._r.get_or_add_rPr()
        if fallback:
            _fallback_font(rPr)
        if bold:
            _child(rPr, "w:b", _RPR_ORDER)
            _child(rPr, "w:bCs", _RPR_ORDER)
        if italic:
            _child(rPr, "w:i", _RPR_ORDER)
            _child(rPr, "w:iCs", _RPR_ORDER)
        if color:
            _child(rPr, "w:color", _RPR_ORDER, val=color)
        if size:
            half_points = str(round(size * 2))
            _child(rPr, "w:sz", _RPR_ORDER, val=half_points)
            _child(rPr, "w:szCs", _RPR_ORDER, val=half_points)
        if fill:
            _child(rPr, "w:shd", _RPR_ORDER, val="clear", color="auto", fill=fill)
        if rtl or (rtl is None and _ARABIC.search(text or "")):
            _child(rPr, "w:rtl", _RPR_ORDER)
    return first


# ── characters the face does not have ────────────────────────────────────
# Noto Kufi Arabic has no "±" or "²". Met inside a run, Word set that
# character -- and everything after it in the run -- in a substitute face of
# its own choosing; in a long clause that changed the size mid-paragraph and
# drew the next entry over its last lines. Each such character gets a short
# run of its own in the substitute face instead.
_GLYPH_CACHE: dict[str, bool] = {}


@lru_cache(maxsize=1)
def _glyph_font():
    path = _static_file("fonts", EMBEDDED_FONTS[0][2]) if EMBEDDED_FONTS else None
    if not path:
        return None
    import pymupdf

    return pymupdf.Font(fontfile=str(path))


def _missing(ch: str) -> bool:
    if ch not in _GLYPH_CACHE:
        font = _glyph_font()
        _GLYPH_CACHE[ch] = bool(font) and not ch.isspace() and not font.has_glyph(ord(ch))
    return _GLYPH_CACHE[ch]


def _segments(text: str) -> list[tuple[str, bool]]:
    """`text` cut into (chunk, needs the substitute face) pieces."""
    pieces: list[tuple[str, bool]] = []
    for ch in text or "":
        missing = _missing(ch)
        if pieces and pieces[-1][1] == missing:
            pieces[-1] = (pieces[-1][0] + ch, missing)
        else:
            pieces.append((ch, missing))
    return pieces or [("", False)]


def _fallback_font(rPr) -> None:
    _child(rPr, "w:rFonts", _RPR_ORDER, ascii=SUBSTITUTE_FONT, hAnsi=SUBSTITUTE_FONT,
           cs=SUBSTITUTE_FONT, eastAsia=SUBSTITUTE_FONT)


def _border(paragraph, side: str, *, eighths: int, color: str, space: int = 4):
    """A rule on one side of `paragraph`. `left` is the start side: Word lays
    a right-to-left paragraph's left properties out on its right."""
    pBdr = _child(paragraph._p.get_or_add_pPr(), "w:pBdr", _PPR_ORDER)
    _child(pBdr, f"w:{side}", _BORDER_ORDER, val="single", sz=str(eighths),
           space=str(space), color=color)


def _fill(paragraph, colour: str):
    _child(paragraph._p.get_or_add_pPr(), "w:shd", _PPR_ORDER,
           val="clear", color="auto", fill=colour)


def _spacing(paragraph, *, before=None, after=None, line=None):
    from docx.shared import Pt

    fmt = paragraph.paragraph_format
    if before is not None:
        fmt.space_before = Pt(before)
    if after is not None:
        fmt.space_after = Pt(after)
    if line is not None:
        fmt.line_spacing = line


# ── tracked changes ──────────────────────────────────────────────────────
def _revision_attrs(element, revision_id: int, when: datetime) -> None:
    element.set(f"{W}id", str(revision_id))
    element.set(f"{W}author", AUTHOR)
    # Word wants a second-precision UTC stamp with a literal Z.
    element.set(f"{W}date", when.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _runs(text: str, *, deleted: bool = False) -> list:
    """`w:r` elements for `text`: one, or more where a character needs the
    substitute face. Deleted runs use `w:delText`, which Word requires."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    runs = []
    for chunk, fallback in _segments(text):
        run = OxmlElement("w:r")
        if fallback:
            _fallback_font(run.get_or_add_rPr())
        if _ARABIC.search(chunk or ""):
            _child(run.get_or_add_rPr(), "w:rtl", _RPR_ORDER)
        node = OxmlElement("w:delText" if deleted else "w:t")
        # Leading and trailing spaces are significant inside clause wording.
        node.set(qn("xml:space"), "preserve")
        node.text = chunk
        run.append(node)
        runs.append(run)
    return runs


def _mark_revision(paragraph, kind: str, revision_id: int, when: datetime) -> None:
    """Track the paragraph's own mark as inserted or deleted."""
    from docx.oxml import OxmlElement

    rPr = _child(paragraph._p.get_or_add_pPr(), "w:rPr", _PPR_ORDER)
    mark = OxmlElement(f"w:{kind}")
    _revision_attrs(mark, revision_id, when)
    rPr.insert(0, mark)                 # ins / del come first in a mark's rPr


def _proposal_paragraph(document, text: str):
    """A paragraph of clause wording, marked on its start side in yellow like
    the PDF's recommended actions."""
    from docx.shared import Pt

    paragraph = _direction(document.add_paragraph(), _reads_rtl(text))
    _border(paragraph, "left", eighths=24, color=YELLOW, space=8)
    paragraph.paragraph_format.left_indent = Pt(10)
    _spacing(paragraph, before=2, after=2, line=1.05)
    return paragraph


def _tracked_paragraphs(document, change: Change, revision_id: int, when: datetime) -> int:
    """The current wording struck through, and the proposed wording under it.

    They were one paragraph, the deletion running straight into the
    insertion ("...limited in amount.12.2 The Consultant's total..."), which
    read as one garbled clause. Now each has its own paragraph, and the
    paragraph marks are tracked too, so the result is still exact: Accept
    leaves the new wording alone in one paragraph, Reject the old.

    Returns the next free revision id.
    """
    from docx.oxml import OxmlElement

    original, replacement = _clean(change.original), _clean(change.replacement)
    if original:
        paragraph = _proposal_paragraph(document, original)
        deletion = OxmlElement("w:del")
        _revision_attrs(deletion, revision_id, when)
        deletion.extend(_runs(original, deleted=True))
        paragraph._p.append(deletion)
        # Accepting the deletion also removes this paragraph's mark, which
        # joins it -- empty by then -- to the new wording below.
        _mark_revision(paragraph, "del", revision_id + 1, when)

    if replacement:
        paragraph = _proposal_paragraph(document, replacement)
        insertion = OxmlElement("w:ins")
        _revision_attrs(insertion, revision_id + 2, when)
        insertion.extend(_runs(replacement))
        paragraph._p.append(insertion)
        # Rejecting removes the paragraph as well as its words.
        _mark_revision(paragraph, "ins", revision_id + 3, when)

    return revision_id + 4


# ── the theme ────────────────────────────────────────────────────────────
def _plain_fonts(rPr) -> None:
    """Noto Kufi Arabic by name in every slot, with the theme's font slots
    removed: a theme attribute outranks a named font, which is how the
    headings came out in Word's default Cambria."""
    from docx.oxml.ns import qn

    rFonts = _child(rPr, "w:rFonts", _RPR_ORDER)
    for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        rFonts.attrib.pop(qn(f"w:{attr}"), None)
    for attr, font in (("ascii", LATIN_FONT), ("hAnsi", LATIN_FONT),
                       ("eastAsia", LATIN_FONT), ("cs", ARABIC_FONT)):
        rFonts.set(qn(f"w:{attr}"), font)


def _brand_styles(document) -> None:
    """Black type in AJi's two faces; no theme blue, no theme borders."""
    from docx.oxml.ns import qn
    from docx.shared import Pt

    defaults = document.styles.element.find(qn("w:docDefaults"))
    if defaults is not None:
        rPr = defaults.find(f"{qn('w:rPrDefault')}/{qn('w:rPr')}")
        if rPr is not None:
            _plain_fonts(rPr)

    for name, size, bold, before, after in (
        ("Normal", BODY_PT, False, 0, 6),
        ("Title", 22, True, 0, 2),
        ("Heading 1", 16, True, 18, 6),
        ("Heading 2", 12.5, True, 16, 6),
        ("Heading 3", 11.5, True, 12, 4),
    ):
        try:
            style = document.styles[name]
        except KeyError:
            continue
        rPr = style.element.get_or_add_rPr()
        _plain_fonts(rPr)
        color = _child(rPr, "w:color", _RPR_ORDER, val=BLACK)
        for attr in ("themeColor", "themeShade", "themeTint"):
            color.attrib.pop(qn(f"w:{attr}"), None)
        for tag in ("w:sz", "w:szCs"):
            _child(rPr, tag, _RPR_ORDER, val=str(round(size * 2)))
        for tag in ("w:b", "w:bCs"):
            element = rPr.find(qn(tag))
            if bold and element is None:
                _child(rPr, tag, _RPR_ORDER)
            elif not bold and element is not None:
                rPr.remove(element)
        for tag in ("w:spacing", "w:kern"):          # the Title's letter-spacing
            element = rPr.find(qn(tag))
            if element is not None:
                rPr.remove(element)
        pPr = style.element.get_or_add_pPr()
        border = pPr.find(qn("w:pBdr"))              # the Title's blue rule
        if border is not None:
            pPr.remove(border)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        # Noto Kufi's own line height is tall; the 1.3 set for Arial doubled it.
        style.paragraph_format.line_spacing = 1.0 if bold else 1.05


def _page(document, rtl: bool) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    for section in document.sections:
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.left_margin = section.right_margin = Cm(2)
        section.top_margin, section.bottom_margin = Cm(1.8), Cm(2.2)
        section.header_distance, section.footer_distance = Cm(1), Cm(1)
        if rtl and section._sectPr.find(qn("w:bidi")) is None:
            # The section itself, so its columns run right to left too.
            section._sectPr.append(OxmlElement("w:bidi"))


def _table(container, cols: int, width_cm: float, rtl: bool):
    """A borderless full-width table; `bidiVisual` puts cell one on the right
    in Arabic, as the PDF puts its start on the right."""
    from docx.shared import Cm

    if hasattr(container, "sections"):              # the document body
        table = container.add_table(rows=1, cols=cols)
    else:                                           # a header or footer
        table = container.add_table(rows=1, cols=cols, width=Cm(width_cm))
    tblPr = table._tbl.tblPr
    if rtl:
        _child(tblPr, "w:bidiVisual", _TBLPR_ORDER)
    _child(tblPr, "w:tblW", _TBLPR_ORDER, w="5000", type="pct")
    borders = _child(tblPr, "w:tblBorders", _TBLPR_ORDER)
    for side in ("w:top", "w:left", "w:bottom", "w:right", "w:insideH", "w:insideV"):
        _child(borders, side, _BORDER_ORDER, val="nil")
    return table


def _cell(cell, *, fill=None, top=0, bottom=0, side=0):
    tcPr = cell._tc.get_or_add_tcPr()
    if fill:
        _child(tcPr, "w:shd", _TCPR_ORDER, val="clear", color="auto", fill=fill)
    margins = _child(tcPr, "w:tcMar", _TCPR_ORDER)
    for tag, value in (("w:top", top), ("w:left", side), ("w:bottom", bottom), ("w:right", side)):
        _child(margins, tag, _BORDER_ORDER, w=str(value), type="dxa")
    paragraph = cell.paragraphs[0]
    _spacing(paragraph, before=0, after=0, line=1)
    return paragraph


def _static_file(folder: str, name: str) -> Path | None:
    from django.conf import settings

    for root in list(settings.STATICFILES_DIRS) + [settings.STATIC_ROOT]:
        if root and (Path(root) / folder / name).is_file():
            return Path(root) / folder / name
    return None


def _brand_strip(document, lang: str, rtl: bool) -> None:
    """The black strip with Sanad in yellow and white, as on the PDF."""
    from docx.shared import Cm

    table = _table(document, 1, 17, rtl)
    paragraph = _direction(_cell(table.rows[0].cells[0], fill=BLACK, top=150, bottom=150, side=230), rtl)
    logo = _static_file("img", f"sanad-{lang}-dark.png")
    if logo:
        paragraph.add_run().add_picture(str(logo), height=Cm(1.05))
    else:
        _add_run(paragraph, _("سند"), bold=True, size=14, color=WHITE)
    _spacing(document.add_paragraph(), before=0, after=4)


def _field(paragraph, instruction: str, placeholder: str, **look) -> None:
    """A Word field (PAGE, NUMPAGES): Word fills it in on every page.

    Written out as begin / instruction / result / end runs, each carrying the
    footer's size and colour: a `w:fldSimple` was re-rendered by Word at the
    body size, the page numbers standing out of the footer.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for kind, text in (("begin", None), ("instr", f" {instruction} "),
                       ("separate", None), ("result", placeholder), ("end", None)):
        run = _add_run(paragraph, "", **look)
        if kind == "instr":
            node = OxmlElement("w:instrText")
            node.set(qn("xml:space"), "preserve")
            node.text = text
        elif kind == "result":
            node = OxmlElement("w:t")
            node.text = text
        else:
            node = OxmlElement("w:fldChar")
            node.set(qn("w:fldCharType"), kind)
        run._r.append(node)


def _footer(document, contract, run, rtl: bool) -> None:
    """The contract, the page and the issue on every page, like the PDF's
    running footer: a separated page can still be identified, and an older
    printout is not mistaken for the current analysis."""
    from django.utils import timezone as dj_timezone
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    footer = document.sections[0].footer
    footer.is_linked_to_previous = False
    table = _table(footer, 3, 17, rtl)
    borders = _child(table._tbl.tblPr, "w:tblBorders", _TBLPR_ORDER)
    _child(borders, "w:top", _BORDER_ORDER, val="single", sz="4", space="0", color=HAIRLINE)
    for cell, width in zip(table.rows[0].cells, (7.9, 1.2, 7.9)):
        cell.width = Cm(width)
    start, middle, end = (_cell(c, top=90) for c in table.rows[0].cells)
    note = dict(size=7.5, color=MUTED)

    _direction(start, rtl)
    _add_run(start, contract.reference or "—", **note)
    _add_run(start, " · ", rtl=rtl, **note)
    _add_run(start, contract.project_name or "", **note)

    middle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _field(middle, "PAGE", "1", **note)
    _add_run(middle, " / ", **note)
    _field(middle, "NUMPAGES", "1", **note)

    _direction(end, rtl)
    end.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    finished = (dj_timezone.localtime(run.finished_at).strftime("%Y-%m-%d")
                if run is not None and getattr(run, "finished_at", None) else "—")
    _add_run(end, _("أُصدر") + " ", rtl=rtl, **note)
    _add_run(end, finished, **note)
    _add_run(end, " · " + _("إصدار التحليل") + " ", rtl=rtl, **note)
    _add_run(end, f"v{run.version}" if run is not None else "—", **note)

    # The table is the footer's content. The footer's own empty paragraph
    # goes after it -- Word wants a table followed by a paragraph -- kept to
    # nothing.
    trailing = footer.paragraphs[0]
    footer._element.append(trailing._p)
    _spacing(trailing, before=0, after=0, line=1)
    mark = _child(trailing._p.get_or_add_pPr(), "w:rPr", _PPR_ORDER)
    _child(mark, "w:sz", _RPR_ORDER, val="2")
    _child(mark, "w:szCs", _RPR_ORDER, val="2")


def _notice(document, text: str) -> None:
    """A warning the reader must not miss: yellow tint, black start rule."""
    paragraph = _direction(document.add_paragraph(), _reads_rtl(text))
    _fill(paragraph, NOTICE_FILL)
    _border(paragraph, "left", eighths=24, color=BLACK, space=6)
    _spacing(paragraph, before=4, after=8)
    _add_run(paragraph, text, bold=True, size=10)


def _risk_label(level: str) -> str:
    return {
        "high": _("مخاطر مرتفعة"),
        "medium": _("مخاطر متوسطة"),
        "low": _("مخاطر منخفضة"),
    }.get(level, _("غير محدد"))


# ── fonts in the file ────────────────────────────────────────────────────
@lru_cache(maxsize=8)
def _font_bytes(name: str) -> bytes:
    path = _static_file("fonts", name)
    return path.read_bytes() if path else b""


def _obfuscate(raw: bytes, key: str) -> bytes:
    """Word's embedded-font obfuscation (ECMA-376 §17.8.1): the first 32
    bytes XORed with the key GUID's bytes, in reverse order."""
    secret = bytes.fromhex(key.strip("{}").replace("-", ""))[::-1]
    return bytes(b ^ secret[i % 16] for i, b in enumerate(raw[:32])) + raw[32:]


#: What Word sets the text in where Noto Kufi Arabic is not installed. The
#: brand guidelines' system typeface for widely shared documents (p. 32).
SUBSTITUTE_FONT = "Arial"


def _font_substitute(data: bytes) -> bytes:
    """Tell Word what to use where the named face is missing.

    Word falls back by the font table's hints, and with none it picked
    Cambria and Times New Roman -- a serif -- on a machine without Noto Kufi
    Arabic. The entry names Arial as the alternate and marks the family
    sans-serif, so the fallback is Arial instead.
    """
    from lxml import etree

    w_ns = W.strip("{}")
    with zipfile.ZipFile(BytesIO(data)) as source:
        names = source.namelist()
        parts = {name: source.read(name) for name in names}
    table = etree.fromstring(parts["word/fontTable.xml"])
    for family in {LATIN_FONT, ARABIC_FONT}:
        font = next((f for f in table.findall(f"{{{w_ns}}}font")
                     if f.get(f"{{{w_ns}}}name") == family), None)
        if font is None:
            font = etree.SubElement(table, f"{{{w_ns}}}font")
            font.set(f"{{{w_ns}}}name", family)
        for tag in ("altName", "charset", "family", "pitch"):     # schema order, first in w:font
            old = font.find(f"{{{w_ns}}}{tag}")
            if old is not None:
                font.remove(old)
        # ANSI, not the Arabic charset: declared Arabic-only, Word set every
        # Latin letter in Calibri. No alternate name once the face travels in
        # the file -- with Arial named, Word used it for digits and bold runs.
        hints = [("charset", "00"), ("family", "swiss"), ("pitch", "variable")]
        if not EMBEDDED_FONTS:
            hints.insert(0, ("altName", SUBSTITUTE_FONT))
        for i, (tag, value) in enumerate(hints):
            element = etree.Element(f"{{{w_ns}}}{tag}")
            element.set(f"{{{w_ns}}}val", value)
            font.insert(i, element)
    parts["word/fontTable.xml"] = etree.tostring(table, xml_declaration=True, encoding="UTF-8", standalone=True)
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name in names:
            target.writestr(name, parts[name])
    return out.getvalue()


def _embed_fonts(data: bytes) -> bytes:
    """Carry EMBEDDED_FONTS inside the .docx, so a reader without them
    installed still sees them rather than Word's substitute."""
    from lxml import etree

    fonts = [(family, slot, _font_bytes(name)) for family, slot, name in EMBEDDED_FONTS]
    fonts = [f for f in fonts if f[2]]
    if not fonts:
        return data

    w_ns = W.strip("{}")
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pr_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"

    with zipfile.ZipFile(BytesIO(data)) as source:
        names = source.namelist()
        parts = {name: source.read(name) for name in names}

    table = etree.fromstring(parts["word/fontTable.xml"])
    rels_name = "word/_rels/fontTable.xml.rels"
    rels = (etree.fromstring(parts[rels_name]) if rels_name in parts
            else etree.Element(f"{{{pr_ns}}}Relationships", nsmap={None: pr_ns}))
    for n, (family, slot, raw) in enumerate(fonts, 1):
        key = "{%s}" % str(uuid.uuid4()).upper()
        target = f"fonts/font{n}.odttf"
        parts[f"word/{target}"] = _obfuscate(raw, key)
        rid = f"rIdSanadFont{n}"
        etree.SubElement(rels, f"{{{pr_ns}}}Relationship", Id=rid, Target=target,
                         Type=f"{r_ns}/font")
        font = next((f for f in table.findall(f"{{{w_ns}}}font")
                     if f.get(f"{{{w_ns}}}name") == family), None)
        if font is None:
            font = etree.SubElement(table, f"{{{w_ns}}}font")
            font.set(f"{{{w_ns}}}name", family)
        embed = etree.SubElement(font, f"{{{w_ns}}}{slot}")
        embed.set(f"{{{r_ns}}}id", rid)
        embed.set(f"{{{w_ns}}}fontKey", key)
    parts["word/fontTable.xml"] = etree.tostring(table, xml_declaration=True, encoding="UTF-8", standalone=True)
    parts[rels_name] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)

    types = etree.fromstring(parts["[Content_Types].xml"])
    if not any(d.get("Extension") == "odttf" for d in types.findall(f"{{{ct_ns}}}Default")):
        etree.SubElement(types, f"{{{ct_ns}}}Default", Extension="odttf",
                         ContentType="application/vnd.openxmlformats-officedocument.obfuscatedFont")
    parts["[Content_Types].xml"] = etree.tostring(types, xml_declaration=True, encoding="UTF-8", standalone=True)

    # Tell Word the file carries its fonts, so a re-save keeps them.
    settings_xml = etree.fromstring(parts["word/settings.xml"])
    if settings_xml.find(f"{{{w_ns}}}embedTrueTypeFonts") is None:
        before = ("writeProtection", "view", "zoom", "removePersonalInformation",
                  "removeDateAndTime", "doNotDisplayPageBoundaries", "displayBackgroundShape",
                  "printPostScriptOverText", "printFractionalCharacterWidth", "printFormsData")
        index = 0
        for i, child in enumerate(settings_xml):
            if etree.QName(child).localname in before:
                index = i + 1
        settings_xml.insert(index, etree.Element(f"{{{w_ns}}}embedTrueTypeFonts"))
    parts["word/settings.xml"] = etree.tostring(settings_xml, xml_declaration=True, encoding="UTF-8", standalone=True)

    out = BytesIO()
    order = ["[Content_Types].xml"] + [n for n in names if n != "[Content_Types].xml"]
    order += [n for n in parts if n not in order]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target_zip:
        for name in order:
            target_zip.writestr(name, parts[name])
    return out.getvalue()


# ── the document ─────────────────────────────────────────────────────────
def build_redline(*, contract, run, changes: list[Change]) -> bytes:
    """Return the .docx bytes."""
    from docx import Document

    when = datetime.now(timezone.utc)
    lang = "en" if (get_language() or "").startswith("en") else "ar"
    rtl = lang == "ar"
    document = Document()
    _brand_styles(document)
    _page(document, rtl)

    _brand_strip(document, lang, rtl)

    title = _direction(document.add_paragraph(style="Title"), rtl)
    _add_run(title, _("مقترح تعديلات على العقد"), bold=True)

    # Who it is for, then which file it is -- the PDF's subtitle line. Two
    # short lines: as one, the date was split across a line break.
    project = _direction(document.add_paragraph(), rtl)
    _add_run(project, contract.project_name or "—", bold=True, size=10.5)
    if contract.client_name:
        _add_run(project, " · ", size=10.5, rtl=rtl)
        _add_run(project, contract.client_name, size=10.5)
    _spacing(project, after=0)
    meta = _direction(document.add_paragraph(), rtl)
    _add_run(meta, contract.reference or "—", size=9.5, color=MUTED)
    _add_run(meta, " · ", size=9.5, color=MUTED, rtl=rtl)
    _add_run(meta, when.strftime("%Y-%m-%d"), size=9.5, color=MUTED)
    # The rule under the title block, as on the PDF.
    _border(meta, "bottom", eighths=16, color=BLACK, space=8)
    _spacing(meta, after=14)

    if getattr(run, "status", "") == "degraded":
        _notice(document, _(
            "تنبيه: التحليل الذي بُني عليه هذا المقترح غير مكتمل. قد تكون هناك "
            "بنود لم تُفحص. راجع المستند الأصلي قبل الإرسال."
        ))

    if any(c.is_suggested and c.is_actionable for c in changes):
        _notice(document, _(
            "تنبيه: بعض الصياغات المقترحة في هذا الملف من اقتراح سند وليست معتمدة من "
            "الشؤون القانونية. راجعها قبل الإرسال."
        ))

    if any(c.source == "demo" or c.on_demo_standard for c in changes):
        _notice(document, _(
            "تنبيه: بعض المعايير المستخدمة بيانات اختبار وليست سياسة AJi معتمدة."
        ))

    intro = _direction(document.add_paragraph(), rtl)
    _add_run(intro, _(
        "التعديلات أدناه معلَّمة كتغييرات متتبَّعة. افتح الملف في Word ثم اقبل "
        "أو ارفض كل تغيير على حدة."
    ), size=10, color=MUTED)
    _spacing(intro, after=4)

    revision_id = 1
    for number, change in enumerate(changes, 1):
        # Separate runs, so the clause number keeps its own direction and
        # cannot swap sides with the Arabic title next to it.
        heading = _direction(document.add_paragraph(style="Heading 2"), rtl)
        heading.paragraph_format.keep_with_next = True
        _add_run(heading, f"{number}.", color=MUTED)
        _add_run(heading, " %s · %s " % (change.category, _("البند")), rtl=rtl or None)
        _add_run(heading, change.clause_number or "—")
        _add_run(heading, "   ", rtl=rtl or None)
        level = change.risk_level if change.risk_level in ("high", "medium", "low") else "unknown"
        # No-break spaces pad the badge: Word drops the fill of a plain
        # space at the end of a line.
        _add_run(heading, f" {_risk_label(level)} ", bold=True, size=8.5,
                 fill=BADGE_FILL.get(level, "F1F1F2"), color=BADGE_TEXT.get(level, BLACK))
        _border(heading, "bottom", eighths=12, color=BLACK, space=4)

        if change.is_actionable:
            revision_id = _tracked_paragraphs(document, change, revision_id, when)
        else:
            # No approved wording exists, so nothing is proposed. Saying so is
            # the honest outcome; inventing a clause here would be the failure.
            flag_text = _("لا توجد صياغة بديلة معتمدة لهذا البند — يتطلب صياغة من الشؤون القانونية.")
            flagged = _direction(document.add_paragraph(), _reads_rtl(flag_text))
            _add_run(flagged, flag_text, bold=True, size=9.5, fill=YELLOW)
            _spacing(flagged, after=4)
            original = _clean(change.original)
            if original:
                quote = _direction(document.add_paragraph(), _reads_rtl(original))
                _fill(quote, QUOTE_FILL)
                _add_run(quote, original, size=10)
                _spacing(quote, before=2, after=4, line=1.1)

        if change.rationale:
            label = _("السبب: ").strip()
            reason = _clean(change.rationale)
            if _reads_rtl(reason) == rtl:
                paragraph = _direction(document.add_paragraph(), rtl)
                _add_run(paragraph, label + " ", bold=True, color=MUTED, size=10)
                _add_run(paragraph, reason, size=10)
                _spacing(paragraph, before=4, after=2)
            else:
                # A reason in the other language gets its own line and its own
                # direction; run on after the label, its words and the label's
                # colon were reordered around each other.
                caption = _direction(document.add_paragraph(), rtl)
                _add_run(caption, label, bold=True, color=MUTED, size=9)
                _spacing(caption, before=4, after=0)
                paragraph = _direction(document.add_paragraph(), _reads_rtl(reason))
                _add_run(paragraph, reason, size=10)
                _spacing(paragraph, before=0, after=2)

        # Plain, not italic: Arabic has no italic, and Word slanted it anyway.
        provenance = _direction(document.add_paragraph(), rtl)
        _add_run(provenance, _("مصدر الصياغة") + ": ", size=8.5, color=MUTED)
        _add_run(provenance, change.source, size=8.5, color=MUTED)
        _spacing(provenance, after=10)

    _footer(document, contract, run, rtl)

    buffer = BytesIO()
    document.save(buffer)
    return _font_substitute(_embed_fonts(buffer.getvalue()))


#: A suggestion that is a note about the clause, not wording for it ("N/A -
#: Clause text is missing."). Proposing it would put the note in the contract.
_NOT_WORDING = re.compile(r"^\s*(n/?a\b|not applicable\b|غير متوفر|لا ينطبق)", re.IGNORECASE)


def changes_for_run(run) -> list[Change]:
    """Collect proposed changes from a completed analysis run."""
    changes: list[Change] = []
    findings = (
        run.findings.select_related("category", "standard_version")
        .prefetch_related("decisions")
        .order_by("-risk_score")
    )

    for finding in findings:
        # A reviewer's own edit outranks the standard's stock wording: it was
        # written for this contract, by a person, after reading the clause.
        edited = next(
            (
                d.edited_fallback_wording
                for d in finding.decisions.all()
                if d.edited_fallback_wording
            ),
            "",
        )
        version = finding.standard_version
        approved = (version.approved_fallback_wording or "") if version else ""
        suggested = False

        if edited:
            replacement, source = edited, _("تعديل المراجع")
        elif (finding.usable_fallback_wording and not _NOT_WORDING.match(finding.usable_fallback_wording)
              # The clause's own words back: deleting and re-inserting the
              # same text is a tracked change that changes nothing.
              and _clean(finding.usable_fallback_wording) != _clean(finding.original_wording)):
            # Never wording in another language than the clause's own.
            replacement = finding.usable_fallback_wording
            if approved.strip() and " ".join(approved.split()) == " ".join(replacement.split()):
                source = _("صياغة معتمدة — المعيار v%(v)s") % {"v": version.version}
                if version.is_demo_fixture:
                    source = "demo"
            else:
                # `fallback_wording` falls back to the model's suggestion when
                # the standard has no approved text. That was labelled
                # "approved wording" here; it is said to be what it is.
                source = _("صياغة مقترحة من سند — غير معتمدة من الشؤون القانونية")
                suggested = True
        else:
            replacement, source = "", _("لا يوجد")

        changes.append(Change(
            clause_number=finding.clause_number or "",
            category=(finding.category.display_name if finding.category else "—"),
            original=finding.original_wording or "",
            replacement=replacement,
            rationale=finding.deviation or finding.recommended_action or "",
            source=source,
            risk_level=finding.risk_level or "",
            is_suggested=suggested,
            on_demo_standard=bool(version and version.is_demo_fixture),
        ))

    return changes
