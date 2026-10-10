"""OCR and AI vision on the pages with figures, settled by rules -- no one is
asked to choose.

OCR reads every page: it is fast and knows where every word is. On the pages
that carry the figures a review turns on -- fee tables, particulars, payment
schedules, the pages a key fact was read from -- the vision model reads the
page as well, and the two readings are compared figure by figure.

    same figure in both                   -> accepted
    they differ                           -> the reading the rules support wins:
        the row's own arithmetic          558 x 382,50 = 213.435,00
        the column adding up to its total
        the same value stated on another page
        a third reading (a closer vision look at the OCR block), majority wins
    a figure only vision read             -> added only when a rule supports it
    nothing settles it                    -> OCR stands; recorded as not verified

Tested on six real scanned contracts: vision read tables better than OCR
(it kept a fee table's rows whole where OCR broke them apart), but where a
stamp or a signature covers a digit it writes a confident guess. So a vision
reading never wins on its own word: it wins only where a rule backs it, and
the letterhead and stamp lines (telephone, P.O. Box, registration number)
are never compared at all.

The original OCR text is never edited: a fix goes to `Block.corrected_text`
and is recorded in `Block.corrections`, so the reviewer sees what changed
and why.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field

from django.conf import settings

from .models import Block, Document, KeyFact, PageKind

logger = logging.getLogger(__name__)

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬٪", "01234567890123456789.,%")

#: 213.435,00 (European) · 1,234.56 · 382,50 (European decimal) · 2508 · 8.740
_FIGURE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+,\d{1,2}|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+,\d{1,2}|\d+(?:\.\d+)?)"
    r"(?![\d,]|\.\d)\s*(%)?")

#: Letterhead and stamp lines. Vision reads the stamp OCR leaves out on
#: purpose, and guesses where it is cut or signed over ("Registration No.
#: 4504" for 4509): never contract figures, never compared.
_LETTERHEAD = re.compile(
    r"\bT(?:el)?\s*[:.]|\bFax\b|P\.?\s*O\.?\s*Box|Registration\s*No|E-?mail|@|www\.|\+\s?962|\b11191\b|"
    r"ص\.\s*ب|هاتف|فاكس|السجل التجاري|^\s*(?:Page\s*)?\d{1,3}\s*(?:of\s*\d{1,3})?\s*$",
    re.I)

#: Pages that state the figures, by their words.
_FIGURE_WORDS = re.compile(
    r"total|amount|fee|price|rate|payment|invoice|remuneration|EUR|JOD|USD|%|"
    r"المبلغ|قيمة|الأتعاب|دينار|يورو|الدفع|دفعة|نسبة|المجموع|الإجمالي", re.I)


@dataclass(eq=False)
class Figure:
    raw: str
    value: float
    #: Other ways to read it: "8.740" is 8.74 or 8740.
    values: tuple[float, ...]
    line: int
    block: Block | None = None

    @property
    def key(self) -> str:
        return f"{self.value:.4f}".rstrip("0").rstrip(".")

    @property
    def digits(self) -> str:
        return re.sub(r"\D", "", self.raw)

    @property
    def significant(self) -> bool:
        return len(self.digits) >= 3


def parse_figure(raw: str) -> tuple[float, tuple[float, ...]] | None:
    text = raw.strip()
    try:
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", text):          # 213.435,00
            value = float(text.replace(".", "").replace(",", "."))
            return value, (value,)
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):         # 1,234.56
            value = float(text.replace(",", ""))
            return value, (value,)
        if re.fullmatch(r"\d+,\d{1,2}", text):                           # 382,50
            value = float(text.replace(",", "."))
            return value, (value,)
        value = float(text)
        if re.fullmatch(r"\d{1,3}\.\d{3}", text):                        # 8.740: 8.74 or 8740
            return value, (value, float(text.replace(".", "")))
        return value, (value,)
    except ValueError:
        return None


def normalise(text: str) -> str:
    text = (text or "").translate(_AR_DIGITS)
    return re.sub(r"(?<![\d.,])%\s*(\d{1,3}(?:\.\d+)?)(?![\d,])", r"\1%", text)


def figures_in(lines: list[str], blocks: list[Block | None] | None = None) -> list[Figure]:
    """Every figure on lines that are not letterhead, with its line index."""
    out = []
    for index, line in enumerate(lines):
        if _LETTERHEAD.search(line):
            continue
        for m in _FIGURE.finditer(line):
            parsed = parse_figure(m.group(1))
            if parsed is None:
                continue
            out.append(Figure(m.group(1), parsed[0], parsed[1], index,
                              blocks[index] if blocks else None))
    return out


# ── the rules ────────────────────────────────────────────────────────────

def _close(a: float, b: float, whole: bool = False) -> bool:
    """Equal to the cent -- or, for a figure stated without decimals, to the
    unit it was rounded to (10% of 87,792.05 is stated as 8,780)."""
    return abs(a - b) <= (1.0 if whole else 0.011)


def row_arithmetic(row: list[Figure]) -> set[int]:
    """Indices (into `row`) of figures in a true row equation: a x b = c,
    a x b% = c, or a + b = c for figures of three digits or more."""
    supported: set[int] = set()
    n = len(row)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            for k in range(n):
                if k in (i, j):
                    continue
                whole = not re.search(r"[.,]\d{1,2}$", row[k].raw)
                for a in row[i].values:
                    for b in row[j].values:
                        for c in row[k].values:
                            if c < 1 or a in (0, 1) or b in (0, 1):
                                continue
                            if (_close(a * b, c, whole) or _close(a * b / 100, c, whole)
                                    or (i < j and min(a, b, c) >= 100 and _close(a + b, c))):
                                supported |= {i, j, k}
    return supported


def column_totals(figures: list[Figure]) -> set[int]:
    """Indices of figures in a column that adds up to the total under it:
    the last figure of each line in a run, and the total on the line after."""
    last: dict[int, int] = {}
    for index, figure in enumerate(figures):
        last[figure.line] = index                  # the last figure on each line
    rows = [last[line] for line in sorted(last)]
    supported: set[int] = set()
    for start in range(len(rows)):
        total = 0.0
        for end in range(start, min(len(rows) - 1, start + 40)):
            total += figures[rows[end]].value
            if end == start:
                continue
            nxt = figures[rows[end + 1]]
            if nxt.value >= 100 and _close(total, nxt.value):
                supported |= {rows[r] for r in range(start, end + 2)}
    return supported


def supported(figures: list[Figure], elsewhere: set[str]) -> set[int]:
    """Indices of the figures some rule backs, within one reading of a page."""
    out = column_totals(figures)
    by_line: dict[int, list[int]] = {}
    for index, figure in enumerate(figures):
        by_line.setdefault(figure.line, []).append(index)
    for indices in by_line.values():
        row = [figures[i] for i in indices]
        out |= {indices[i] for i in row_arithmetic(row)}
    out |= {i for i, f in enumerate(figures) if f.significant and f.key in elsewhere}
    return out


def _why(index: int, figures: list[Figure], elsewhere: set[str]) -> str:
    if index in column_totals(figures):
        return "column_total"
    figure = figures[index]
    row = [f for f in figures if f.line == figure.line]
    if row.index(figure) in row_arithmetic(row):
        return "row_math"
    return "elsewhere"


# ── pages ────────────────────────────────────────────────────────────────

@dataclass
class PageCheck:
    page: int
    agreed: int = 0
    fixed: list[dict] = field(default_factory=list)
    added: list[dict] = field(default_factory=list)
    unsettled: list[dict] = field(default_factory=list)
    error: str = ""


@dataclass
class FigureCheck:
    pages: list[PageCheck] = field(default_factory=list)
    calls: int = 0
    skipped: str = ""

    @property
    def changed(self) -> bool:
        return any(p.fixed or p.added for p in self.pages)

    def summary(self) -> dict:
        return {
            "pages": [p.page for p in self.pages], "calls": self.calls, "skipped": self.skipped,
            "agreed": sum(p.agreed for p in self.pages),
            "fixed": sum(len(p.fixed) for p in self.pages),
            "added": sum(len(p.added) for p in self.pages),
            "unsettled": sum(len(p.unsettled) for p in self.pages),
        }


def pages_to_check(document: Document) -> list:
    """Scanned pages in the analysis that carry figures: the pages a key fact
    was read from first, then by how many figures they hold."""
    fact_pages = {c.get("page") for f in document.key_facts.all() for c in f.candidates}
    scored = []
    for page in document.pages.filter(has_native_text=False, include_in_analysis=True):
        if page.kind in (PageKind.SIGNATURE, PageKind.CV, PageKind.REGISTRATION, PageKind.DRAWING):
            continue
        text = normalise("\n".join(b.usable_text for b in page.blocks.order_by("block_index")))
        lines = text.splitlines()
        count = sum(1 for f in figures_in(lines) if f.significant)
        is_fact_page = page.page_number in fact_pages
        if is_fact_page or (count >= 4 and _FIGURE_WORDS.search(text)):
            scored.append((not is_fact_page, -count, page.page_number, page))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored[:settings.FIGURE_CHECK_MAX_PAGES]]


def _page_image(document: Document, page) -> bytes:
    import pymupdf

    from . import extractors
    from .storage import absolute_path

    path = absolute_path(document.source_file.stored_path)
    with pymupdf.open(str(path)) as pdf:
        rect = pdf[page.page_number - 1].rect
    return extractors.crop_region(path, page.page_number, (0, 0, rect.width, rect.height),
                                  dpi=200, padding=0, rotate=page.ocr_rotation)


def _read(image: bytes, *, language: str, page_number: int, bbox=None, strict=False, contract_id=""):
    from .verification import vision_reader

    read = vision_reader(language=language, page_number=page_number, bbox=bbox, contract_id=contract_id)
    return read(image, strict, 1 if not strict else 2)


def _language(text: str) -> str:
    letters = re.findall(r"[A-Za-z؀-ۿ]", text)
    arabic = sum(1 for c in letters if "؀" <= c <= "ۿ")
    return "ar" if letters and arabic / len(letters) > 0.3 else "en"


def _pair(ocr_only: list[Figure], vision_only: list[Figure], ocr_lines, vision_lines):
    """Pair OCR and vision figures that are readings of the same printed
    figure: similar digits ("30,00" / "130,00"), on similar lines."""
    scored = []
    for i, o in enumerate(ocr_only):
        for j, v in enumerate(vision_only):
            if abs(len(o.digits) - len(v.digits)) > 2:
                continue
            digits = difflib.SequenceMatcher(None, o.digits, v.digits).ratio()
            if digits < 0.6:
                continue
            words = difflib.SequenceMatcher(
                None, re.sub(r"[\d\W]+", " ", ocr_lines[o.line]).lower(),
                re.sub(r"[\d\W]+", " ", vision_lines[v.line]).lower()).ratio()
            scored.append((digits + 0.5 * words, i, j))
    pairs, used_o, used_v = [], set(), set()
    for _, i, j in sorted(scored, reverse=True):
        if i not in used_o and j not in used_v:
            pairs.append((ocr_only[i], vision_only[j]))
            used_o.add(i)
            used_v.add(j)
    return pairs, [v for j, v in enumerate(vision_only) if j not in used_v]


def _replace_figure(text: str, old: str, new: str) -> str | None:
    pattern = re.compile(rf"(?<![\d.,]){re.escape(old)}(?![\d]|[.,]\d)")
    if not pattern.search(text):
        return None
    return pattern.sub(new, text, count=1)


def _store(block: Block, text: str, correction: dict) -> None:
    block.corrected_text = text
    if block.final_text:
        block.final_text = text
    block.corrections = [*(block.corrections or []), correction]
    block.save(update_fields=["corrected_text", "final_text", "corrections", "updated_at"])


def _best_block(line: str, blocks: list[Block]) -> Block:
    """The block that shares the most distinctive words with the line -- a
    name like "ESSALAIMEH" counts for more than "Expert"."""
    words = {w for w in re.findall(r"[^\W\d_]{3,}", line.lower())}
    best, score = blocks[-1], 0
    for block in blocks:
        overlap = sum(len(w) for w in words & set(re.findall(r"[^\W\d_]{3,}", block.usable_text.lower())))
        if overlap > score:
            best, score = block, overlap
    return best


def check_page(document: Document, page, elsewhere: set[str], budget: dict) -> PageCheck:
    result = PageCheck(page=page.page_number)
    blocks = list(page.blocks.order_by("block_index"))
    if not blocks:
        return result
    ocr_lines, line_blocks = [], []
    for block in blocks:
        for line in normalise(block.usable_text).splitlines():
            ocr_lines.append(line)
            line_blocks.append(block)
    language = _language("\n".join(ocr_lines))
    try:
        image = _page_image(document, page)
    except Exception as exc:                           # a bad page must not stop the file
        result.error = f"image: {exc}"[:200]
        return result
    record = _read(image, language=language, page_number=page.page_number,
                   contract_id=document.contract_id)
    budget["calls"] += 1
    if not record.ok:
        result.error = f"vision: {record.error}"[:200]
        return result
    if not record.text.strip():
        result.error = "empty reading"
        return result
    vision_lines = normalise(record.text).splitlines()

    ocr = figures_in(ocr_lines, line_blocks)
    vision = figures_in(vision_lines)
    page.quality_signals = {**(page.quality_signals or {}),
                            # Every figure vision read, small ones too: "30 days" and
                            # "10%" are key facts as much as the contract value.
                            "figure_check": {"vision_keys": sorted({f.key for f in vision})}}
    page.save(update_fields=["quality_signals", "updated_at"])

    # Agreement as multisets of values.
    pool: dict[str, list[Figure]] = {}
    for f in vision:
        pool.setdefault(f.key, []).append(f)
    ocr_only = []
    #: Vision lines OCR also read (a figure on them matched): such a line is
    #: corrected figure by figure, never added a second time.
    matched_lines: set[int] = set()
    for f in ocr:
        if pool.get(f.key):
            twin = pool[f.key].pop()
            result.agreed += f.significant
            if f.significant:
                matched_lines.add(twin.line)
        elif f.significant:
            ocr_only.append(f)
    vision_only = [f for items in pool.values() for f in items if f.significant]

    ocr_support = supported(ocr, elsewhere)
    vision_support = supported(vision, elsewhere)
    pairs, unpaired = _pair(ocr_only, vision_only, ocr_lines, vision_lines)
    matched_lines |= {v.line for _, v in pairs}

    for o, v in pairs:
        o_ok, v_ok = ocr.index(o) in ocr_support, vision.index(v) in vision_support
        why = ""
        if v_ok and not o_ok:
            winner, why = "vision", _why(vision.index(v), vision, elsewhere)
        elif o_ok and not v_ok:
            winner = "ocr"
        else:
            winner = _third_reading(document, page, o, v, language, budget)
            why = "third_reading" if winner == "vision" else ""
        if winner == "vision" and o.block is not None:
            text = _replace_figure(normalise(o.block.usable_text), o.raw, v.raw)
            if text is not None:
                _store(o.block, text, {"kind": "vision_figure", "from": o.raw, "to": v.raw, "note": why})
                result.fixed.append({"from": o.raw, "to": v.raw, "why": why})
                continue
        if winner is None:
            result.unsettled.append({"ocr": o.raw, "vision": v.raw})

    # Figures only vision read (OCR lost the line): the line is added only
    # when a rule backs one of its figures -- never on vision's word alone.
    lines_to_add: dict[int, str] = {}
    for v in unpaired:
        index = vision.index(v)
        if index in vision_support and v.line not in matched_lines:
            lines_to_add.setdefault(v.line, _why(index, vision, elsewhere))
    for line_index, why in sorted(lines_to_add.items()):
        line = vision_lines[line_index].strip()
        block = _best_block(line, blocks)
        _store(block, f"{normalise(block.usable_text)}\n{line}",
               {"kind": "vision_line", "from": "", "to": line, "note": why})
        result.added.append({"line": line[:200], "why": why})
    return result


def _third_reading(document, page, o: Figure, v: Figure, language: str, budget: dict) -> str | None:
    """A closer vision look at the OCR block that holds the disputed figure;
    the reading two of the three agree on wins."""
    if o.block is None or not o.block.bbox or budget["tiebreaks"] <= 0:
        return None
    from . import extractors
    from .storage import absolute_path

    budget["tiebreaks"] -= 1
    try:
        image = extractors.crop_region(absolute_path(document.source_file.stored_path), page.page_number,
                                       o.block.bbox, dpi=300, padding=4.0, rotate=page.ocr_rotation)
    except Exception:
        return None
    record = _read(image, language=language, page_number=page.page_number, bbox=o.block.bbox,
                   strict=True, contract_id=document.contract_id)
    budget["calls"] += 1
    if not record.ok:
        return None
    keys = {f.key for f in figures_in(normalise(record.text).splitlines())}
    if v.key in keys and o.key not in keys:
        return "vision"
    if o.key in keys and v.key not in keys:
        return "ocr"
    return None


def check_figures(document: Document) -> FigureCheck:
    """Run the figure check over a document's figure pages."""
    outcome = FigureCheck()
    if not settings.FIGURE_CHECK_ENABLED:
        outcome.skipped = "disabled"
        return outcome
    try:
        from apps.ai.registry import get_ai_provider

        if not get_ai_provider().supports_vision:
            outcome.skipped = "no_vision_model"
            return outcome
    except Exception as exc:
        outcome.skipped = f"no_provider: {exc}"[:200]
        return outcome
    pages = pages_to_check(document)
    if not pages:
        outcome.skipped = "no_figure_pages"
        return outcome

    # Values the OCR text of every page states, for "stated elsewhere".
    by_page: dict[int, set[str]] = {}
    for block in Block.objects.filter(document=document, page__include_in_analysis=True).select_related("page"):
        keys = {f.key for f in figures_in(normalise(block.usable_text).splitlines()) if f.significant}
        by_page.setdefault(block.page.page_number, set()).update(keys)
    budget = {"calls": 0, "tiebreaks": settings.FIGURE_CHECK_TIEBREAK_CALLS}
    for page in pages:
        elsewhere = set().union(*(keys for number, keys in by_page.items() if number != page.page_number)) \
            if len(by_page) > 1 else set()
        result = check_page(document, page, elsewhere, budget)
        outcome.pages.append(result)
        if result.error.startswith("vision:") and all(p.error.startswith("vision:") for p in outcome.pages)                 and len(outcome.pages) >= 2:
            # The model is not answering: do not spend the rest of the file on it.
            outcome.skipped = f"vision_unavailable: {result.error}"[:200]
            break
    outcome.calls = budget["calls"]
    logger.info("Figure check on %s: %s", document.pk, outcome.summary())
    return outcome


# ── settling the key facts ───────────────────────────────────────────────

_SPECIFIC = re.compile(r"الشروط الخاصة|بيانات العقد|اتفاقية العقد|ملحق العرض|Particular Conditions|"
                       r"Special Conditions|Contract Data|Contract Agreement|Appendix to (?:Tender|Bid)", re.I)
_GENERAL = re.compile(r"الشروط العامة|General Conditions", re.I)


def _page_rank(document: Document, number: int, cache: dict) -> int:
    """2 for a page of the particular conditions or contract data (which
    override the general conditions), 0 for the general conditions."""
    if number not in cache:
        text = "\n".join(Block.objects.filter(document=document, page__page_number=number)
                         .order_by("block_index").values_list("original_source_text", flat=True))
        cache[number] = 2 if _SPECIFIC.search(text) else 0 if _GENERAL.search(text) else 1
    return cache[number]


def _checked_keys(document: Document) -> dict[int, set[str]]:
    out = {}
    for page in document.pages.all():
        keys = ((page.quality_signals or {}).get("figure_check") or {}).get("vision_keys")
        if keys is not None:
            out[page.page_number] = set(keys)
    return out


def _key(value: str) -> str:
    parsed = parse_figure(value) if value else None
    return f"{parsed[0]:.4f}".rstrip("0").rstrip(".") if parsed else value


_REASONS = {
    "vision": "vision confirmed it", "figures": "stated in figures, not words",
    "specific": "particular conditions override general conditions",
    "most_often": "stated most often", "first": "stated first",
}


def settle_facts(document: Document) -> list[dict]:
    """Choose every key fact's value by rule, without asking anyone.

    One value: it stands -- marked checked when vision read the same figure
    on that page. Several: the value vision confirmed, then one stated by the
    figures rather than words, then the particular conditions over the
    general ones, then the value stated most often, then the first. The
    others are kept in the note and returned as warnings, so a contract that
    states two different rates is reported, not silently resolved.
    """
    from django.utils import timezone, translation

    checked = _checked_keys(document)
    ranks: dict[int, int] = {}
    warnings = []
    scanned = document.pages.filter(has_native_text=False).exists()
    disputed = {(w.get("page"), _key(w.get("ocr", ""))) for w in document.warnings or []
                if w.get("kind") == "figure_unsettled"}
    for fact in document.key_facts.all():
        if fact.status == KeyFact.Status.CONFIRMED:
            continue
        candidates = [c for c in fact.candidates if c.get("included")] or fact.candidates
        if not candidates:
            if scanned and fact.name in settings.KEY_FACTS_REQUIRED:
                # Required and nowhere to be read -- often written by hand.
                _ask(fact, "not readable in the file")
            continue

        def is_checked(c) -> bool:
            return bool(c.get("native")) or _key(c.get("value", "")) in checked.get(c.get("page"), set())

        values = list(dict.fromkeys(c["value"] for c in candidates if c.get("value")))
        groups = {v: [c for c in candidates if c.get("value") == v] for v in values}

        def score(value):
            cs = groups[value]
            return (any(is_checked(c) for c in cs),
                    any(c.get("source") != "words" for c in cs),
                    max(_page_rank(document, c["page"], ranks) for c in cs),
                    len({c["page"] for c in cs}),
                    -min(c["page"] for c in cs))

        chosen = max(values, key=score)
        first = groups[chosen][0]
        verified = any(is_checked(c) for c in groups[chosen])
        note = "checked by OCR and vision" if verified else "OCR only, not verified by a second reading"
        if len(values) > 1:
            listed = ", ".join(f"{v} (p{groups[v][0]['page']})" for v in values)
            reason = ("vision" if score(chosen)[0] and not all(score(v)[0] for v in values)
                      else "figures" if score(chosen)[1] and not all(score(v)[1] for v in values)
                      else "specific" if score(chosen)[2] > min(score(v)[2] for v in values)
                      else "most_often" if score(chosen)[3] > min(score(v)[3] for v in values)
                      else "first")
            note = f"The file states {len(values)} values: {listed}. {chosen} used ({_REASONS[reason]}). " + note
            with translation.override("ar"):
                label = str(KeyFact.Name(fact.name).label)
            warnings.append({"kind": "figure_conflict", "page": first["page"], "fact": fact.name,
                             "fact_label": label, "values": listed, "chosen": chosen, "why": reason})
        fact.value, fact.unit = chosen, first.get("unit", "") or ""
        unsure = [c for c in groups[chosen] if not c.get("native") and (
            c.get("repaired") or c.get("source") == "words"
            or (c.get("confidence") is not None and c["confidence"] < settings.KEY_FACTS_MIN_CONFIDENCE)
            or (c.get("page"), _key(c.get("value", ""))) in disputed)]
        if unsure and not verified:
            # OCR was unsure of it and no second reading confirmed it: the
            # user types it in. The reading is kept as the suggestion.
            _ask(fact, note)
            continue
        fact.status = KeyFact.Status.SETTLED
        fact.vision_checked = verified
        fact.note = note[:500]
        fact.confirmed_by, fact.confirmed_at = None, timezone.now()
        fact.save(update_fields=["value", "unit", "status", "vision_checked", "note",
                                 "confirmed_by", "confirmed_at", "updated_at"])
    return warnings


def _ask(fact: KeyFact, note: str) -> None:
    # Empty, so it is typed rather than waved through; an unsure reading is
    # still offered on the screen with the image of where it was read.
    fact.value, fact.unit = "", ""
    fact.status = KeyFact.Status.NEEDS_INPUT
    fact.vision_checked = False
    fact.note = f"Not clear in the file ({note}); the user is asked to enter it."[:500]
    fact.save(update_fields=["status", "vision_checked", "note", "value", "unit", "updated_at"])


def needs_input(document: Document) -> int:
    return document.key_facts.filter(status=KeyFact.Status.NEEDS_INPUT).count()


def fill_contract_value(document: Document) -> None:
    """The settled contract value fills the contract's record when the upload
    left it empty -- as a reviewer's confirmation does."""
    from decimal import Decimal, InvalidOperation

    contract = document.contract
    fact = document.key_facts.filter(name=KeyFact.Name.CONTRACT_VALUE).first()
    if fact is None or not fact.value or contract.contract_value is not None:
        return
    try:
        contract.contract_value = Decimal(fact.value.replace(",", ""))
    except (InvalidOperation, ValueError):
        return
    fields = ["contract_value", "updated_at"]
    if fact.unit in dict(contract._meta.get_field("currency").choices):
        contract.currency = fact.unit
        fields.append("currency")
    contract.save(update_fields=fields)


def run(document: Document) -> FigureCheck:
    """The whole automatic step after extraction: check the figure pages,
    re-read the key facts from the fixed text, settle them, record why."""
    from .key_facts import analyse_document

    outcome = check_figures(document)
    if outcome.changed:
        analyse_document(document, keep_page_choices=True)
    extra = []
    for page in outcome.pages:
        for item in page.unsettled:
            extra.append({"kind": "figure_unsettled", "page": page.page, **item})
    if outcome.pages or outcome.skipped:
        extra.append({"kind": "figure_check", "page": None, **outcome.summary(),
                      "pages": ", ".join(str(p.page) for p in outcome.pages)})
    document.warnings = [*(document.warnings or []), *extra][:200]
    document.save(update_fields=["warnings", "updated_at"])
    # After the warnings: a figure the rules could not settle is asked for.
    conflicts = settle_facts(document)
    fill_contract_value(document)
    if conflicts:
        document.warnings = [*document.warnings, *conflicts][:200]
        document.save(update_fields=["warnings", "updated_at"])
    return outcome


def refresh_facts(document: Document) -> list[dict]:
    """Read the key facts again under the current rules -- after a rule is
    improved -- keeping what the vision check found and every value a person
    typed or confirmed. Returns the conflicts now found."""
    from .key_facts import analyse_document

    kept = [w for w in document.warnings or [] if w.get("kind") in ("figure_check", "figure_unsettled")]
    confirmed = {f.name: (f.value, f.unit, f.note, f.confirmed_by_id, f.confirmed_at)
                 for f in document.key_facts.filter(status=KeyFact.Status.CONFIRMED)}
    analyse_document(document, keep_page_choices=True)
    for fact in document.key_facts.filter(name__in=list(confirmed)):
        fact.value, fact.unit, fact.note, fact.confirmed_by_id, fact.confirmed_at = confirmed[fact.name]
        fact.status = KeyFact.Status.CONFIRMED
        fact.save()
    document.warnings = [*(document.warnings or []), *kept][:200]
    document.save(update_fields=["warnings", "updated_at"])
    conflicts = settle_facts(document)
    if conflicts:
        document.warnings = [*document.warnings, *conflicts][:200]
        document.save(update_fields=["warnings", "updated_at"])
    return conflicts
