"""Arabic that leaked onto the English pages -- and what must stay Arabic.

* The risk matrix printed the stored (Arabic) business impact and
  recommendation instead of the reader's language.
* Recommended actions were never translated: they were treated as approved
  wording, which on a finding they are not -- that is `fallback_wording`.
  Approved wording is still never translated.
* The model sometimes wrote its suggested replacement wording in Arabic for an
  English clause. It may be pasted into the contract, so it is withheld, and
  dropped when the analysis stores it.
"""
from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import translation

from apps.ai.providers.base import AICallResult
from apps.ai.schemas import ProseTranslation
from apps.analysis.models import AnalysisRun, Finding
from apps.analysis.services import _in_clause_language

pytestmark = pytest.mark.django_db

EN_CLAUSE = "The Consultant's liability shall not be limited in amount."
AR_WORDING = "يجب أن تقتصر مسؤولية الاستشاري على قيمة العقد."


@pytest.fixture
def finding(contract):
    run = AnalysisRun.objects.create(contract=contract, version=1, status="succeeded")
    contract.current_analysis = run
    contract.save(update_fields=["current_analysis"])
    return Finding.objects.create(
        analysis=run, contract=contract, risk_level="high", title="غياب سقف المسؤولية",
        deviation="غياب سقف المسؤولية", business_impact="أثر مالي غير محدود",
        recommended_action="التفاوض على سقف يساوي قيمة العقد", original_wording=EN_CLAUSE,
        impact=5, probability=5, risk_score=25,
        wording_translations={"ar": "لا تكون مسؤولية الاستشاري محدودة بمبلغ.", "en": EN_CLAUSE},
        prose={"ar": {"title": "غياب سقف المسؤولية", "business_impact": "أثر مالي غير محدود",
                      "recommended_action": "التفاوض على سقف يساوي قيمة العقد"},
               "en": {"title": "No liability cap", "business_impact": "Unlimited financial exposure",
                      "recommended_action": "التفاوض على سقف يساوي قيمة العقد"}},
    )


class _Translator:
    name = "stub"

    def __init__(self):
        self.asked = []

    def translate_prose(self, *, fields, target_language):
        self.asked.append((sorted(fields), target_language))
        return AICallResult(parsed=ProseTranslation(
            title="T", deviation="D", explanation="E", business_impact="B",
            recommended_action="Negotiate a cap equal to the contract value"),
            provider="stub", model="m", request_type="translate_prose", latency_ms=1)

    def translate_clause(self, **kwargs):
        raise AssertionError("not asked in these tests")


@pytest.fixture
def translator(monkeypatch):
    from apps.ai import services as ai_services

    stub = _Translator()
    monkeypatch.setattr(ai_services, "get_ai_provider", lambda: stub)
    monkeypatch.setattr(ai_services.ai_service, "_log", lambda *a, **k: None)
    return stub


class TestRecommendedActionsAreTranslated:
    def test_an_old_translation_gets_its_action_in_english(self, finding, translator):
        from apps.ai.services import ai_service

        added = ai_service.translate_finding(finding=finding)
        assert "action:en" in added
        assert translator.asked == [(["recommended_action"], "en")]     # only the action, only English
        finding.refresh_from_db()
        assert finding.prose["en"]["recommended_action"].startswith("Negotiate")
        assert finding.prose["en"]["title"] == "No liability cap"        # the rest untouched
        with translation.override("en"):
            assert finding.display_recommended_action.startswith("Negotiate")

    def test_approved_wording_is_never_translated(self, finding, translator):
        from apps.ai.services import ai_service

        Finding.objects.filter(pk=finding.pk).update(fallback_wording=finding.recommended_action)
        finding.refresh_from_db()
        assert ai_service.translate_finding(finding=finding) == []
        assert translator.asked == []

    def test_a_new_translation_carries_the_action(self, finding, translator):
        from apps.ai.services import ai_service

        finding.prose = {"ar": finding.prose["ar"]}
        finding.save(update_fields=["prose"])
        ai_service.translate_finding(finding=finding)
        assert "recommended_action" in translator.asked[0][0]
        assert finding.prose["en"]["recommended_action"].startswith("Negotiate")


class TestSuggestedWordingKeepsTheClausesLanguage:
    def test_the_analysis_drops_wording_in_another_language(self):
        assert _in_clause_language(AR_WORDING, EN_CLAUSE) == ""
        assert _in_clause_language("Liability is capped at the fees.", EN_CLAUSE) == "Liability is capped at the fees."
        assert _in_clause_language(AR_WORDING, "تكون مسؤولية الاستشاري غير محدودة.") == AR_WORDING

    def test_existing_wording_in_another_language_is_withheld(self, finding):
        finding.fallback_wording = AR_WORDING
        assert finding.usable_fallback_wording == ""
        finding.fallback_wording = "Liability is capped at the fees."
        assert finding.usable_fallback_wording == "Liability is capped at the fees."

    def test_wording_legal_approved_is_always_shown(self, finding, contract):
        from apps.standards.models import ClauseCategory, Standard, StandardVersion

        category = ClauseCategory.objects.create(code="liability", name="Liability")
        standard = Standard.objects.create(category=category, title="Liability cap")
        version = StandardVersion.objects.create(standard=standard, version=1, preferred_position="x",
                                                 approved_fallback_wording=AR_WORDING)
        finding.standard_version = version
        finding.fallback_wording = AR_WORDING
        assert finding.usable_fallback_wording == AR_WORDING


class TestTheRiskMatrixSpeaksTheReadersLanguage:
    def test_business_impact_and_recommendation_in_english(self, client, reviewer, finding):
        reviewer.preferred_language = "en"
        reviewer.save(update_fields=["preferred_language"])
        client.force_login(reviewer)
        body = client.get(reverse("analysis:risk_matrix")).content.decode()
        assert "Unlimited financial exposure" in body and "أثر مالي غير محدود" not in body


class TestMixedTextIsJudgedByItsMainLanguage:
    def test_an_arabic_action_quoting_an_english_term_is_still_arabic(self, finding, translator):
        from apps.ai.services import ai_service
        from apps.documents.language import detect_language, mostly

        mixed = "مراجعة البند لإضافة تأمين التعويض المهني وتحديد المبالغ أو الإشارة إلى (Special Conditions)"
        assert detect_language(mixed) == "bi" and mostly(mixed) == "ar"
        finding.prose["en"]["recommended_action"] = mixed
        finding.save(update_fields=["prose"])
        assert "action:en" in ai_service.translate_finding(finding=finding)
