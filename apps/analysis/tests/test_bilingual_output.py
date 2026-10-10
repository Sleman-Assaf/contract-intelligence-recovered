"""One finding, two readers.

The analysis is written once and read by people working in different
languages. The judgement must not change with the reader -- only the words
carrying it. And the contract's own text must not change at all: a translated
clause is a different clause, so it is shown beside the original, never in
place of it.
"""
from __future__ import annotations

import pytest
from django.utils import translation

from apps.analysis.models import Finding

pytestmark = pytest.mark.django_db

#: the clause repeated unchanged for its own language
ARABIC_UNCHANGED = "لا تتجاوز غرامات التأخير عشرة بالمئة من قيمة العقد."


def _finding(**kwargs) -> Finding:
    """An unsaved Finding: these are pure read helpers, no database needed."""
    defaults = dict(
        title="English title",
        deviation="English deviation",
        explanation="English explanation",
        business_impact="English impact",
        recommended_action="English action",
        original_wording="Liquidated damages shall not exceed ten per cent.",
    )
    defaults.update(kwargs)
    return Finding(**defaults)


class TestTheReaderSeesTheirOwnLanguage:
    def test_each_language_reads_its_own_prose(self):
        f = _finding(prose={
            "ar": {"explanation": "شرح عربي", "recommended_action": "إجراء عربي"},
            "en": {"explanation": "English explanation", "recommended_action": "English action"},
        })
        with translation.override("ar"):
            assert f.display_explanation == "شرح عربي"
            assert f.display_recommended_action == "إجراء عربي"
        with translation.override("en"):
            assert f.display_explanation == "English explanation"

    def test_a_regional_code_still_matches_its_language(self):
        f = _finding(prose={"ar": {"explanation": "شرح عربي"}})
        with translation.override("ar-sa"):
            assert f.display_explanation == "شرح عربي"

    def test_a_finding_written_before_this_feature_still_reads(self):
        """No stored prose: fall back to the column rather than show nothing."""
        f = _finding(prose={})
        with translation.override("ar"):
            assert f.display_explanation == "English explanation"

    def test_a_missing_language_falls_back_rather_than_blanking(self):
        f = _finding(prose={"en": {"explanation": "English explanation"}})
        with translation.override("ar"):
            assert f.display_explanation == "English explanation"


class TestTheContractsOwnWordsAreNotTranslated:
    def test_the_quoted_clause_is_never_replaced(self):
        f = _finding(wording_translations={"ar": "ترجمة البند"})
        with translation.override("ar"):
            assert f.original_wording == (
                "Liquidated damages shall not exceed ten per cent."
            )

    def test_the_translation_is_offered_separately(self):
        f = _finding(wording_translations={"ar": "ترجمة البند"})
        with translation.override("ar"):
            assert f.display_wording_translation == "ترجمة البند"

    def test_no_translation_is_offered_when_there_is_none(self):
        f = _finding(wording_translations={})
        with translation.override("ar"):
            assert f.display_wording_translation == ""

    def test_nothing_is_offered_when_the_clause_is_already_that_language(self):
        """A provider asked for every language returns the clause unchanged
        for the one it is already in. Showing that under a "translation"
        heading prints the same paragraph twice and implies a translation was
        made where none was needed."""
        f = _finding(wording_translations={
            "en": "Liquidated damages shall not exceed ten per cent.",
        })
        with translation.override("en"):
            assert f.display_wording_translation == ""

    def test_reflowed_whitespace_still_counts_as_the_same_text(self):
        f = _finding(wording_translations={
            "en": "Liquidated   damages shall  not exceed ten per cent.",
        })
        with translation.override("en"):
            assert f.display_wording_translation == ""


class TestTheCardDoesNotRepeatItself:
    def test_a_recommended_action_equal_to_the_approved_wording_is_hidden(self):
        wording = "Delay damages shall not exceed ten per cent."
        f = _finding(recommended_action=wording, fallback_wording=wording)
        assert f.display_recommended_action == ""

    def test_a_real_recommendation_still_shows(self):
        f = _finding(
            recommended_action="Negotiate the cap down to ten per cent.",
            fallback_wording="Delay damages shall not exceed ten per cent.",
        )
        assert f.display_recommended_action.startswith("Negotiate")


class TestAnArabicContractReadInEnglish:
    """The production case: AJi's own contracts are mostly Arabic, and the
    English side has to be readable without the original ever changing."""

    ARABIC_CLAUSE = "لا تتجاوز غرامات التأخير عشرة بالمئة من قيمة العقد."

    def _arabic_contract_finding(self):
        return _finding(
            original_wording=self.ARABIC_CLAUSE,
            title="سقف غرامات التأخير يتجاوز موقف AJi",
            prose={
                "ar": {"title": "سقف غرامات التأخير يتجاوز موقف AJi"},
                "en": {"title": "The delay-damages cap exceeds the AJi position"},
            },
            wording_translations={
                "ar": ARABIC_UNCHANGED,
                "en": "Delay damages shall not exceed ten per cent of the contract value.",
            },
        )

    def test_the_english_reader_gets_a_translation(self):
        f = self._arabic_contract_finding()
        with translation.override("en"):
            assert f.display_wording_translation.startswith("Delay damages")
            assert f.display_title == "The delay-damages cap exceeds the AJi position"

    def test_the_arabic_original_is_still_the_record_in_english(self):
        f = self._arabic_contract_finding()
        with translation.override("en"):
            assert f.original_wording == self.ARABIC_CLAUSE

    def test_the_arabic_reader_sees_no_redundant_translation(self):
        f = self._arabic_contract_finding()
        with translation.override("ar"):
            assert f.display_wording_translation == ""
            assert f.original_wording == self.ARABIC_CLAUSE
