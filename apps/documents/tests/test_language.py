"""Script-based language detection.

The analysis prompts are written in the document's language. Nothing on the
native-text path used to set one, so every native PDF fell back to Arabic --
including English contracts, which were then analysed with Arabic
instructions.
"""
from __future__ import annotations

import pytest

from apps.documents.language import (
    MIN_LETTERS,
    combine,
    count_scripts,
    detect_language,
    prompt_language,
)

ARABIC = (
    "يلتزم الاستشاري بالحفاظ على تأمين المسؤولية المهنية بمبلغ لا يقل عن "
    "خمسة وعشرين مليون ريال سعودي لكل مطالبة على حدة."
)
ENGLISH = (
    "The Consultant shall maintain professional indemnity insurance with a "
    "limit of indemnity of not less than SAR 25,000,000 for each claim."
)


class TestDetection:
    def test_arabic_is_detected(self):
        assert detect_language(ARABIC) == "ar"

    def test_english_is_detected(self):
        assert detect_language(ENGLISH) == "en"

    def test_a_genuinely_bilingual_clause_is_bilingual(self):
        assert detect_language(ARABIC + " " + ENGLISH) == "bi"

    def test_an_arabic_clause_with_english_terms_stays_arabic(self):
        """Arabic contracts routinely carry English party names and units.
        That does not make them bilingual documents."""
        assert detect_language(ARABIC + " BIM DWG PDF") == "ar"

    def test_too_little_text_is_undetermined_not_guessed(self):
        assert detect_language("SAR 25") == ""
        assert detect_language("") == ""
        assert detect_language("   \n  ") == ""

    def test_digits_and_punctuation_are_not_evidence(self):
        assert detect_language("15% - 10% = 5% (2026/01/14) ####") == ""

    def test_the_threshold_is_explicit(self):
        assert detect_language("a" * (MIN_LETTERS - 1)) == ""
        assert detect_language("a" * MIN_LETTERS) == "en"

    def test_arabic_presentation_forms_are_recognised(self):
        """Some PDF producers emit presentation forms instead of the standard
        Arabic block. Missing those would report Arabic pages as undetermined."""
        assert detect_language("\ufedf\ufe8e\ufeeb\ufeae" * 5) == "ar"

    def test_counting_ignores_non_letters(self):
        arabic, latin = count_scripts("abc 123 ,.! ")
        assert (arabic, latin) == (0, 3)


class TestRollUp:
    def test_all_arabic_pages_make_an_arabic_document(self):
        assert combine(["ar", "ar", "ar"]) == "ar"

    def test_mixed_pages_make_a_bilingual_document(self):
        assert combine(["ar", "en"]) == "bi"

    def test_undetermined_pages_are_ignored(self):
        assert combine(["", "en", ""]) == "en"

    def test_nothing_determined_stays_undetermined(self):
        assert combine(["", ""]) == ""
        assert combine([]) == ""


class TestPromptLanguage:
    def test_the_first_determined_candidate_wins(self):
        assert prompt_language("en", "ar") == "en"

    def test_a_blank_candidate_is_skipped(self):
        assert prompt_language("", "en") == "en"

    def test_bilingual_is_prompted_in_arabic(self):
        assert prompt_language("bi", "en") == "ar"

    def test_the_documented_fallback_is_arabic(self):
        assert prompt_language("", "") == "ar"
