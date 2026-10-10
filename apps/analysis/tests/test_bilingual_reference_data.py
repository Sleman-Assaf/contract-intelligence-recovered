"""Reference data follows the reader's language, like the chrome around it.

Clause categories and contract types are stored bilingually and shown through
`display_name`, which picks by the active language. A `values()` aggregation
cannot use that property -- it never loads a model -- so each one resolves the
label itself, and one of them resolved it as "Arabic if there is any Arabic".

The result was an English risk matrix listing "الملكية الفكرية وملكية
التصاميم" and "أوامر التغيير" under a heading that read "Where the risk
concentrates". Nothing was missing from the catalogue; the page simply asked
for the wrong column.

Worth its own test because the string-coverage tests cannot see it. They check
that every translatable string has an English entry, and this is data rather
than a string -- it has no msgid to be missing.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.analysis.models import Finding
from apps.standards.models import ClauseCategory

pytestmark = pytest.mark.django_db

ENGLISH = "Intellectual property"
ARABIC = "الملكية الفكرية"


@pytest.fixture
def finding_in_a_bilingual_category(run_with_finding, contract):
    category = ClauseCategory.objects.create(
        code="ip", name=ENGLISH, name_ar=ARABIC)
    Finding.objects.create(
        analysis=run_with_finding, contract=contract, category=category,
        risk_level="high", impact=5, probability=4, risk_score=20,
        title="Design ownership is not assigned",
        system_result="non_compliant", ai_result="non_compliant",
        verification_status="verified",
    )
    return category


def _matrix(client, user, language):
    user.preferred_language = language
    user.save(update_fields=["preferred_language"])
    client.force_login(user)
    return client.get(reverse("analysis:risk_matrix")).content.decode()


class TestTheRiskMatrix:
    def test_an_english_reader_sees_the_english_category(
            self, client, reviewer, finding_in_a_bilingual_category):
        body = _matrix(client, reviewer, "en")
        assert ENGLISH in body
        assert ARABIC not in body

    def test_an_arabic_reader_sees_the_arabic_one(
            self, client, reviewer, finding_in_a_bilingual_category):
        body = _matrix(client, reviewer, "ar")
        assert ARABIC in body

    def test_a_category_with_no_arabic_still_shows_its_name(
            self, client, reviewer, run_with_finding, contract):
        """Falling back to the English name: a blank label is worse than one
        in the wrong language."""
        category = ClauseCategory.objects.create(
            code="novation", name="Novation", name_ar="")
        Finding.objects.create(
            analysis=run_with_finding, contract=contract, category=category,
            risk_level="high", impact=4, probability=4, risk_score=16,
            title="Assignment is unrestricted",
            system_result="non_compliant", ai_result="non_compliant",
            verification_status="verified",
        )
        assert "Novation" in _matrix(client, reviewer, "ar")


class TestTheDashboard:
    """The same aggregation shape, for contract types rather than categories."""

    def test_an_english_reader_sees_the_english_type(
            self, client, reviewer, contract):
        contract.contract_type.name = "Design Consultancy"
        contract.contract_type.name_ar = "استشارات تصميم"
        contract.contract_type.save(update_fields=["name", "name_ar"])

        reviewer.preferred_language = "en"
        reviewer.save(update_fields=["preferred_language"])
        client.force_login(reviewer)
        body = client.get(reverse("contracts:dashboard")).content.decode()

        assert "Design Consultancy" in body
        assert "استشارات تصميم" not in body


def test_display_name_itself_picks_by_language():
    """The property the aggregations have to imitate."""
    from django.utils import translation

    category = ClauseCategory(code="x", name="Scope of work", name_ar="نطاق العمل")
    with translation.override("en"):
        assert category.display_name == "Scope of work"
    with translation.override("ar"):
        assert category.display_name == "نطاق العمل"
