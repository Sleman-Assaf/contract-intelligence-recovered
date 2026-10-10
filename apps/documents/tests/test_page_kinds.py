"""What each page of a contract file is (apps/documents/page_kinds.py)."""
from __future__ import annotations

import pytest

from apps.documents.models import PageKind
from apps.documents.page_kinds import classify_page, classify_pages

STAMP = "AJi Water & Environment\nT:+962 6 585 7167\naji@ajigroup.com\nAmman 11191-Jordan\nRegistration No.4509"
CLAUSES = ("4.1 The Consultant shall carry out the Services with reasonable skill and care.\n"
           "4.2 The Client shall pay the Consultant within thirty (30) days.\n"
           "4.3 Delay damages shall not exceed 10% of the Contract Price under this Agreement.")


@pytest.mark.parametrize("text,kind", [
    (CLAUSES, PageKind.CONTRACT),
    ("Attachment 2: Service Contract\nsee external pdf file", PageKind.PLACEHOLDER),
    ("APPENDIX C — PRICE BREAKDOWN\nMM HAA", PageKind.PLACEHOLDER),
    ("Curriculum Vitae\nName: Enas\nNationality: Jordanian\nEducation: BSc Civil Engineering\n"
     "Years of experience: 15\nEmployment record: ...", PageKind.CV),
    ("شهادة تصنيف مقدم الخدمة الفنية\nوزارة الأشغال العامة والإسكان\nصالحة لغاية 19/11/2025\n"
     "رقم التسجيل 4509", PageKind.REGISTRATION),
    ("", PageKind.BLANK),
])
def test_kinds(text, kind):
    assert classify_page(text, page_number=7).kind == kind


def test_the_stamp_on_every_page_is_not_a_certificate():
    assert classify_page(CLAUSES + "\n" + STAMP, page_number=9).kind == PageKind.CONTRACT


def test_a_clause_that_refers_to_an_annex_is_not_an_addendum():
    text = "يلتزم الاستشاري بتقديم الخدمات المبينة في ملحق رقم (2) من هذا العقد بموجب الشروط.\n" + CLAUSES
    assert classify_page(text, page_number=12).kind == PageKind.CONTRACT


def test_the_bid_section_is_left_out_and_a_letter_ends_it():
    pages = [
        (1, CLAUSES, None),
        (2, "عرض الشركة\nالعرض الفني للاستشاري", None),
        (3, "Our firm has completed similar projects across the region with an experienced team "
            "of engineers and planners, delivering design and supervision services for utilities.", None),
        (4, "Jordan Water Company\nالرقم: 123\nالتاريخ: 12/11/2025\nالسادة المناقصين\nملحق رقم (1) "
            "للعطاء: تعدل المادة الخامسة من الشروط الخاصة كما يلي.", None),
        (5, "9.1 Disputes shall be settled by arbitration in Amman under the laws of Jordan.\n"
            "9.2 This Agreement shall be governed by the laws of the Hashemite Kingdom of Jordan.", None),
    ]
    verdicts = classify_pages(pages)
    assert verdicts[3].kind == PageKind.BID and not verdicts[3].include
    assert verdicts[4].kind == PageKind.ADDENDUM and verdicts[4].include
    assert verdicts[5].include


def test_a_page_bound_in_twice_is_a_duplicate():
    addendum = ("Addendum No. 2\n" + " ".join(f"clause{i} reads as amended text number {i}" for i in range(40)))
    verdicts = classify_pages([(10, addendum, None), (40, addendum, None)])
    assert verdicts[40].kind == PageKind.DUPLICATE and verdicts[40].duplicate_of == 10
    assert not verdicts[40].include


def test_a_list_of_article_titles_is_a_contents_page():
    from apps.documents.page_kinds import classify_page
    titles = ["التعاريف", "نطاق العمل", "اللغة والقانون", "الضرائب والرسوم", "كفالة حسن الأداء",
              "مستوى الأداء", "سريان العقد", "واجبات الاستشاري", "التنازل", "التغييرات"]
    text = "\n".join(f"المادة ({i}): {t}: م م عع م" for i, t in enumerate(titles, 1))
    assert classify_page(text, page_number=12).kind == PageKind.CONTENTS


def test_a_page_with_a_few_full_articles_is_contract_text():
    from apps.documents.page_kinds import classify_page
    body = "يلتزم الاستشاري بموجب هذا العقد بأداء الخدمات المطلوبة منه وفقا للشروط والمواصفات المعتمدة " * 4
    text = "\n".join(f"المادة ({i}): عنوان\n{body}" for i in range(1, 5))
    assert classify_page(text, page_number=12).kind == PageKind.CONTRACT


class TestSectionsInALongFile:
    """A 179-page file: the bid copy, then the contract, then the award and
    the performance bond, then the registry certificate."""

    BODY = "يلتزم الاستشاري بموجب هذا العقد بأداء الخدمات وفقا للشروط والمواصفات المعتمدة في العطاء " * 3

    def test_a_bare_part_one_heading_does_not_end_the_bid(self):
        pages = [(1, "الجزء الأول: عرض الشركة\n" + self.BODY, 90.0),
                 (2, "الجزء الأول", 90.0),
                 (3, "عطاء خدمات استشارية\n" + self.BODY, 90.0)]
        verdicts = classify_pages(pages)
        assert verdicts[3].kind == PageKind.BID

    def test_the_bidders_own_letters_stay_in_the_bid(self):
        letter = "الرقم: 2600/45\nالتاريخ: 2026/01/22\nالسادة شركة ارابتك المحترمين\n" + self.BODY
        verdicts = classify_pages([(1, "العرض الفني\n" + self.BODY, 90.0), (2, letter, 90.0)])
        assert verdicts[2].kind == PageKind.BID

    def test_the_award_letter_and_the_bond_are_contract_documents(self):
        award = "اسم العطاء: خدمات استشارية\nالموضوع: إشعار بالإحالة النهائية رقم C-T-25\n" + self.BODY
        bond = "Housing Bank\nرقم الكفالة: 1630\nكفالة حسن تنفيذ رقم: 1630LG\n" + self.BODY
        verdicts = classify_pages([(1, "العرض الفني\n" + self.BODY, 90.0), (2, award, 90.0), (3, bond, 90.0)])
        assert verdicts[2].include and verdicts[3].include
        assert verdicts[2].reason == "the award letter"
        assert verdicts[3].reason == "the performance security"

    def test_the_registry_certificate_read_badly_is_still_a_certificate(self):
        text = ("Gila مراقبة الشمركات\nصفحة 1 من 6\nحالة السجل:(قائمة) رقم التسجيل:(4509)\n"
                "الرقم الوطني للمنشاه : 200009362\nغايات الشركة كما يلي")
        assert classify_page(text, page_number=174).kind == PageKind.REGISTRATION


def test_a_contents_page_is_not_analysed():
    titles = ["التعاريف", "نطاق العمل", "اللغة والقانون", "الضرائب والرسوم", "كفالة حسن الأداء",
              "مستوى الأداء", "سريان العقد", "واجبات الاستشاري", "التنازل", "التغييرات"]
    verdict = classify_page("\n".join(f"المادة ({i}): {t}" for i, t in enumerate(titles, 1)), page_number=2)
    assert verdict.kind == PageKind.CONTENTS and not verdict.include
