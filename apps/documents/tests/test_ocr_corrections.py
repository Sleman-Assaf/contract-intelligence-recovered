"""Known OCR misreadings repaired by rule, and figures checked against words
(apps/documents/ocr/corrections.py). Every case was read off a real scan."""
from __future__ import annotations

import pytest

from apps.documents.ocr.corrections import (
    arabic_words_to_number,
    arabic_words_vs_figures,
    bad_ibans,
    correct,
    english_words_to_number,
    fix_arabic_percent,
    iban_is_valid,
    words_vs_figures,
)


class TestArabicPercent:
    @pytest.mark.parametrize("read,fixed", [
        ("وتكون الكفالة بنسبة 9610 من قيمة العقد", "وتكون الكفالة بنسبة 10% من قيمة العقد"),
        ("سلفة للاستشاري تعادل 9610 من قيمة العطاء", "سلفة للاستشاري تعادل 10% من قيمة العطاء"),
        ("2 ( 30 )96 من قيمة العطاء تدفع", "2 ( 30% من قيمة العطاء تدفع"),
        ("3096 من قيمة العطاء", "30% من قيمة العطاء"),
    ])
    def test_repaired_in_context(self, read, fixed):
        result = fix_arabic_percent(read)
        assert result.text == fixed
        assert result.corrections and result.corrections[0].kind == "arabic_percent"

    @pytest.mark.parametrize("text", [
        "بدفع مبلغ 9610 دينار أردني",          # an amount, not a percentage
        "رقم الهاتف 0796 123456",
        "The fee is 9610 EUR from the Client",  # not Arabic
    ])
    def test_left_alone_out_of_context(self, text):
        assert fix_arabic_percent(text).text == text


class TestKnownNames:
    def test_aji_and_client_acronyms(self):
        text = ("Adi Water & Environment and Water Authority of Jordan (WAd) under WAv-Tender No. 32, "
                "funded by KIW, paid in FURO")
        result = correct(text)
        assert result.text == ("AJi Water & Environment and Water Authority of Jordan (WAJ) under "
                               "WAJ-Tender No. 32, funded by KfW, paid in EURO")
        assert {c.kind for c in result.corrections} == {"known_name"}

    def test_a_person_called_adi_is_not_renamed(self):
        assert correct("Eng. Adi Haddad, Resident Engineer").text == "Eng. Adi Haddad, Resident Engineer"

    def test_wa_is_only_waj_where_the_water_authority_is_named(self):
        assert correct("WAd-Tender No. 3").text == "WAd-Tender No. 3"


class TestNumbersInWords:
    @pytest.mark.parametrize("words,value", [
        ("Fifty", 50), ("forty-four", 44), ("Twelve Thousand Nine Hundred Forty", 12940),
        ("two million three hundred seventy-five thousand", 2375000),
    ])
    def test_english(self, words, value):
        assert english_words_to_number(words) == value

    @pytest.mark.parametrize("words,value", [
        ("مائة وسبعة وخمسون ألفاً وثلاثمائة واثنان وسبعون", 157372),
        ("ثمانية آلاف وسبعمائة وثمانون", 8780),
        ("سبعه وثمانون الفاً وسبعمائه واثنان وتسعون", 87792),
        ("ألفان وخمسمائة", 2500),
        ("ثلاث مائة", 300),
        ("أحد عشر", 11),
    ])
    def test_arabic(self, words, value):
        assert arabic_words_to_number(words) == value

    def test_fifty_44_months(self):
        found = words_vs_figures("The time period shall be: Fifty (44) months, including 20 months")
        assert [(m.words_value, m.figure_value) for m in found] == [(50, "44")]

    def test_matching_words_raise_nothing(self):
        assert words_vs_figures("within thirty (30) calendar days") == []
        assert arabic_words_vs_figures("(87,792.05) سبعه وثمانون الفاً وسبعمائه واثنان وتسعون دينار") == []

    def test_a_misread_figure_is_caught_by_its_words(self):
        found = arabic_words_vs_figures("بدفع مبلغ 0 ثمانية آلاف وسبعمائة وثمانون دينار أردني")
        assert [(m.figure_value, m.words_value) for m in found] == [("0", 8780)]


class TestIban:
    def test_checksum(self):
        assert iban_is_valid("JO31ARAB1180000000118293079536")
        assert not iban_is_valid("JO37ARAB1180000000118293079536")

    def test_a_misread_iban_is_reported(self):
        assert bad_ibans("IBAN: JO37ARAB1180000000118293079536 (Arab Bank)") == [
            "JO37ARAB1180000000118293079536"]
        assert bad_ibans("IBAN: JO31ARAB1180000000118293079536") == []


def test_vision_compares_against_the_repaired_reading():
    """A reading vision agrees with becomes the block's final text: with the
    raw OCR text there, "AJi Water" went back to "Adi Water" whenever vision
    verification ran."""
    from types import SimpleNamespace

    from apps.documents.services import _block_region

    block = SimpleNamespace(
        original_source_text="Adi Water & Environment", corrected_text="AJi Water & Environment",
        page=SimpleNamespace(page_number=2), bbox=(0, 0, 10, 10), ocr_confidence=80.0,
        min_word_confidence=60.0, word_confidences=[], language="en", extraction_method="ocr")
    assert _block_region(block).ocr_text == "AJi Water & Environment"
    block.corrected_text = ""
    assert _block_region(block).ocr_text == "Adi Water & Environment"
