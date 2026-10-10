"""Clause numbers printed in a margin column (attach_margin_numbers).

OCR returned the column of numbers ("5.5", "6.0", "7.1") as blocks of their
own at the top of the page and the clause text after them, so a page read as
one clause: insurance was folded into liability, "changed circumstances" into
termination, and both were reported missing.
"""
from apps.analysis.clause_detection import TextUnit, attach_margin_numbers, group_into_clauses


def unit(i, text, y, x=60.0, page=11):
    return TextUnit(i, text, page, (x, y, x + 300, y + 12))


def test_numbers_rejoin_the_text_level_with_them():
    units = [
        unit(1, "5.5", 100, x=40), unit(2, "6.0", 200, x=40), unit(3, "7.4", 300, x=40),
        unit(4, "LIABILITY FOR PATENTS AND COPYRIGHTS. The Sub-Consultant shall be liable...", 101),
        unit(5, "INSURANCE The Sub-Consultant shall maintain insurances...", 199),
        unit(6, "CHANGED CIRCUMSTANCES. If circumstances arise for which...", 302),
    ]
    clauses = group_into_clauses(attach_margin_numbers(units))
    by_number = {c.clause_number: c.text for c in clauses}
    assert by_number["6.0"].startswith("6.0 INSURANCE")
    assert by_number["7.4"].startswith("7.4 CHANGED CIRCUMSTANCES")
    assert len(clauses) == 3


def test_a_number_with_nothing_level_with_it_stays():
    units = [unit(1, "7.4", 100, x=40), unit(2, "Some text far below", 400)]
    out = attach_margin_numbers(units)
    assert [u.text for u in out] == ["7.4", "Some text far below"]


def test_units_without_positions_are_left_alone():
    units = [TextUnit(1, "7.4", 1), TextUnit(2, "Text", 1)]
    assert [u.text for u in attach_margin_numbers(units)] == ["7.4", "Text"]


def test_arabic_article_headings_with_ocr_brackets():
    from apps.analysis.clause_detection import _clause_number_of

    assert _clause_number_of("المادة (1): التعاريف:") == "1"
    assert _clause_number_of("المادة )4 الضرائب والرسوم:") == "4"
    assert _clause_number_of("المادة 3(16 مدة العمل:") == "16"
    assert _clause_number_of("2025 was the year") is None


def test_a_named_article_is_never_a_table_row():
    units = [TextUnit(1, "5- يتعهد الفريق الأول بأن يدفع", 1), TextUnit(2, "المادة (1): التعاريف: كلمات", 2),
             TextUnit(3, "المادة (2): نطاق العمل", 2)]
    numbers = [c.clause_number for c in group_into_clauses(units)]
    assert numbers == ["5", "1", "2"]


def test_numbers_in_brackets_start_clauses():
    from apps.analysis.clause_detection import _clause_number_of

    assert _clause_number_of("(3.1) Overall co-ordination of technical matters") == "3.1"
    assert _clause_number_of("(4.13) AJi WE shall have the right") == "4.13"


def test_article_heading_inside_a_block_starts_its_own_clause():
    units = [
        TextUnit(1, "المادة (15)- مسؤوليات صاحب العمل :\n1 تقديم المتطلبات", 136),
        TextUnit(2, "5. موافاة الاستشاري بالدفعات المستحقة له.\nالمادة (16)- مراحل ومدد العمل:\nيلتزم الاستشاري", 136),
        TextUnit(3, "تفاصيل أخرى\nالمادة ( 4 ) - الضرائب والرسوم:\nيدفع الاستشاري", 137),
    ]
    clauses = group_into_clauses(units)
    numbers = [c.clause_number for c in clauses]
    assert numbers == ["15", "16", "4"]
    assert "موافاة" in clauses[0].text and clauses[0].block_ids == [1, 2]
    assert clauses[1].block_ids == [2, 3] and clauses[2].text.startswith("المادة ( 4 )")


def test_a_cross_reference_on_its_own_line_does_not_split():
    units = [TextUnit(1, "7.1 Payment shall be made\nArticle 5 of the Conditions applies\nwithin 30 days", 3)]
    clauses = group_into_clauses(units)
    assert [c.clause_number for c in clauses] == ["7.1"]
