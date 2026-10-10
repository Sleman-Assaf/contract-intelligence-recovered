"""A punctuation slip in the model's JSON does not cost the clause.

The repair mends escaping only; the strict schema still decides whether the
answer is usable, so nothing missing or invented gets through.
"""
from __future__ import annotations

import json

import pytest

from apps.ai.providers.base import AIProvider, AIResponseInvalid, repair_json
from apps.ai.schemas import SemanticAssessment

parse = AIProvider.parse_or_raise


def _raw(deviation: str, **extra) -> str:
    fields = {"ai_result": '"partial"', "deviation": deviation, "explanation": '"Above the cap."',
              "confidence": "0.8", **extra}
    return "{\n" + ",\n".join(f'  "{k}": {v}' for k, v in fields.items()) + "\n}"


class TestTheSlipsSeenInRealRuns:
    def test_a_defined_term_quoted_inside_the_text(self):
        # "Expecting ',' delimiter" -- the contract's own quotes copied in
        raw = _raw('"The "Client" may withhold payment, and the "Deliverables" are unpriced."')
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)
        parsed = parse(SemanticAssessment, raw)
        assert parsed.deviation == 'The "Client" may withhold payment, and the "Deliverables" are unpriced.'

    def test_a_quoted_term_followed_by_a_comma_in_prose(self):
        raw = _raw('"Liability to the "Client", which is uncapped, exceeds the standard."')
        assert parse(SemanticAssessment, raw).deviation.startswith('Liability to the "Client", which')

    def test_a_quoted_term_at_the_end_of_the_text(self):
        raw = _raw('"The cap excludes the "Services""')
        assert parse(SemanticAssessment, raw).deviation == 'The cap excludes the "Services"'

    def test_a_trailing_comma(self):
        raw = '{"ai_result": "partial", "exclusions_found": ["negligence",], "confidence": 0.7,}'
        assert parse(SemanticAssessment, raw).exclusions_found == ["negligence"]

    def test_a_raw_line_break_and_text_around_the_object(self):
        raw = 'Here is the result:\n{"ai_result": "partial", "deviation": "Line one\nline two", "confidence": 0.7}\nDone.'
        assert parse(SemanticAssessment, raw).deviation == "Line one\nline two"

    def test_arabic_text_is_untouched(self):
        raw = _raw('"يحق لـ "العميل" حجب الدفعات"')
        assert parse(SemanticAssessment, raw).deviation == 'يحق لـ "العميل" حجب الدفعات'


class TestTheRepairNeverDecidesTheAnswer:
    def test_valid_json_is_unchanged(self):
        raw = '{"a": "x \\"y\\" z", "b": [1, 2], "c": {"d": null}}'
        assert json.loads(repair_json(raw)) == json.loads(raw)

    def test_a_missing_required_field_still_fails(self):
        with pytest.raises(AIResponseInvalid, match="did not match"):
            parse(SemanticAssessment, '{"ai_result": "partial", "deviation": "The "Client" pays."}')

    def test_a_truncated_answer_still_fails(self):
        with pytest.raises(AIResponseInvalid, match="not valid JSON"):
            parse(SemanticAssessment, '{"ai_result": "partial", "deviation": "The cap is')


class TestATranslatedTitleThatRunsLong:
    def test_it_is_shortened_not_rejected(self):
        from apps.ai.schemas import ProseTranslation

        parsed = ProseTranslation.model_validate({"title": "ع" * 300, "deviation": "نص"})
        assert len(parsed.title) == 255 and parsed.deviation == "نص"


class TestAnEmptyStrayKeyInATranslation:
    def test_it_is_dropped_but_a_stray_with_content_still_fails(self):
        from apps.ai.schemas import ProseTranslation

        parsed = ProseTranslation.model_validate({"title": "T", "business_impayment": ""})
        assert parsed.title == "T"
        with pytest.raises(ValueError):
            ProseTranslation.model_validate({"title": "T", "risk_level": "high"})


class TestALongCitationListNeverCostsTheAnswer:
    def test_fifty_five_citations_are_cut_not_refused(self):
        from apps.ai.schemas import MAX_REFERENCES, AdvisorAnswer

        refs = [f"SOURCE {n}" for n in range(1, 56)] + ["SOURCE 1"]
        parsed = AdvisorAnswer.model_validate({"answer": "ملخص الصفحات", "source_references": refs})
        assert parsed.answer == "ملخص الصفحات"
        assert parsed.source_references == [f"SOURCE {n}" for n in range(1, MAX_REFERENCES + 1)]


class TestSlipsSeenOnARealRun:
    """Two answers in a real run failed for a slip, and the analysis was
    marked incomplete: six alternative topics, and a misspelt field name."""

    def parse(self, schema, payload):
        import json

        from apps.ai.providers.base import AIProvider

        return AIProvider.parse_or_raise(schema, json.dumps(payload, ensure_ascii=False))

    def test_six_alternatives_keep_the_first_five(self):
        from apps.ai.schemas import ClauseClassification

        parsed = self.parse(ClauseClassification, {
            "primary": "insurance", "confidence": 0.8,
            "alternatives": ["scope", "governing_law", "liability", "financial", "ip", "schedule"]})
        assert parsed.alternatives == ["scope", "governing_law", "liability", "financial", "ip"]

    def test_a_misspelt_field_is_read_as_the_field_it_meant(self):
        from apps.ai.schemas import SemanticAssessment

        parsed = self.parse(SemanticAssessment, {
            "ai_result": "partial", "confidence": 0.7, "business_imparket": "قد يواجه المستشار تكاليف"})
        assert parsed.business_impact == "قد يواجه المستشار تكاليف"

    def test_an_invented_field_is_still_refused(self):
        import pytest

        from apps.ai.providers.base import AIResponseInvalid
        from apps.ai.schemas import SemanticAssessment

        with pytest.raises(AIResponseInvalid):
            self.parse(SemanticAssessment, {"ai_result": "partial", "confidence": 0.7,
                                            "approve_contract_now": True})
