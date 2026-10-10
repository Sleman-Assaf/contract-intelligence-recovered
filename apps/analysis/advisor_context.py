"""Which parts of a contract the advisor reads for a question.

The advisor used to send the model the contract's first twelve clauses,
whatever was asked -- a question about the liability article of a
twenty-article contract was answered "the liability clause is not in the
excerpts". The clauses are now chosen for the question, within the same
budget:

  * a clause or article the question names ("12.2", "المادة 12", "Article 9");
  * clauses whose category the question is about, matched on the category's
    English and Arabic names and a few common synonyms, so an Arabic
    question finds an English contract's clause;
  * clauses sharing the question's words;
  * and, for a general question ("the main risks"), the clauses with the most
    serious findings first.

Only the current analysis's clauses: every run stores its own, and a
re-analysed contract would otherwise send the same clause twice.
"""
from __future__ import annotations

import re
import unicodedata

from django.conf import settings

#: The same size the first-twelve rule could reach (12 x 1,200 characters).
BUDGET_CHARS = 14_000
PER_CLAUSE = 1_200
#: A clause the question names is sent in fuller form.
PER_NAMED_CLAUSE = 3_000

_NUMBER = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d{1,2}){0,3})(?![\d.])")
_WORD = re.compile(r"[\w؀-ۿ]+")
_AR_PREFIXES = ("وبال", "وال", "بال", "كال", "فال", "لل", "ال")
_STOP = {
    # English
    "the", "and", "for", "this", "that", "what", "which", "with", "from", "are", "does",
    "contract", "clause", "article", "agreement", "about", "explain", "tell", "our",
    # Arabic (normalised)
    "ما", "ماذا", "هل", "هذا", "هذه", "في", "من", "على", "عن", "الى", "اي", "كيف",
    "عقد", "بند", "بنود", "ماده", "مواد", "اشرح", "اذكر", "حلل", "لي", "مع", "هو", "هي",
}
_RISK_WEIGHT = {"high": 6, "medium": 3, "low": 1}

#: Words readers use for a category that its own names do not contain.
#: Also the bridge between an Arabic question and an English contract in the
#: chat's RAG index (apps.analysis.rag): each passage carries its category's
#: words, so the Arabic ones here are what an Arabic question can match.
#: Measured against a fixed question set (QA, 2026-09-29): "قيمة العقد",
#: "الفواتير", "تبدأ/تنتهي" and "تمديد المدة" found nothing until their words
#: were listed.
SYNONYMS = {
    "delayLiq": ["غرامه", "غرامات", "penalty", "penalties", "liquidated", "تاخير", "تمديد", "مهله"],
    "liability": ["سقف", "cap", "indemnity", "indemnities", "تعويض", "مسووليه", "مسؤوليه",
                  "اضرار", "خسائر", "مطالبات"],
    "dispute": ["تحكيم", "arbitration", "نزاع", "نزاعات", "court", "courts", "محكمه", "محاكم",
                "خلاف", "خلافات", "قضاء"],
    "governing_law": ["قانون", "law", "jurisdiction", "اختصاص"],
    "financial": ["دفع", "دفعات", "payment", "payments", "invoice", "فاتوره", "فواتير", "retention",
                  "محتجزات", "fee", "fees", "اتعاب", "قيمه", "سعر", "مبلغ", "ثمن", "سداد", "مستحقات",
                  "تدفع", "يدفع"],
    "late_payment": ["تاخر", "late", "interest", "فائده", "فوائد", "سداد", "فواتير", "تدفع", "يدفع"],
    "termination": ["انهاء", "فسخ", "terminate", "termination", "ينهي", "انهائه"],
    "variations": ["تغيير", "variation", "variations", "change", "تعديلات", "تعديل"],
    "insurance": ["تامين", "insurance", "بوليصه"],
    "ip": ["ملكيه", "فكريه", "copyright", "ownership", "تصاميم", "حقوق"],
    "confidentiality": ["سريه", "confidential", "اسرار"],
    "force_majeure": ["قاهره", "majeure", "pandemic", "جائحه", "اوبئه", "وباء"],
    "suspension": ["تعليق", "suspend", "suspension", "ايقاف", "توقف"],
    "schedule": ["برنامج", "programme", "program", "extension", "تمديد", "موعد", "مده", "تبدا",
                 "بدء", "تنتهي", "ينتهي", "انتهاء", "مواعيد", "مراحل", "زمني"],
    "assignment": ["تنازل", "باطن", "assign", "assignment", "subcontract", "احاله"],
    "scope": ["نطاق", "scope", "services", "خدمات"],
    "deliverables": ["مخرجات", "تسليمات", "رسومات", "مخططات"],
    "obligations": ["التزامات", "موظفين", "كادر"],
    "acceptance": ["مراجعه", "يراجع", "اعتماد", "موافقه", "ملاحظات", "review", "approval"],
}


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub("[ً-ْـ]", "", text)                  # tashkeel, tatweel
    return (text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
                .replace("ة", "ه").replace("ى", "ي"))


def _stem(word: str) -> str:
    for prefix in _AR_PREFIXES:
        if word.startswith(prefix) and len(word) - len(prefix) >= 3:
            return word[len(prefix):]
    # "و" joined to a word ("وغرامات"), when what is left is still a word.
    if word.startswith("و") and len(word) >= 5:
        return word[1:]
    if word.isascii() and len(word) > 4 and word.endswith("s"):
        return word[:-1]
    return word


def words(text: str) -> set[str]:
    out = set()
    for raw in _WORD.findall(_normal(text)):
        if raw.isdigit() or raw in _STOP:
            continue
        stem = _stem(raw)
        if len(stem) >= 3 and stem not in _STOP:
            out.add(stem)
    return out


def _overlap(asked: set[str], found: set[str]) -> int:
    """Question words present in `found`. A word may also begin the other
    ("دفع" in "دفعات"), but only when the shorter of the two has four
    letters or more -- three-letter beginnings match nearly everything."""
    hits = 0
    for word in asked:
        if word in found or any(
                min(len(f), len(word)) >= 4 and (f.startswith(word) or word.startswith(f))
                for f in found):
            hits += 1
    return hits


def named_clauses(question: str) -> set[str]:
    """Clause and article numbers the question names."""
    from apps.documents.critical_values import normalise_digits

    return set(_NUMBER.findall(normalise_digits(question or "")))


def asked_categories(question: str, categories) -> set[str]:
    """Codes of the categories the question is about."""
    asked = words(question)
    if not asked:
        return set()
    found = set()
    for category in categories:
        vocabulary = words(f"{category.name} {category.name_ar}") | {
            _stem(_normal(s)) for s in SYNONYMS.get(category.code, [])}
        if _overlap(asked, vocabulary):
            found.add(category.code)
    return found


def current_clauses(contract):
    """The clauses of the contract's current analysis, in reading order."""
    return run_clauses(contract, contract.current_analysis)


def run_clauses(contract, run):
    """The clauses one analysis run stored, in reading order."""
    from .models import Clause

    everything = Clause.objects.filter(contract=contract).select_related("primary_category")
    if run is not None and run.document_id:
        mine = everything.filter(document_id=run.document_id)
        if run.started_at:
            mine = mine.filter(created_at__gte=run.started_at)
        if run.finished_at:
            mine = mine.filter(created_at__lte=run.finished_at)
        if mine.exists():
            return list(mine.order_by("ordinal"))
    # No run to go by: the newest copy of each clause.
    latest = {}
    for clause in everything.order_by("created_at"):
        latest[clause.ordinal] = clause
    return [latest[k] for k in sorted(latest)]


def contract_excerpts(contract, question: str) -> tuple[list[str], set[str]]:
    """The excerpts for this question, in reading order, and the categories
    they were chosen for (the standards sent alongside follow those)."""
    from apps.standards.models import ClauseCategory

    clauses = current_clauses(contract)
    if not clauses:
        return [], set()
    budget = getattr(settings, "ADVISOR_CONTRACT_BUDGET_CHARS", BUDGET_CHARS)

    run = contract.current_analysis
    risk: dict = {}
    if run is not None:
        for clause_id, level in run.findings.values_list("clause_id", "risk_level"):
            risk[clause_id] = max(risk.get(clause_id, 0), _RISK_WEIGHT.get(level, 0))

    numbers = named_clauses(question)
    topics = asked_categories(question, ClauseCategory.objects.filter(is_active=True))
    asked = words(question)

    def is_named(clause) -> bool:
        number = clause.clause_number or ""
        return any(number == n or number.startswith(n + ".") for n in numbers)

    if sum(min(len(c.text), PER_CLAUSE) for c in clauses) <= budget:
        excerpts = [f"[Clause {c.clause_number or '—'} p.{c.page_number or '—'}] "
                    f"{c.text[:PER_NAMED_CLAUSE if is_named(c) else PER_CLAUSE]}" for c in clauses]
        return excerpts, topics | {c.primary_category.code for c in clauses
                                   if c.primary_category and (is_named(c) or c.primary_category.code in topics)}

    scored = []
    for clause in clauses:
        category = clause.primary_category.code if clause.primary_category else ""
        relevance = ((100 if is_named(clause) else 0)
                     + (20 if category in topics else 0)
                     + 3 * _overlap(asked, words(f"{clause.heading} {clause.text}")))
        scored.append((relevance, risk.get(clause.pk, 0), clause))

    specific = any(relevance for relevance, _, _ in scored)
    # A specific question: the most relevant first, risk breaking ties.
    # A general one: the most serious findings first, then reading order.
    scored.sort(key=lambda row: (-row[0], -row[1], row[2].ordinal) if specific
                else (-row[1], row[2].ordinal))

    chosen, used = [], 0
    for relevance, _, clause in scored:
        if specific and relevance == 0 and used >= budget // 2:
            break                                      # the rest is padding, not context
        limit = PER_NAMED_CLAUSE if is_named(clause) else PER_CLAUSE
        text = clause.text[:limit]
        if used + len(text) > budget:
            continue
        chosen.append(clause)
        used += len(text)

    chosen.sort(key=lambda clause: clause.ordinal)
    excerpts = [
        f"[Clause {c.clause_number or '—'} p.{c.page_number or '—'}] "
        f"{c.text[:PER_NAMED_CLAUSE if is_named(c) else PER_CLAUSE]}"
        for c in chosen
    ]
    used_categories = {c.primary_category.code for c in chosen if c.primary_category}
    return excerpts, (topics | used_categories) if specific else used_categories


def standards_for(contract, categories: set[str], limit: int = 8) -> list[str]:
    """The standards the current analysis applied, those of the question's
    categories first, one per category."""
    run = contract.current_analysis
    if run is None:
        return []
    out, seen = [], set()
    findings = list(run.findings.select_related("category"))
    findings.sort(key=lambda f: (0 if f.category and f.category.code in categories else 1,
                                 -(f.risk_score or 0)))
    for finding in findings:
        if not finding.standard_position:
            continue
        key = finding.category_id or finding.pk
        if key in seen:
            continue
        seen.add(key)
        name = finding.category.name if finding.category else "standard"
        out.append(f"{name}: {finding.standard_position[:600]}")
        if len(out) >= limit:
            break
    return out
