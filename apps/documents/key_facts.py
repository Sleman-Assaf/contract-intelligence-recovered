"""The figures a review turns on, found by rule and held for confirmation.

After extraction every page is given a kind (page_kinds.py), every stated
value of a few key terms is collected with its page and position, and the
file's own inconsistencies are listed. A fact whose readings disagree -- two
delay-damage rates on two pages, "Fifty (44) months", a figure the amount in
words contradicts -- or that rests on a repaired or uncertain OCR reading is
shown to a reviewer before the analysis starts (views: confirm_facts).

Rules only: no language model reads or proposes a value here (§53).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction

from .models import Block, Document, ExtractionMethod, KeyFact, PageKind
from .ocr.corrections import arabic_words_vs_figures, bad_ibans, words_vs_figures
from .page_kinds import classify_pages

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬٪", "01234567890123456789.,%")

#: 1,234.56 / 1.234,56 (European, as in a German partner's tables) / 1234.
NUM = r"(\d{1,3}(?:\.\d{3})+,\d{1,2}(?!\d)|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
CUR = r"(JOD|JD|EUR|EURO|€|USD|US\$|\$|SAR|AED|OMR)"
AR_CUR = r"(دينار|يورو|دولار|ريال|درهم)"


@dataclass(frozen=True)
class Pattern:
    regex: str
    value_group: int
    unit: str = ""                 # fixed unit, or "" to read unit_group
    unit_group: int = 0
    minimum: float | None = None


def _p(regex, value_group=1, unit="", unit_group=0, minimum=None):
    return Pattern(regex, value_group, unit, unit_group, minimum)


PATTERNS: dict[str, list[Pattern]] = {
    KeyFact.Name.CONTRACT_VALUE: [
        _p(rf"(?:contract (?:value|price|amount|sum)|total (?:lump[- ]sum )?(?:amount|price|fee)s?|"
           rf"lump[- ]sum (?:amount|fee|price)|accepted contract amount|remuneration ceiling|"
           rf"total amount of services)[^\d\n]{{0,60}}?{CUR}?\s*{NUM}", 2, unit_group=1, minimum=1000),
        _p(rf"(?:قيمة العقد|قيمة العطاء|قيمة الاتفاقية|المبلغ الإجمالي|قيمة الأتعاب|إجمالي قيمة)"
           rf"[^\d\n]{{0,60}}?\(?\s*{NUM}", 1, minimum=1000),
    ],
    KeyFact.Name.DURATION: [
        _p(r"(?:time period|duration|period of (?:the )?(?:services|contract|agreement)|contract period|"
           r"completion period)[^\n\d]{0,60}?\(?(\d{1,4})\)?\s*(?:calendar\s+)?(days?|months?|years?|weeks?)",
           1, unit_group=2),
        _p(r"(?:مدة العقد|مدة العمل|مدة الاتفاقية|مدة التنفيذ|مدة الخدمات)\s*[:：]?\s*\(?\s*(\d{1,4})\s*\)?\s*"
           r"(يوم(?:اً|ا)?|أيام|شهر(?:اً|ا)?|أشهر|سنة|سنوات)", 1, unit_group=2),
    ],
    KeyFact.Name.DELAY_DAMAGES_RATE: [
        _p(rf"(?:delay damages|liquidated damages|damages for delay|delay penalt(?:y|ies))[^\n\d]{{0,80}}?"
           rf"{CUR}?\s*{NUM}\s*(%|percent|per (?:calendar )?day|/\s*day|per week)?", 2, unit_group=3),
        _p(rf"(?:غرامة|غرامات)\s+(?:ال)?تأخ[يـ]*ر[^\n\d]{{0,60}}?\(?\s*{NUM}\s*\)?\s*(%|{AR_CUR[1:-1]})?",
           1, unit_group=2),
        # "غرامة مقدارها (125 دينار) عن كل يوم تأخير": the delay comes after.
        # Brackets either way: Arabic OCR reads "(125 دينار)" as ")125 دينار(".
        # The closing words are often misread ("تأخير" came back as "WALT"),
        # so "عن كل يوم" / "لكل يوم" mark it as well.
        _p(rf"(?:غرامة|غرامات)\s+(?:مقدارها|قدرها|بقيمة|بمبلغ)\s*[()]?\s*{NUM}\s*[()]?\s*(%|{AR_CUR[1:-1]})?"
           rf"[^\n\d]{{0,30}}?(?:تأخ[يـ]*ر|عن كل يوم|لكل يوم|يومي)", 1, unit_group=2),
        # ...and with those words misread: "غرامة (pln 125( lalate عن كل يوم
        # تأخير" -- "مقدارها" and "دينار" came back garbled, and article 7's
        # 125 a day went unseen beside the particulars' 170. "Per day" after
        # the figure keeps a cap ("غرامة ... 15%") out.
        _p(rf"(?:غرامة|غرامات)\s*[^\d\n%]{{0,15}}?[()]?\s*{NUM}\s*[()]?[^\n\d%]{{0,30}}?(?:عن كل يوم|لكل يوم)",
           1, unit="JOD"),
    ],
    KeyFact.Name.DELAY_DAMAGES_CAP: [
        _p(r"(?:damages|penalt)[^.]{0,160}?(?:maximum|cap|not (?:to )?exceed|limited to)[^.\d]{0,40}?"
           r"(\d+(?:\.\d+)?)\s*%", 1, unit="%"),
        _p(r"(?:الغرامة|غرامة|للغرامة|الغرامات)[^.\n]{0,80}?(?:سقف|حد(?:ه)?\s*(?:الأعلى|أعلى)|لا تتجاوز|بحد أقصى)"
           r"[^\d\n]{0,30}?(\d+(?:\.\d+)?)\s*%", 1, unit="%"),
    ],
    KeyFact.Name.PERFORMANCE_BOND: [
        _p(r"performance (?:security|bond|guarantee)[^.]{0,120}?(\d+(?:\.\d+)?)\s*%", 1, unit="%"),
        _p(r"(?:كفالة|ضمان)\s+حسن\s+(?:ال)?تنفيذ[^.\n]{0,80}?(\d+(?:\.\d+)?)\s*%", 1, unit="%"),
        _p(r"(?:الكفالة|كفالة)[^.\n]{0,40}?بنسبة\s*\(?\s*(\d+(?:\.\d+)?)\s*%", 1, unit="%"),
    ],
    KeyFact.Name.ADVANCE_PAYMENT: [
        _p(r"advance payment[^.]{0,80}?(\d+(?:\.\d+)?)\s*%", 1, unit="%"),
        _p(r"(?:سلفة|دفعة مقدمة|الدفعة المقدمة)[^.\n]{0,60}?(\d+(?:\.\d+)?)\s*%", 1, unit="%"),
    ],
    KeyFact.Name.RETENTION: [
        _p(r"retention(?: money)?[^.]{0,80}?(\d+(?:\.\d+)?)\s*%", 1, unit="%"),
        _p(r"(?:المحتجزات|محتجزات)[^.\n]{0,60}?(\d+(?:\.\d+)?)\s*%", 1, unit="%"),
    ],
    KeyFact.Name.PAYMENT_DAYS: [
        _p(r"(?:pay(?:ment)?s?|paid|invoices?)[^.]{0,100}?within\s+(?:[a-z\-]+\s*)?\(?(\d{1,3})\)?\s*"
           r"(?:calendar\s+|working\s+)?days", 1, unit="days"),
        # The day word is optional ("يوما" was read as "Logs"); months and
        # years are not days.
        _p(r"(?:تدفع|الدفع|دفع|يدفع|صرف)[^.\n]{0,80}?(?:خلال|في غضون)\s*[()]?\s*(\d{1,3})\s*[()]?"
           r"(?!\s*(?:شهر|أشهر|سنة|سنوات|months?|years?))", 1, unit="days"),
    ],
    KeyFact.Name.LIABILITY_CAP: [
        _p(r"liability[^.]{0,150}?(?:shall not exceed|limited to|not exceed|capped at)\s*([^.\n]{3,80})", 1),
        _p(r"(?:المسؤولية|مسؤولية)[^.\n]{0,100}?(?:لا تتجاوز|بحد أقصى|سقف)\s*([^.\n]{3,80})", 1),
    ],
}

_UNITS = {
    "jd": "JOD", "jod": "JOD", "دينار": "JOD", "eur": "EUR", "euro": "EUR", "€": "EUR", "يورو": "EUR",
    "usd": "USD", "us$": "USD", "$": "USD", "دولار": "USD", "sar": "SAR", "ريال": "SAR",
    "aed": "AED", "درهم": "AED", "omr": "OMR",
}


def _unit(raw: str, fact: str) -> str:
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    if raw in _UNITS:
        return _UNITS[raw]
    if raw.startswith(("day", "يوم", "أيام")) or "/" in raw and "day" in raw:
        return "days"
    if raw.startswith(("month", "شهر", "أشهر")):
        return "months"
    if raw.startswith(("year", "سنة", "سنوات")):
        return "years"
    if raw.startswith("week"):
        return "weeks"
    if raw in ("%", "percent"):
        return "%"
    if "per" in raw and "day" in raw:
        return "per day"
    return raw


def normalise(text: str) -> str:
    """Western digits; and "%15" -- how Arabic OCR writes a percentage it
    read right to left -- as "15%"."""
    text = (text or "").translate(_AR_DIGITS)
    return re.sub(r"(?<![\d.,])%\s*(\d{1,3}(?:\.\d+)?)(?![\d,])", r"\1%", text)


def _value(raw: str) -> str:
    raw = raw.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", raw):      # 690.945,38
        raw = raw.replace(".", "").replace(",", ".")
    v = raw.replace(",", "").strip()
    if re.fullmatch(r"\d+\.0+", v):
        v = v.split(".")[0]
    return v


@dataclass
class FactsSummary:
    facts: list[KeyFact] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    pages_excluded: int = 0
    needs_confirmation: bool = False


def _value_confidence(block: Block, raw: str) -> float | None:
    """OCR's confidence in the very word that holds the value -- not the
    block's worst word, which is usually some other word entirely."""
    digits = re.sub(r"\D", "", raw)
    best = None
    for item in block.word_confidences or []:
        try:
            word, conf = item[0], float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if digits and digits in re.sub(r"\D", "", normalise(str(word))):
            best = conf if best is None else min(best, conf)
    if best is not None:
        return best
    return block.ocr_confidence


def _candidates(blocks: list[Block], kinds: dict[int, tuple[str, bool]]) -> dict[str, list[dict]]:
    """Every reading of every fact, page by page.

    Matched over the whole page, not block by block: OCR often splits a
    stated value from its label ("Lump-sum amount of" / "9,940 JOD" landed
    in two blocks). Each reading is credited to the block its value is in.
    """
    found: dict[str, list[dict]] = {name: [] for name in PATTERNS}
    by_page: dict[int, list[Block]] = {}
    for block in blocks:
        by_page.setdefault(block.page.page_number, []).append(block)
    for page, page_blocks in by_page.items():
        kind, included = kinds.get(page, (PageKind.CONTRACT, True))
        parts, spans, cursor = [], [], 0
        for block in page_blocks:
            text = re.sub(r"\s+", " ", normalise(block.usable_text)).strip()
            parts.append(text)
            spans.append((cursor, cursor + len(text), block))
            cursor += len(text) + 1
        flat = " ".join(parts)

        def block_at(position: int) -> Block:
            for a, b, block in spans:
                if a <= position <= b:
                    return block
            return spans[-1][2]

        for name, patterns in PATTERNS.items():
            for pattern in patterns:
                for m in re.finditer(pattern.regex, flat, re.I):
                    raw = m.group(pattern.value_group)
                    if not raw:
                        continue
                    value = _value(raw) if name != KeyFact.Name.LIABILITY_CAP else raw.strip()[:120]
                    if pattern.minimum is not None:
                        try:
                            if float(value) < pattern.minimum:
                                continue
                        except ValueError:
                            continue
                    block = block_at(m.start(pattern.value_group))
                    repaired_to = {normalise(c.get("to", "")) for c in (block.corrections or [])}
                    unit = pattern.unit or _unit(m.group(pattern.unit_group) if pattern.unit_group else "", name)
                    quote = flat[max(0, m.start() - 50): m.end() + 50].strip()
                    native = block.extraction_method in (ExtractionMethod.NATIVE_PDF, ExtractionMethod.DOCX)
                    found[name].append({
                        "value": value,
                        "display": raw.strip(),
                        "unit": unit,
                        "page": page,
                        "page_kind": kind,
                        "included": included,
                        "block": block.pk,
                        "bbox": list(block.bbox) if block.bbox else None,
                        "confidence": None if native else _value_confidence(block, raw),
                        "native": native,
                        "repaired": any(raw.strip() in r or value in r for r in repaired_to),
                        "source": "text",
                        "quote": quote[:220],
                    })
    return found


def _status(candidates: list[dict]) -> str:
    if not candidates:
        return KeyFact.Status.NOT_FOUND
    in_contract = [c for c in candidates if c.get("included")] or candidates
    values = {(c["value"], c.get("unit", "")) for c in in_contract}
    if len({v for v, _ in values}) > 1:
        return KeyFact.Status.CONFLICT
    uncertain = any(
        c.get("repaired") or c.get("source") == "words"
        or (not c.get("native") and c.get("confidence") is not None
            and c["confidence"] < settings.KEY_FACTS_MIN_CONFIDENCE)
        for c in in_contract)
    return KeyFact.Status.UNCERTAIN if uncertain else KeyFact.Status.FOUND


_VALUE_LABEL = re.compile(r"قيمة العقد|قيمة العطاء|قيمة الاتفاقية|contract (?:value|price|amount)|"
                          r"lump[- ]sum|total amount", re.I)


def _file_warnings(document: Document, blocks: list[Block], verdicts) -> tuple[list[dict], list[dict]]:
    """(warnings, extra candidates from figures/words disagreements).

    Stored as data, not sentences: the page words them in the reader's
    language (templates/documents/_file_warning.html).
    """
    warnings, extra = [], []
    for number, verdict in sorted(verdicts.items()):
        if verdict.kind == PageKind.PLACEHOLDER:
            warnings.append({"kind": "placeholder", "page": number})
    # Figures against words over the whole page: an amount in words often
    # runs on into the next OCR block, and cut at the block it read as "87".
    by_page: dict[int, list[Block]] = {}
    for block in blocks:
        by_page.setdefault(block.page.page_number, []).append(block)
    for page, page_blocks in by_page.items():
        flat = " ".join(normalise(b.usable_text) for b in page_blocks)
        for mismatch in words_vs_figures(flat) + arabic_words_vs_figures(flat):
            holder = next((b for b in page_blocks if mismatch.figure in normalise(b.usable_text)),
                          page_blocks[0])
            warnings.append({"kind": "words_vs_figure", "page": page, "figure": mismatch.figure,
                             "words": mismatch.words, "words_value": mismatch.words_value})
            extra.append({"page": page, "block": holder.pk, "words": mismatch.words,
                          "figure": mismatch.figure_value, "words_value": str(mismatch.words_value),
                          "bbox": list(holder.bbox) if holder.bbox else None,
                          "labelled_value": bool(_VALUE_LABEL.search(normalise(holder.usable_text)))})
    for block in blocks:
        text = normalise(block.usable_text)
        page = block.page.page_number
        for iban in bad_ibans(text):
            warnings.append({"kind": "iban_checksum", "page": page, "iban": iban})
        for c in block.corrections or []:
            warnings.append({"kind": "ocr_repair", "page": page, "from": c.get("from", ""),
                             "to": c.get("to", ""), "repair": c.get("kind", ""), "note": c.get("note", "")})
    rotated = list(document.pages.filter(ocr_rotation__gt=0).values_list("page_number", flat=True))
    if rotated:
        warnings.append({"kind": "rotated", "page": None, "pages": ", ".join(str(n) for n in rotated)})
    return warnings, extra


@transaction.atomic
def analyse_document(document: Document, *, keep_page_choices: bool = False) -> FactsSummary:
    """Page kinds, key facts and file warnings for an extracted document."""
    pages = list(document.pages.all())
    texts: dict[int, list[str]] = {p.page_number: [] for p in pages}
    blocks = list(Block.objects.filter(document=document).select_related("page")
                  .order_by("page__page_number", "block_index"))
    for block in blocks:
        texts[block.page.page_number].append(block.usable_text)
    verdicts = classify_pages([(p.page_number, "\n".join(texts[p.page_number]),
                                p.ocr_confidence if not p.has_native_text else None) for p in pages])
    for page in pages:
        verdict = verdicts[page.page_number]
        page.kind = verdict.kind
        page.kind_reason = verdict.reason
        page.duplicate_of = verdict.duplicate_of
        if not keep_page_choices:
            page.include_in_analysis = verdict.include
        page.save(update_fields=["kind", "kind_reason", "duplicate_of", "include_in_analysis", "updated_at"])
    kinds = {p.page_number: (p.kind, p.include_in_analysis) for p in pages}

    found = _candidates(blocks, kinds)
    warnings, extra = _file_warnings(document, blocks, verdicts)

    # A figure the amount in words contradicts: both readings become
    # candidates of the fact the figure belongs to.
    for item in extra:
        for name in (KeyFact.Name.CONTRACT_VALUE, KeyFact.Name.DURATION, KeyFact.Name.PERFORMANCE_BOND):
            hits = [c for c in found[name] if c["block"] == item["block"]
                    and _value(c["value"]) == _value(item["figure"])]
            for c in hits:
                found[name].append({**c, "value": item["words_value"], "display": item["words"],
                                    "source": "words", "repaired": False})
            # Only where the text says it is the contract's value: the amount
            # in words of a bank guarantee is not the contract value.
            if (not hits and name == KeyFact.Name.CONTRACT_VALUE and item.get("labelled_value")
                    and int(item["words_value"]) >= 1000):
                kind, included = kinds.get(item["page"], (PageKind.CONTRACT, True))
                found[name].append({"value": item["words_value"], "display": item["words"], "unit": "",
                                    "page": item["page"], "page_kind": kind, "included": included,
                                    "block": item["block"], "bbox": item["bbox"], "confidence": None,
                                    "native": False, "repaired": False, "source": "words",
                                    "quote": f"{item['figure']} … {item['words']}"})

    document.key_facts.all().delete()
    facts = []
    for name in PATTERNS:
        candidates = sorted(found[name], key=lambda c: (c["page"], str(c["block"])))
        # The same reading of the same block once.
        unique, seen = [], set()
        for c in candidates:
            key = (c["block"], c["value"], c.get("source"))
            if key not in seen:
                seen.add(key)
                unique.append(c)
        status = _status(unique)
        chosen = unique[0] if status == KeyFact.Status.FOUND else None
        facts.append(KeyFact.objects.create(
            document=document, name=name, status=status, candidates=[
                {k: (str(v) if k == "block" else v) for k, v in c.items()} for c in unique[:20]],
            value=chosen["value"] if chosen else "", unit=chosen.get("unit", "") if chosen else "",
        ))

    document.warnings = warnings[:200]
    document.save(update_fields=["warnings", "updated_at"])

    scanned = document.pages.filter(has_native_text=False).exists()
    mode = settings.KEY_FACTS_CONFIRMATION
    attention = any(f.needs_attention for f in facts)
    needs = mode == "always" or (mode == "scanned" and (scanned or attention)) or (mode == "conflicts" and attention)
    return FactsSummary(facts=facts, warnings=warnings,
                        pages_excluded=sum(1 for p in pages if not p.include_in_analysis),
                        needs_confirmation=needs)
