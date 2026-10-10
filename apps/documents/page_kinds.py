"""What each page of a contract file is (§12).

A signed contract file is rarely only the contract. One of 179 pages held
about 45 pages of agreement, conditions and annexes; the rest was the bid
copy, CVs, registration certificates, the tender addenda twice over, and an
older contract whose "not to exceed" figure an automated reader could take
for this one's value. Other files had appendix pages that were a title and
nothing else, or a page saying only "see external pdf file".

Each page is given a kind from its own text, by rule, and only the
contract's own pages go into the analysis by default. The reviewer sees the
list and can change any page before the analysis runs. Nothing here reads
or rewrites the text itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .critical_values import normalise_digits
from .models import PageKind

#: Kinds analysed unless the reviewer says otherwise.
ANALYSED_KINDS = {
    PageKind.CONTRACT, PageKind.SCHEDULE, PageKind.ADDENDUM, PageKind.COVER,
    PageKind.SIGNATURE, PageKind.OTHER,
}


@dataclass(frozen=True)
class PageVerdict:
    kind: str
    reason: str
    duplicate_of: int | None = None

    @property
    def include(self) -> bool:
        return self.kind in ANALYSED_KINDS


def _hits(patterns, text: str) -> int:
    return sum(1 for p in patterns if re.search(p, text, re.I))


_CLAUSE_LINE = re.compile(
    r"^\s*(?:\(?\d{1,2}(?:\.\d{1,2}){1,3}\)?|\d{1,2}[.\-)]\s|المادة|البند|Article\s+\d|Clause\s+\d|Sub-?Clause)",
    re.M | re.I)
#: A line that opens with an article title: "المادة (4): ..." / "Article 4 - ...".
_ARTICLE_TITLE = re.compile(r"^\s*(?:المادة|البند|Article|Clause)\s*[\(\)\[\]]?\s*\d{1,2}\b", re.M | re.I)
_LEGAL = [r"\bshall\b", r"\bhereby\b", r"\bagreement\b", r"\bcontract\b", r"\bparty\b|\bparties\b",
          r"\bconsultant\b", r"\bclient\b|\bemployer\b", r"يلتزم", r"الفريق الأول|الطرف الأول",
          r"الفريق الثاني|الطرف الثاني", r"العقد", r"الاستشاري", r"صاحب العمل", r"بموجب"]
_CV = [r"curriculum\s+vitae", r"\bC\.?V\.?\b", r"السيرة الذاتية", r"years of (?:professional )?experience",
       r"\beducation\b", r"employment record", r"date of birth|تاريخ الميلاد", r"\bnationality\b|الجنسية",
       r"key qualifications", r"languages?\s*:", r"المؤهلات العلمية", r"الخبرات العملية"]
_REGISTRATION = [r"certificate", r"شهادة (?:تسجيل|تصنيف|عضوية|خبرة)", r"chamber of (?:commerce|industry)",
                 r"commercial regist", r"السجل التجاري", r"نقابة المهندسين", r"registration (?:no|number)",
                 r"رخصة|ترخيص", r"وزارة الصناعة والتجارة", r"دائرة مراقبة الشركات", r"companies control",
                 r"تصنيف (?:مقدم|المكاتب)", r"valid (?:until|till)|صالحة لغاية",
                 # The registry certificate, read by OCR as "مراقية الشمركات": its fixed phrases.
                 r"مراق[بي][ةه] ال?ش\S{0,2}ركات", r"الرقم الوطني للمنش[أا][ةه]", r"حالة السجل",
                 r"غايات الشركة", r"المفوضين بالتوقيع", r"حصص الشركاء"]
_BID = [r"technical proposal", r"financial proposal", r"العرض الفني", r"العرض المالي", r"methodology",
        r"منهجية", r"work ?plan", r"خطة العمل", r"similar projects", r"المشاريع المماثلة",
        r"form\s+(?:tech|fin)", r"letter of (?:commitment|intent|availability)", r"تعهد", r"بيان الخبرات",
        r"bid (?:security|bond)|كفالة الدخول", r"our (?:firm|company) (?:has|is)"]
_ADDENDUM = [r"addendum\s*(?:no\.?|number|#)?\s*\d", r"ملحق\s+(?:رقم|العطاء)\s*\(?\d", r"ملحق رقم",
             r"clarification|استيضاح|إيضاح رقم|توضيح رقم", r"amendment\s+no"]
_PARTICULARS = [r"مدة العقد|مدة العمل|contract period|time period|duration", r"قيمة العقد|contract (?:value|price)",
                r"غرامة|delay damages|liquidated damages", r"كفالة|performance (?:security|bond)",
                r"دينار|JOD|EUR|USD|\d\s*%", r"تاريخ (?:التوقيع|توقيع)|commencement date|effective date"]
_ADDENDUM_HEADING = re.compile(
    r"^\W*(?:addendum|amendment|clarification|ملحق\s+(?:رقم|العطاء)|استيضاح|إيضاح\s+رقم|توضيح\s+رقم)",
    re.I)
_SIGNATURE = [r"signature|signed|witness", r"التوقيع|توقيع|الشاهد|شاهد", r"for and on behalf",
              r"عن الفريق|عن الطرف|المفوض بالتوقيع", r"\bname\s*:|الاسم\s*:", r"stamp|الختم"]
_COVER = [r"table of contents|contents|index", r"فهرس|المحتويات", r"\.{6,}"]
_SCHEDULE = [r"^\W*(?:schedule|annex|appendix|attachment|exhibit)\b", r"^\W*(?:ملحق|الملحق|جدول|مرفق)\b"]
_PLACEHOLDER = [r"see (?:the )?external (?:pdf )?file", r"refer to (?:the )?(?:attached|separate) (?:file|document)",
                r"provided separately", r"to be (?:attached|provided)", r"يرجى الرجوع إلى (?:الملف|المرفق)",
                r"مرفق (?:بشكل )?منفصل"]
_TITLE_ONLY = re.compile(
    r"^\W*(?:schedule|annex|appendix|attachment|exhibit|ملحق|الملحق|مرفق|جدول)\b[\w\s\-–—:()/.,&]{0,60}$",
    re.I)


#: The company stamp printed on every page, read as text: it said
#: "Registration No.4509" on every page of a contract, which must not make
#: every page look like a registration certificate.
_BOILERPLATE = re.compile(
    r"water\s*&\s*envir|registration\s*n[o0ou]|ajigroup|amman\s*1119|t\s*:\s*\+?962|\+962\s*6",
    re.I)

#: Headings that open a part of the file; the pages after one belong to it
#: until the next.
_SECTION_BID = re.compile(
    r"عرض الشركة|العرض الفني|العرض المالي|القسم\s+(?:الأول|الثاني|الثالث|الرابع|الخامس|السادس)\s*[:：]|"
    r"technical\s+(?:proposal|offer)|financial\s+(?:proposal|offer)|our\s+offer", re.I)
#: A bare "الجزء الأول" (Part One) is not a contract heading: in one file
#: Part One was the company's bid, and the pages after it were analysed.
_SECTION_CONTRACT = re.compile(
    r"الجزء\s+(?:الأول|الثاني|الثالث|الرابع|الخامس)\s*[:：\-–]?\s*(?:الشروط|العقد|الاتفاقية|اتفاقية)|"
    r"الشروط العامة|الشروط الخاصة|"
    r"قرار الإحالة|اتفاقية|general conditions|particular conditions|special conditions|"
    r"conditions of (?:contract|engagement)|form of (?:agreement|contract)|letter of (?:award|acceptance)",
    re.I)


#: The award letter and the performance security: contract documents, even
#: bound in after the bid copy, and they end it. In one file the award
#: ("إشعار بالإحالة النهائية") and the bank's performance bond came after the
#: tender's evaluation forms and were taken for more of the bid.
_AWARD = re.compile(r"إشعار\s*ب?\s*الإحالة|قرار الإحالة|الإحالة النهائية|letter of (?:award|acceptance)|"
                    r"notice of award", re.I)
_PERFORMANCE_SECURITY = re.compile(r"كفالة حسن\s+(?:ال)?(?:تنفيذ|أداء)|performance (?:bond|guarantee|security)",
                                   re.I)
_BID_SECURITY = re.compile(r"كفالة دخول|بدل دخول|bid (?:bond|security)|tender (?:bond|security)", re.I)

#: What only the employer writes: the award, addenda, clarifications.
_EMPLOYER_LETTER = re.compile(
    r"قرار الإحالة|الإحالة|إحالة العطاء|ملحق\s+(?:رقم|العطاء)|استيضاح|إيضاح رقم|توضيح رقم|"
    r"letter of (?:award|acceptance)|notice of award|addendum|clarification", re.I)

#: A letter between the parties (addenda, award letters arrive as letters on
#: the employer's letterhead): a reference and a date, or a salutation.
_LETTER = re.compile(r"(?:الرقم|Ref\.?|Our ref)\s*[:：]|(?:التاريخ|Date)\s*[:：]|السادة|Dear\s+(?:Sir|Madam|Mr|Eng)|Messrs",
                     re.I)


def strip_boilerplate(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not _BOILERPLATE.search(ln))


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text) if len(w) > 1]


def _shingles(text: str) -> set[str]:
    words = [w.lower() for w in re.findall(r"\w+", text) if len(w) > 2]
    return {" ".join(words[i:i + 3]) for i in range(max(0, len(words) - 2))}


def classify_page(text: str, *, page_number: int, confidence: float | None = None) -> PageVerdict:
    """The kind of one page, from its text alone."""
    text = strip_boilerplate(text)
    words = _words(text)
    n = len(words)
    head = "\n".join(text.strip().splitlines()[:4])
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if _hits(_PLACEHOLDER, text) and n < 80:
        return PageVerdict(PageKind.PLACEHOLDER, "says its content is in another file")
    if n <= 25 and lines and _TITLE_ONLY.match(lines[0]) and len(lines) <= 4:
        return PageVerdict(PageKind.PLACEHOLDER, "an annex title with nothing under it")
    if n < 8 or (confidence is not None and confidence < 45 and n < 60):
        return PageVerdict(PageKind.BLANK, "almost no readable text")
    clause_lines = len(_CLAUSE_LINE.findall(text))
    legal = _hits(_LEGAL, text)
    if _hits(_CV, text) >= 3 and clause_lines < 3:
        return PageVerdict(PageKind.CV, "a CV or staff document")
    if _hits(_REGISTRATION, text) >= 3 and clause_lines < 3 and legal < 5:
        return PageVerdict(PageKind.REGISTRATION, "a certificate or registration document")
    if _hits(_BID, text) >= 3 and clause_lines < 4:
        return PageVerdict(PageKind.BID, "part of the bid / tender submission")
    # A heading that opens with it: contract clauses refer to "ملحق رقم (2)"
    # in the middle of a sentence too.
    if any(_ADDENDUM_HEADING.match(ln) for ln in lines[:3]):
        return PageVerdict(PageKind.ADDENDUM, "a tender addendum or clarification")
    if page_number <= 4 and _hits(_COVER, text) >= 1 and clause_lines < 3 and n < 400:
        return PageVerdict(PageKind.COVER, "cover or contents page")
    # A contents page that does not say so: every article title in a row,
    # with a page number or dot leaders after each and nothing under it.
    # Read as contract text, each title became an empty "clause" of its own.
    titles = len(_ARTICLE_TITLE.findall(normalise_digits(text)))
    if titles >= 8 and n / titles < 40:
        return PageVerdict(PageKind.CONTENTS, "a list of article titles (contents page)")
    if n < 70 and _hits(_SIGNATURE, text) >= 2 and clause_lines < 2:
        return PageVerdict(PageKind.SIGNATURE, "signatures and stamps")
    if _hits(_SCHEDULE, head) and n >= 15:
        return PageVerdict(PageKind.SCHEDULE, "a schedule or annex")
    if clause_lines >= 2 or legal >= 3:
        return PageVerdict(PageKind.CONTRACT, "contract text")
    # A particulars sheet is short but holds the figures that matter most:
    # "مدة العقد: 180 يوما ... قيمة غرامة التأخير: 170 دينار".
    if _hits(_PARTICULARS, text) >= 2:
        return PageVerdict(PageKind.CONTRACT, "contract particulars")
    if n < 40:
        return PageVerdict(PageKind.DRAWING, "little text: a drawing, map or image")
    return PageVerdict(PageKind.OTHER, "not recognised; kept in the analysis")


def classify_pages(pages: list[tuple[int, str, float | None]]) -> dict[int, PageVerdict]:
    """Kinds for a whole file: [(page number, text, confidence)] -> verdicts.

    A page that repeats an earlier one (the same addenda bound in twice) is a
    duplicate of it.
    """
    verdicts: dict[int, PageVerdict] = {}
    seen: list[tuple[int, set[str]]] = []
    section = ""            # "bid" | "contract" | ""
    for number, text, confidence in pages:
        verdict = classify_page(text, page_number=number, confidence=confidence)
        lines = strip_boilerplate(text).strip().splitlines()
        heading, top = "\n".join(lines[:3]), "\n".join(lines[:8])
        is_letter = len(_LETTER.findall(top)) >= 2 and verdict.kind in (
            PageKind.OTHER, PageKind.DRAWING, PageKind.CONTRACT, PageKind.BID)
        opening = "\n".join(lines[:15])
        if verdict.kind in (PageKind.OTHER, PageKind.CONTRACT, PageKind.BID, PageKind.REGISTRATION,
                            PageKind.SCHEDULE, PageKind.ADDENDUM) and (
                _AWARD.search(opening)
                or (_PERFORMANCE_SECURITY.search(opening) and not _BID_SECURITY.search(text))):
            kind = "the award letter" if _AWARD.search(opening) else "the performance security"
            verdict = PageVerdict(PageKind.ADDENDUM if kind == "the award letter" else PageKind.SCHEDULE, kind)
            section = ""
        elif is_letter and section == "bid" and not _EMPLOYER_LETTER.search(text):
            # Inside the bid the bidder's own letters and certificates carry a
            # reference and a date too ("الرقم: ... التاريخ: ..."): still bid.
            verdict = PageVerdict(PageKind.BID, "inside the bid section of the file")
        elif is_letter:
            # Letters (addenda, award letters, clarifications) bind; they also
            # end a bid section, which never holds the employer's letters.
            verdict = PageVerdict(PageKind.ADDENDUM, "a letter between the parties")
            section = ""
        elif _SECTION_BID.search(heading) and verdict.kind not in (PageKind.COVER, PageKind.CONTENTS, PageKind.ADDENDUM):
            section = "bid"
        elif _SECTION_CONTRACT.search(heading) and verdict.kind not in (PageKind.COVER, PageKind.CONTENTS):
            section = "contract"
        if section == "bid" and verdict.kind in (PageKind.OTHER, PageKind.CONTRACT, PageKind.SCHEDULE,
                                                 PageKind.DRAWING):
            verdict = PageVerdict(PageKind.BID, "inside the bid section of the file")
        elif section == "contract" and verdict.kind == PageKind.OTHER:
            verdict = PageVerdict(PageKind.CONTRACT, "inside the contract section of the file")
        shingles = _shingles(text)
        if len(shingles) >= 25 and verdict.kind not in (PageKind.BLANK, PageKind.PLACEHOLDER):
            for earlier, other in seen:
                overlap = len(shingles & other) / max(1, len(shingles | other))
                if overlap >= 0.8:
                    verdict = PageVerdict(PageKind.DUPLICATE, "repeats an earlier page", earlier)
                    break
            if verdict.kind != PageKind.DUPLICATE:
                seen.append((number, shingles))
        verdicts[number] = verdict
    return verdicts
