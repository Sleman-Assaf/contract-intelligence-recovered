"""The analysis page's headline verdict names the real overall risk.

`overall_risk` has no choices, so the template's `get_overall_risk_display`
did not exist and every contract's headline read "غير محدد"."""
from django.utils import translation

from apps.analysis.models import AnalysisRun


def test_the_label_is_the_badge_wording():
    with translation.override("ar"):
        assert AnalysisRun(overall_risk="high").overall_risk_label == "مخاطر مرتفعة"
        assert AnalysisRun(overall_risk="medium").overall_risk_label == "مخاطر متوسطة"
        assert AnalysisRun(overall_risk="").overall_risk_label == "غير محدد"
    with translation.override("en"):
        assert AnalysisRun(overall_risk="high").overall_risk_label == "High risk"
