"""Contract structure detection (§13, §20).

Clauses are assembled from stored source blocks, so every clause can be traced
back to the evidence it came from. A clause that cannot be traced is marked
`is_inferred` rather than quietly presented as if it were in the document.

Clause numbers come from the text itself, never from a model: a hallucinated
"12.3" makes the citation worthless and the audit trail indefensible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.documents.critical_values import normalise_digits
from apps.documents.models import Block

# 12.3 · 12.3.1 · 7 · ١٢.٣  — optionally preceded by a keyword.
# After a keyword the number may sit in brackets, either way round, as Arabic
# OCR returns them: "المادة (1): التعاريف:", "المادة )4 الضرائب", and with a
# stray character before the bracket, "المادة 3(16 مدة العمل" -- read as no
# clause at all, a 31-page Arabic contract came out as 12 clauses.
_CLAUSE_NUMBER = re.compile(
    r"^\s*(?P<keyword>(?:clause|article|section|البند|المادة|الفقرة)\s*(?:\d?[\(\)\[\]])?\s*)?"
    # "(3.1) Overall co-ordination ..." -- numbers set in brackets.
    r"[\(\[]?\s*"
    r"(\d{1,2}(?:\.\d{1,2}){0,3})(?![\d.]\d)(?:\s*[\.\)\(\]\-:–_]{1,3}\s*|\s+)(?=\S)",
    re.IGNORECASE,
)

# A heading line: short, often bold in the original, no terminal full stop.
_HEADING_MAX_CHARS = 90

# "subject to Clause 18.4" / "وفقاً للبند ١٨.٤"
_CROSS_REFERENCE = re.compile(
    r"(?:clause|article|section|البند|المادة|الفقرة)\s*(\d{1,2}(?:\.\d{1,2}){0,3})",
    re.IGNORECASE,
)


@dataclass
class DetectedClause:
    clause_number: str
    heading: str
    text: str
    page_number: int | None
    ordinal: int
    block_ids: list[int] = field(default_factory=list)
    referenced_clauses: list[str] = field(default_factory=list)
    is_schedule: bool = False

    @property
    def is_traceable(self) -> bool:
        return bool(self.block_ids)


def _clause_number_of(text: str) -> str | None:
    match = _CLAUSE_NUMBER.match(normalise_digits(text))
    return match.group(2) if match else None


def _introduced_by_keyword(text: str) -> bool:
    """"المادة (1)" / "Article 1": named as a clause, never a table row."""
    match = _CLAUSE_NUMBER.match(normalise_digits(text))
    return bool(match and match.group("keyword"))


def _looks_like_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > _HEADING_MAX_CHARS:
        return False
    if stripped.endswith((".", "،", "؛", ";")):
        return False
    return len(stripped.split()) <= 12


def find_cross_references(text: str) -> list[str]:
    """Clause numbers this clause depends on (§20).

    Resolving them is what stops "subject to Clause 18.4" being analysed as if
    18.4 did not exist.
    """
    found = _CROSS_REFERENCE.findall(normalise_digits(text or ""))
    return list(dict.fromkeys(found))


@dataclass
class TextUnit:
    """One piece of source text in reading order -- a contract Block or a
    region of a chat attachment -- as clause grouping sees it."""

    id: object
    text: str
    page_number: int | None
    #: Where it sits on the page (PDF points), when known.
    bbox: tuple | None = None


def detect_clauses(document) -> list[DetectedClause]:
    """Group a document's blocks into clauses in reading order.

    A new clause starts at a block whose text opens with a clause number.
    Everything until the next such block belongs to it.
    """
    # Only the pages analysed with the contract (Page.include_in_analysis):
    # bid copies, CVs, certificates and repeated pages are left out unless
    # the reviewer put them back.
    blocks = (
        Block.objects.filter(document=document, page__include_in_analysis=True)
        .select_related("page")
        .order_by("page__page_number", "block_index")
    )
    # The verified text where verification chose one (see Block.usable_text).
    return group_into_clauses(attach_margin_numbers(
        [TextUnit(b.pk, b.usable_text, b.page.page_number, b.bbox) for b in blocks]
    ))


#: A block that is only a clause number: "7.4", "6.0", "12.3.1."
_BARE_NUMBER = re.compile(r"^\s*\(?(\d{1,2}(?:\.\d{1,2}){0,3})\.?\)?\s*$")


def attach_margin_numbers(units: list[TextUnit]) -> list[TextUnit]:
    """Put clause numbers printed in a margin column back on their clauses.

    Many contracts set the clause numbers in a column of their own beside
    the text. OCR (and a PDF's own text layer) returns that column as
    separate blocks -- "5.5", "6.0", "7.1", "7.4" stacked at the top of the
    page -- and the clause text after them with no numbers at all, so the
    whole page was read as one clause: an insurance clause was folded into
    liability and "changed circumstances" into termination. Each bare number
    is joined to the block level with it on the same page.
    """
    out: list[TextUnit] = []
    by_page: dict = {}
    for unit in units:
        by_page.setdefault(unit.page_number, []).append(unit)
    prefix: dict = {}
    consumed: set = set()
    for page_units in by_page.values():
        numbers = [u for u in page_units if u.bbox and _BARE_NUMBER.match(normalise_digits(u.text or ""))]
        others = [u for u in page_units if u.bbox and u not in numbers and (u.text or "").strip()]
        for number in numbers:
            height = max(6.0, number.bbox[3] - number.bbox[1])
            best, best_gap = None, None
            for other in others:
                gap = abs(other.bbox[1] - number.bbox[1])
                if gap <= max(10.0, 1.4 * height) and (best_gap is None or gap < best_gap):
                    best, best_gap = other, gap
            if best is not None and best.id not in prefix:
                prefix[best.id] = _BARE_NUMBER.match(normalise_digits(number.text)).group(1)
                consumed.add(number.id)
    for unit in units:
        if unit.id in consumed:
            continue
        if unit.id in prefix:
            unit = TextUnit(unit.id, f"{prefix[unit.id]} {unit.text}", unit.page_number, unit.bbox)
        out.append(unit)
    return out


#: "المادة ( 4 ) - الضرائب والرسوم:" / "Article 16 – Programme": an article
#: heading -- keyword, number, then a dash or colon -- on its own line.
_INNER_HEADING = re.compile(
    r"^\s*(?:clause|article|section|البند|المادة)\s*[\(\)\[\]]?\s*\d{1,2}(?:\.\d{1,2}){0,3}"
    r"\s*[\(\)\[\]]?\s*[-–—:]",
    re.IGNORECASE,
)


def split_inner_headings(units) -> list[TextUnit]:
    """Start a new unit at an article heading OCR left inside a block.

    OCR sometimes reads the end of one article and the heading of the next
    as one block ("5. موافاة الاستشاري بالدفعات ... / المادة (16)- مراحل
    ومدد العمل:"), so article 16 ran on inside article 15 and its topic was
    reported missing. Only a keyword heading splits a block -- a numbered
    list item inside a clause stays where it is. Both parts keep the block's
    id, so each clause still traces back to its source.
    """
    out: list[TextUnit] = []
    for unit in units:
        lines = (unit.text or "").split("\n")
        start = 0
        for index in range(1, len(lines)):
            if _INNER_HEADING.match(normalise_digits(lines[index])):
                out.append(TextUnit(unit.id, "\n".join(lines[start:index]), unit.page_number, unit.bbox))
                start = index
        out.append(unit if start == 0 else
                   TextUnit(unit.id, "\n".join(lines[start:]), unit.page_number, unit.bbox))
    return out


def group_into_clauses(units) -> list[DetectedClause]:
    """The grouping itself, over any `TextUnit`s in reading order."""
    units = split_inner_headings(units)
    if not units:
        return []

    clauses: list[DetectedClause] = []
    current: DetectedClause | None = None
    preamble: list[TextUnit] = []
    #: The article the body has reached (6 for 6.2), and the annex being read.
    article = 0
    annex = ""

    for unit in units:
        text = (unit.text or "").strip()
        if not text:
            continue

        first_line = text.split("\n", 1)[0].strip()
        label = _annex_label(first_line)
        number = None if label else _clause_number_of(text)
        if number and not _introduced_by_keyword(text) and _is_table_row(number, article, annex):
            number = None                              # a row of the clause it sits in

        if label or number:
            if current is not None:
                clauses.append(current)

            heading = first_line if _looks_like_heading(first_line) else ""
            if label:
                annex = label
            elif not annex:
                article = int(number.split(".")[0])

            current = DetectedClause(
                clause_number=label or (f"{annex} {number}" if annex else number),
                heading=heading,
                text=text,
                page_number=unit.page_number,
                ordinal=len(clauses),
                block_ids=[unit.id],
                is_schedule=bool(annex),
            )
        elif current is not None:
            current.text = f"{current.text}\n{text}"
            current.block_ids.append(unit.id)
        else:
            # Title page, parties, recitals — before any numbered clause.
            preamble.append(unit)

    if current is not None:
        clauses.append(current)

    clauses = _fold_bare_headings(clauses)

    # Preamble is kept as clause 0 rather than discarded: definitions and the
    # parties' identities live there and are needed for context (§20).
    if preamble:
        text = "\n".join(u.text.strip() for u in preamble)
        clauses.insert(0, DetectedClause(
            clause_number="",
            heading="Preamble / Definitions",
            text=text,
            page_number=preamble[0].page_number,
            ordinal=0,
            block_ids=[u.id for u in preamble],
        ))
        for index, clause in enumerate(clauses):
            clause.ordinal = index

    for clause in clauses:
        clause.referenced_clauses = find_cross_references(clause.text)

    return clauses


_ANNEX_LABEL = re.compile(
    r"^\s*((?:schedule|appendix|annex|exhibit|الملحق|الجدول|المرفق)\b"
    r"(?:\s+(?:no\.?\s*)?[A-Z0-9]{1,4}\b)?)",
    re.IGNORECASE,
)


def _annex_label(first_line: str) -> str:
    """"Annex B" for a block headed "Annex B – Design Programme": an annex
    starts its own section, instead of running on inside the last article
    (Annex A's scope of services was being read as part of "Signatures").
    Only a heading counts -- "Annex D lists the key personnel." is a
    sentence."""
    if not _looks_like_heading(first_line):
        return ""
    match = _ANNEX_LABEL.match(normalise_digits(first_line))
    return match.group(1).strip() if match else ""


def _is_table_row(number: str, article: int, annex: str) -> bool:
    """A bare number that does not move the numbering on is a table row or
    list item of the clause it sits in -- the "1", "2", "3" of 6.2's payment
    table, after 6.2 -- not a new clause. Inside an annex every bare number
    is a row; dotted numbers there still start their own clauses."""
    if "." in number:
        return False
    return bool(annex) or int(number) <= article


def _fold_bare_headings(clauses: list[DetectedClause]) -> list[DetectedClause]:
    """"Article 6 – Contract Price and Payment" on its own is a title, not a
    clause: there is nothing in it to compare against a standard. It is folded
    into the sub-clause right under it (6.1), which keeps the words and the
    source blocks and gains the title as its heading. Only a direct child is
    folded into -- a bare heading followed by anything else stays as it is."""
    folded: list[DetectedClause] = []
    pending: DetectedClause | None = None
    for clause in clauses:
        if pending is not None:
            if clause.clause_number.startswith(pending.clause_number + "."):
                clause.text = f"{pending.text}\n{clause.text}"
                clause.block_ids = pending.block_ids + clause.block_ids
                clause.heading = clause.heading or pending.heading
                clause.page_number = pending.page_number or clause.page_number
            else:
                folded.append(pending)
            pending = None
        text = clause.text.strip()
        if "\n" not in text and clause.heading and clause.heading == text:
            pending = clause
            continue
        folded.append(clause)
    if pending is not None:
        folded.append(pending)
    for index, clause in enumerate(folded):
        clause.ordinal = index
    return folded


def build_context(
    clause: DetectedClause, all_clauses: list[DetectedClause], *, max_chars: int = 6000
) -> list[str]:
    """Referenced clauses and definitions, for analysing a clause in context (§20).

    Bounded, because sending an entire contract with every clause defeats the
    point of clause-level analysis (§51).
    """
    by_number = {c.clause_number: c for c in all_clauses if c.clause_number}
    context: list[str] = []
    budget = max_chars

    for number in clause.referenced_clauses:
        referenced = by_number.get(number)
        if referenced is None or referenced is clause:
            continue
        snippet = f"Clause {number}: {referenced.text}"[:2000]
        if len(snippet) > budget:
            break
        context.append(snippet)
        budget -= len(snippet)

    definitions = next(
        (c for c in all_clauses if c.heading.lower().startswith(("preamble", "definitions"))),
        None,
    )
    if definitions is not None and definitions is not clause and budget > 500:
        context.append(f"Definitions: {definitions.text}"[:budget])

    return context
