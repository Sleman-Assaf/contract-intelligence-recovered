"""The chat's RAG format of a contract: build it, and search it.

When an analysis finishes, the contract's clauses (the canonical text the
analysis read, with clause numbers and pages) are cut into passages and
stored as a RagIndex -- next to the original file, which is kept as it was
uploaded. The chat answers only from what `search` retrieves here, plus the
AJi standards the analysis applied.

Retrieval is BM25 over normalised terms -- the same normalisation the rest of
the advisor uses (Arabic letter forms, prefixes, plurals) -- with two bridges
a plain keyword search lacks:

  * every passage also carries its category's names in both languages and
    their synonyms, and the question is expanded with the categories it is
    about, so "سقف المسؤولية" finds an English contract's liability clause;
  * a clause or article the question names ("12.2", "المادة 12") is always
    retrieved first.

No embedding model is needed: the AJi Brain exposes none, and nothing here
depends on one. A vector score can be added beside BM25 later without
changing what is stored.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter

from django.conf import settings
from django.db import transaction

from . import advisor_context as ctx

logger = logging.getLogger(__name__)

BUILDER_VERSION = "1"

#: Passage size, in words. A clause shorter than this is one passage; a
#: longer one is cut with an overlap, so a sentence at a cut is whole in one.
PASSAGE_WORDS = 170
OVERLAP_WORDS = 30

#: What one answer may be given, in characters of contract text, when the
#: file is too large to send whole.
BUDGET_CHARS = 12_000

#: A file up to this size goes to the model complete -- every page, for every
#: question -- so nothing on any page is out of the chat's reach. About 12-19
#: thousand tokens at worst (Arabic tokenises densest), well inside the
#: 32,768-token window AJI_BRAIN_MAX_NUM_CTX allows with the instructions,
#: the standards and the conversation beside it. Every file analysed so far is
#: under half of this.
WHOLE_FILE_CHARS = 48_000

#: A question about the whole document rather than a clause: on a file too
#: large to send complete, it is given part of every page instead of the
#: best-matching passages, so a page-by-page summary has every page.
_WHOLE_QUESTION = re.compile(
    r"لخص|ملخص|تلخيص|صفحه صفحه|صفحة صفحة|كل الصفحات|جميع الصفحات|كل البنود|جميع البنود|كامل|"
    r"العقد كله|الملف كله|ترجم العقد|ترجم الملف|ترجمة العقد|"
    r"summar|page by page|every page|all pages|all (the )?clauses|whole|entire|full (text|contract|file)|"
    r"translate (the )?(contract|file|document)",
    re.I,
)

#: Marks a line the chat adds about the file itself, not text from it; never
#: shown as a quotation under an answer.
FILE_NOTE = "[FILE NOTE]"

K1, B = 1.4, 0.75


#: Words every clause of a consultancy contract uses: matching on them ranks
#: the whole document equally ("what happens if the client terminates?"
#: matched every clause naming the Client).
_BOILERPLATE = {
    "shall", "client", "consultant", "party", "parties", "such", "under", "other", "will",
    "been", "have", "has", "its", "their", "any", "all", "not", "may", "must", "into",
    "each", "then", "than", "upon", "within", "without", "herein", "hereof", "thereof",
    "happen", "happens", "when", "where", "who", "why", "there", "these", "those",
    "service", "services", "servic", "خدمات", "الخدمات",
    "العميل", "الاستشاري", "استشاري", "الطرف", "الطرفين", "يجب", "يحق", "ذلك", "التي", "الذي",
    "عند", "اذا", "متى", "ماذا", "يقول", "يحدث",
}
#: English endings taken off so "terminates", "terminated" and "termination"
#: meet ("terminat"); the Arabic forms are handled by advisor_context._stem.
_EN_SUFFIXES = ("ations", "ation", "ions", "ion", "ings", "ing", "ed", "es", "s")


def _en_stem(word: str) -> str:
    for suffix in _EN_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def terms(text: str) -> list[str]:
    """The text's searchable terms, with repeats (BM25 counts them)."""
    out = []
    for raw in ctx._WORD.findall(ctx._normal(text)):
        if raw in ctx._STOP or raw in _BOILERPLATE:
            continue
        if raw.isdigit():
            if len(raw) >= 2:                      # amounts, percentages, days
                out.append(raw)
            continue
        stem = _en_stem(raw) if raw.isascii() else ctx._stem(raw)
        if len(stem) >= 3 and stem not in ctx._STOP and stem not in _BOILERPLATE:
            out.append(stem)
    return out


def _category_terms(category) -> list[str]:
    """A category's names in both languages and its synonyms, as terms."""
    if category is None:
        return []
    words = terms(f"{category.name} {category.name_ar}")
    for synonym in ctx.SYNONYMS.get(category.code, []):
        words.extend(terms(synonym))
    return words


def _windows(text: str) -> list[str]:
    words = text.split()
    if len(words) <= PASSAGE_WORDS:
        return [text.strip()] if text.strip() else []
    out, start = [], 0
    step = PASSAGE_WORDS - OVERLAP_WORDS
    while start < len(words):
        out.append(" ".join(words[start:start + PASSAGE_WORDS]))
        if start + PASSAGE_WORDS >= len(words):
            break
        start += step
    return out


def _page_segments(clause) -> list[tuple]:
    """(page, text) for each page a clause is on.

    A clause is stored with the page it starts on. One that runs over a page
    break -- Annex D beginning on page 10 and ending on 11 -- was indexed
    entirely as "p.10", so page 11 looked empty to the chat and a
    page-by-page summary skipped it. A clause on several pages is cut at each
    break, from its own source blocks, and every part carries its real page.
    """
    blocks = sorted(clause.source_blocks.select_related("page"), key=lambda b: (b.page.page_number, b.block_index))
    pages = sorted({b.page.page_number for b in blocks})
    if len(pages) <= 1:
        return [(clause.page_number or (pages[0] if pages else None), clause.text or "")]
    return [(page, "\n".join(b.usable_text for b in blocks if b.page.page_number == page))
            for page in pages]


@transaction.atomic
def build(run):
    """(Re)build the RAG index of one analysis run; returns it."""
    from .models import RagIndex, RagPassage

    contract = run.contract
    RagIndex.objects.filter(analysis=run).delete()
    index = RagIndex.objects.create(analysis=run, contract=contract,
                                    builder_version=BUILDER_VERSION)
    rows, doc_freq, total = [], Counter(), 0
    ordinal = 0
    for clause in ctx.run_clauses(contract, run):
        category = clause.primary_category
        bridge = _category_terms(category)
        for page, segment in _page_segments(clause):
            for piece in _windows(segment):
                counted = Counter(terms(f"{clause.heading} {piece}"))
                counted.update(bridge)
                if not counted:
                    continue
                doc_freq.update(counted.keys())
                length = sum(counted.values())
                total += length
                rows.append(RagPassage(
                    index=index, ordinal=ordinal, clause=clause,
                    clause_number=clause.clause_number or "", heading=(clause.heading or "")[:512],
                    page_number=page, category_code=category.code if category else "",
                    text=piece, terms=dict(counted), length=length,
                ))
                ordinal += 1
    RagPassage.objects.bulk_create(rows, batch_size=500)
    index.passages_count = len(rows)
    index.avg_terms = (total / len(rows)) if rows else 0
    index.doc_freq = dict(doc_freq)
    index.save(update_fields=["passages_count", "avg_terms", "doc_freq", "updated_at"])
    logger.info("RAG index for run %s: %d passages", run.pk, len(rows))
    return index


def build_quietly(run):
    """Build, but never fail the caller: the analysis itself has succeeded,
    and a file without an index is simply not offered to the chat yet."""
    try:
        return build(run)
    except Exception:                                  # noqa: BLE001 - logged, not raised
        logger.exception("Building the RAG index of run %s failed", run.pk)
        return None


def index_for(contract):
    """The RAG index of the contract's current analysis, or None."""
    from .models import RagIndex

    if contract is None or not contract.current_analysis_id:
        return None
    return RagIndex.objects.filter(analysis_id=contract.current_analysis_id).first()


def _matches(term: str, found: dict) -> int:
    """How often `term` occurs in a passage: exactly, or as the beginning of
    a longer word ("دفع" / "دفعات") when both have four letters or more."""
    if term in found:
        return found[term]
    if len(term) < 4:
        return 0
    return sum(n for word, n in found.items()
               if len(word) >= 4 and (word.startswith(term) or term.startswith(word)))


def _label(passage) -> str:
    return f"[Clause {passage.clause_number or '—'} p.{passage.page_number or '—'}] {passage.text}"


def search(index, question: str, budget: int | None = None) -> tuple[list[str], set[str]]:
    """The passages that answer `question`, in reading order, and the
    categories they are about (the standards sent with them follow those)."""
    from apps.standards.models import ClauseCategory

    explicit = budget is not None
    budget = budget or getattr(settings, "RAG_BUDGET_CHARS", BUDGET_CHARS)
    # Markup and links are not the question: "<script>alert(1)</script> ما
    # سقف المسؤولية؟" retrieved the definitions clause on "script" and
    # "alert" instead of the liability article. The prompt already tells the
    # model to ignore such material; retrieval ignores it too.
    question = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", question or "", flags=re.S | re.I)
    question = re.sub(r"<[^>]*>|https?://\S+", " ", question)
    passages = list(index.passages.all())
    if not passages:
        return [], set()

    # The question is matched as asked: each passage already carries its
    # category's names in both languages, so "مسؤولية" reaches an English
    # liability clause without the question being widened -- widening it by
    # every loosely matching category pulled in delay and payment clauses.
    topics = ctx.asked_categories(question, ClauseCategory.objects.filter(is_active=True))
    query = Counter(terms(question))
    numbers = ctx.named_clauses(question)

    def named(p) -> bool:
        return any(p.clause_number == n or p.clause_number.startswith(n + ".") for n in numbers)

    # Every page is in reach. A file that fits is given complete, for every
    # question; a larger one is given part of every page when the question is
    # about the whole document. Only a specific question about a large file is
    # answered from the best-matching passages. (An explicit `budget` is a
    # caller asking for a ranked selection of that size.)
    whole_limit = budget if explicit else getattr(settings, "RAG_WHOLE_FILE_CHARS", WHOLE_FILE_CHARS)
    total = sum(len(p.text) for p in passages)
    page_count = _page_count(index, passages)
    note = ""
    if total <= whole_limit:
        chosen = passages
        specific = bool(numbers or topics)
        if not explicit:
            note = (f"{FILE_NOTE} This is the complete file: all {page_count} page(s), every clause, "
                    f"in order. Nothing has been left out.")
    elif _WHOLE_QUESTION.search(ctx._normal(question)) and not numbers:
        chosen = _every_page(passages, whole_limit)
        specific = False
        note = (f"{FILE_NOTE} The file has {page_count} page(s). Every page is represented above, "
                f"in order; the longest pages are shortened.")
    else:
        n = len(passages)
        avg = index.avg_terms or 1
        scored = []
        for p in passages:
            score = 0.0
            for term, weight in query.items():
                tf = _matches(term, p.terms)
                if not tf:
                    continue
                df = index.doc_freq.get(term, 1)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                score += weight * idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * p.length / avg))
            if named(p):
                score += 100
            scored.append((score, p))
        specific = any(score > 0 for score, _ in scored)
        if specific:
            scored.sort(key=lambda row: (-row[0], row[1].ordinal))
        else:
            # Nothing in the question to match ("what are the main risks?"):
            # the clauses with the most serious findings, then reading order.
            risk = _risk_by_clause(index)
            scored.sort(key=lambda row: (-risk.get(row[1].clause_id, 0), row[1].ordinal))
        # Only passages in the same league as the best one: a passage sharing
        # one common word with the question is not an answer to it.
        floor = scored[0][0] * 0.25 if specific else 0
        chosen, used = [], 0
        for score, p in scored:
            if specific and (score <= 0 or score < floor):
                break
            if used + len(p.text) > budget:
                continue
            chosen.append(p)
            used += len(p.text)
        chosen.sort(key=lambda p: p.ordinal)

    used_categories = {p.category_code for p in chosen if p.category_code}
    excerpts = [_label(p) for p in chosen] + ([note] if note else [])
    return excerpts, (topics | used_categories) if specific else used_categories


def _page_count(index, passages) -> int:
    document = index.analysis.document if index.analysis.document_id else None
    pages = {p.page_number for p in passages if p.page_number}
    return max((document.page_count if document else 0), max(pages, default=0)) or 1


def _every_page(passages, limit: int) -> list:
    """Passages from every page, in reading order, within `limit`: each page's
    share of the budget is filled from the top of the page down."""
    by_page: dict = {}
    for p in passages:
        by_page.setdefault(p.page_number or 0, []).append(p)
    share = limit // max(1, len(by_page))
    chosen = []
    for page in sorted(by_page):
        used = 0
        for p in by_page[page]:
            if used and used + len(p.text) > share:
                break
            chosen.append(p)
            used += len(p.text)
    return sorted(chosen, key=lambda p: p.ordinal)


def _risk_by_clause(index) -> dict:
    run = index.analysis
    risk: dict = {}
    for clause_id, level in run.findings.values_list("clause_id", "risk_level"):
        risk[clause_id] = max(risk.get(clause_id, 0), ctx._RISK_WEIGHT.get(level, 0))
    return risk
