"""The analysis spends its model time on judgement, not on repeating itself.

The assessment is written once, in AI_OUTPUT_LANGUAGE; the other language and
the clause reading aid are filled afterwards by translate_findings. A title on
its own line is not a clause. A stray `recommended_action_en` is a copy, not a
reason to lose the clause.
"""
from __future__ import annotations

import pytest

from apps.ai import prompts
from apps.ai.providers.base import AICallResult
from apps.ai.schemas import ClauseTranslation, ProseTranslation, SemanticAssessment
from apps.analysis import services
from apps.analysis.clause_detection import TextUnit, group_into_clauses
from apps.analysis.tests.test_pipeline_end_to_end import analysed, stub, uploader  # noqa: F401  (fixtures)


def _assessment(**extra):
    return {"ai_result": "partial", "deviation": "Cap is 20%.", "explanation": "Above 10%.",
            "business_impact": "Exposure.", "recommended_action": "Negotiate.",
            "confidence": 0.8, **extra}


class TestThePromptAsksForOneLanguage:
    def test_no_translations_are_requested(self):
        text = prompts.assess_clause(clause_text="x", standard_position="y", acceptable_range="",
                                     red_flags="", context_clauses=[], contract_context={},
                                     language="en")
        assert '"prose"' not in text and '"clause_translation"' not in text
        assert "one or two plain sentences" in text


class TestLanguageCopiesAreDropped:
    def test_a_suffixed_copy_of_a_field_is_ignored(self):
        parsed = SemanticAssessment.model_validate(
            _assessment(recommended_action_en="Negotiate.", deviation_ar="..."))
        assert parsed.recommended_action == "Negotiate."

    def test_any_other_unknown_key_still_fails(self):
        with pytest.raises(ValueError):
            SemanticAssessment.model_validate(_assessment(risk_level="high"))
        with pytest.raises(ValueError):
            SemanticAssessment.model_validate(_assessment(recommended_action_fr="x"))


class TestTheFindingIsStoredInTheLanguageItWasWritten:
    def test_prose_is_keyed_by_the_output_language(self, settings):
        settings.AI_OUTPUT_LANGUAGE = "en"
        prose = services._prose_of(SemanticAssessment.model_validate(_assessment()))
        assert list(prose) == ["en"]
        assert prose["en"]["title"] == "Cap is 20%." and prose["en"]["explanation"] == "Above 10%."

    def test_nothing_to_store_without_an_assessment(self):
        assert services._prose_of(None) == {}


class TestABareTitleIsNotAClause:
    def _group(self, *texts):
        return group_into_clauses(TextUnit(id=i, text=t, page_number=1) for i, t in enumerate(texts))

    def test_the_title_folds_into_its_first_sub_clause(self):
        clauses = self._group("Article 6 – Contract Price and Payment",
                              "6.1 The Client shall pay the Contract Price within 30 days.",
                              "6.2 Payments are made in SAR.")
        assert [c.clause_number for c in clauses] == ["6.1", "6.2"]
        first = clauses[0]
        assert first.text.startswith("Article 6 – Contract Price and Payment\n6.1")
        assert first.block_ids == [0, 1] and first.heading == "Article 6 – Contract Price and Payment"
        assert [c.ordinal for c in clauses] == [0, 1]

    def test_a_title_followed_by_something_else_stays(self):
        clauses = self._group("Article 6 – Payment", "7.1 The Consultant shall insure.")
        assert [c.clause_number for c in clauses] == ["6", "7.1"]

    def test_a_short_sentence_is_not_a_title(self):
        clauses = self._group("6.4 No interest shall accrue on late payments.", "6.5 The price is fixed.")
        assert [c.clause_number for c in clauses] == ["6.4", "6.5"]


class _Finding:
    """Just what translate_finding reads and writes."""

    def __init__(self, clause):
        self.prose = {"en": {"title": "Cap is 20%.", "deviation": "Cap is 20%."}}
        self.wording_translations = {}
        self.original_wording = clause
        self.recommended_action = "Negotiate."
        self.contract_id = ""
        self.saved = None
        for name in prompts.TRANSLATABLE_PROSE:
            setattr(self, name, self.prose["en"].get(name, ""))

    def prose_in(self, name):
        return self.prose["en"].get(name, "")

    def save(self, update_fields):
        self.saved = update_fields


class _Translator:
    name = "stub"

    def __init__(self):
        self.calls = []

    def _result(self, parsed, kind):
        return AICallResult(parsed=parsed, provider="stub", model="stub", request_type=kind, latency_ms=1)

    def translate_prose(self, *, fields, target_language):
        self.calls.append(("prose", target_language))
        return self._result(ProseTranslation(title="السقف 20%", deviation="السقف 20%"), "translate_prose")

    def translate_clause(self, *, clause_text, target_language):
        self.calls.append(("clause", target_language))
        return self._result(ClauseTranslation(text="نص البند"), "translate_clause")


class TestTranslationAfterwards:
    @pytest.fixture
    def translator(self, monkeypatch):
        from apps.ai import services as ai_services

        stub = _Translator()
        monkeypatch.setattr(ai_services, "get_ai_provider", lambda: stub)
        monkeypatch.setattr(ai_services.ai_service, "_log", lambda *a, **k: None)
        return stub

    def test_only_the_missing_language_is_written(self, translator):
        from apps.ai.services import ai_service

        finding = _Finding("The Consultant's total liability shall not exceed twenty per cent of the price.")
        ai_service.translate_finding(finding=finding)
        # the prose once, into Arabic; the clause once, into Arabic -- never English into English
        assert translator.calls == [("prose", "ar"), ("clause", "ar")]
        assert finding.prose["ar"]["title"] == "السقف 20%"
        assert finding.wording_translations["en"] == finding.original_wording
        assert finding.saved == ["prose", "wording_translations"]


@pytest.mark.django_db
class TestTheTranslationTask:
    @pytest.fixture(autouse=True)
    def _pipeline(self, analysed):             # noqa: F811 - fixture from the pipeline tests
        self.contract, _document, self.run, _outcome = analysed

    def test_one_failed_finding_does_not_stop_the_rest(self, monkeypatch):
        from apps.ai.services import ai_service
        from apps.analysis.tasks import translate_findings

        seen = []

        def translate(*, finding, analysis_run=None):
            seen.append(finding.pk)
            if len(seen) == 1:
                raise RuntimeError("model down")
            return ["ar"]

        monkeypatch.setattr(ai_service, "translate_finding", translate)
        total = self.run.findings.count()
        result = translate_findings(str(self.run.pk))
        assert total > 1 and len(seen) == total
        assert result == {"status": "done", "run": str(self.run.pk), "translated": total - 1, "failed": 1}

    def test_the_analysis_task_queues_it_after_commit(self, monkeypatch, django_capture_on_commit_callbacks):
        from apps.analysis import tasks

        queued = []
        monkeypatch.setattr(tasks.translate_findings, "delay", queued.append)
        with django_capture_on_commit_callbacks(execute=True):
            result = tasks.analyse_contract(str(self.contract.pk))
        assert result["findings"] and queued == [result["run"]]
