"""What the bilingual QA pass found, pinned so it stays fixed.

* Permission and workflow refusals reached the reader in English with internal
  codes ("Role 'management' lacks capability 'decide_finding'."), on the
  Arabic site too; so did the upload checks ("Unsupported file type '.exe'").
* The rules engine's line ("12000000 must be at least 25000000") was shown
  verbatim: English, and without thousands separators.
* Missing clauses were titled with the standard's English title on Arabic pages.
* The search page listed contract types in English on the Arabic site.
* English contract text on an Arabic page was laid out right-to-left, so the
  clause number and full stop jumped to the wrong ends.
* Contract values were printed as bare digits (4850000 SAR).
* A contracts page past the last one was a 404 rather than the last page.
"""
from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import translation

from apps.analysis.models import AnalysisRun, Finding, MissingClause
from apps.contracts.templatetags.aji import amount
from apps.documents.storage import validate_upload
from apps.standards.models import ClauseCategory

pytestmark = pytest.mark.django_db


def _flashed(response):
    return [str(m) for m in response.wsgi_request._messages._queued_messages]


def _english(user):
    user.preferred_language = "en"
    user.save(update_fields=["preferred_language"])


class TestRefusalsAreInTheReadersLanguage:
    def test_a_missing_permission_is_arabic_and_names_no_internal_code(self, signed_in, contract):
        response = signed_in.post(reverse("workflow:transition", args=[contract.pk]), {"action": "approve"})
        [message] = _flashed(response)
        assert "صلاحية" in message
        assert "capability" not in message and "final_approval" not in message

    def test_the_same_refusal_in_english(self, signed_in, reviewer, contract):
        _english(reviewer)
        response = signed_in.post(reverse("workflow:transition", args=[contract.pk]), {"action": "approve"})
        assert _flashed(response) == ["Your role (Contract Reviewer) does not have permission for this action."]

    def test_an_empty_comment(self, signed_in, reviewer, contract):
        response = signed_in.post(reverse("workflow:comment", args=[contract.pk]), {"body": "  "})
        assert _flashed(response) == ["لا يمكن أن يكون التعليق فارغاً."]
        _english(reviewer)
        response = signed_in.post(reverse("workflow:comment", args=[contract.pk]), {"body": "  "})
        assert _flashed(response) == ["A comment cannot be empty."]

    def test_an_unknown_action_does_not_echo_the_posted_key(self, signed_in, contract):
        response = signed_in.post(reverse("workflow:transition", args=[contract.pk]), {"action": "<b>x</b>"})
        assert _flashed(response) == ["إجراء غير معروف في مسار المراجعة."]

    @pytest.mark.parametrize("lang, expected", [
        ("ar", "نوع الملف غير مدعوم (.exe). استخدم PDF أو DOC أو DOCX."),
        ("en", "Unsupported file type (.exe). Use PDF, DOC or DOCX."),
    ])
    def test_an_upload_of_the_wrong_type(self, lang, expected):
        with translation.override(lang), pytest.raises(ValidationError) as caught:
            validate_upload(SimpleUploadedFile("tool.exe", b"MZ\x90\x00", "application/octet-stream"))
        assert caught.value.messages == [expected]


@pytest.fixture
def finding(contract):
    run = AnalysisRun.objects.create(contract=contract, version=1, status="succeeded")
    contract.current_analysis = run
    contract.save(update_fields=["current_analysis"])
    return Finding.objects.create(
        analysis=run, contract=contract, risk_level="high", impact=5, probability=4, risk_score=20,
        title="PI cover below the standard", clause_number="18.3",
        original_wording="18.3 The Consultant shall continue to perform the Services during any dispute.",
        system_result="partial",
        system_rule_detail={"operator": "gte", "actual": "22000000", "expected": "25000000",
                            "unit": "currency", "currency": "SAR", "fallback": "20000000",
                            "within_fallback": True, "statement": "22000000 must be at least 25000000 ..."},
    )


class TestTheRuleLine:
    def test_arabic_with_grouped_figures(self, finding):
        with translation.override("ar"):
            line = finding.display_rule_statement
        assert line.startswith("القيمة 22,000,000 SAR، والمطلوب ألا تقل عن 25,000,000 SAR")
        assert "20,000,000 SAR" in line and "must" not in line

    def test_english_with_grouped_figures(self, finding):
        with translation.override("en"):
            assert finding.display_rule_statement == (
                "22,000,000 SAR must be at least 25,000,000 SAR (preferred), "
                "but within the approved fallback of 20,000,000 SAR - requires sign-off")

    def test_percent_days_and_wording(self, finding):
        finding.system_rule_detail = {"operator": "lte", "actual": "0.5", "expected": "10", "unit": "percent"}
        with translation.override("en"):
            assert finding.display_rule_statement == "0.5% must be at most 10%"
        finding.system_rule_detail = {"operator": "gte", "actual": "1095", "expected": "1825", "unit": "days"}
        with translation.override("en"):
            assert finding.display_rule_statement == "1,095 days must be at least 1,825 days"
        finding.system_rule_detail = {"operator": "present", "markers_found": []}
        with translation.override("ar"):
            assert finding.display_rule_statement == "الصياغة المطلوبة غير موجودة"

    def test_an_unknown_shape_falls_back_to_the_stored_sentence(self, finding):
        finding.system_rule_detail = {"statement": "value not found"}
        assert finding.display_rule_statement == "value not found"

    def test_the_findings_tab_shows_it(self, signed_in, finding):
        page = signed_in.get(reverse("analysis:detail", args=[finding.analysis_id]) + "?tab=findings").content.decode()
        assert "22,000,000 SAR" in page and "22000000 must" not in page
        # English clause text on the Arabic page keeps its own direction
        assert 'class="finding__quote" dir="auto">18.3 The Consultant' in page


class TestMissingClauseTitle:
    def test_arabic_page_uses_the_arabic_name(self, contract):
        run = AnalysisRun.objects.create(contract=contract, version=1, status="succeeded")
        category = ClauseCategory.objects.create(code="suspension", name="Suspension", name_ar="التعليق")
        missing = MissingClause.objects.create(analysis=run, contract=contract, category=category,
                                               title="Suspension — AJi global position")
        with translation.override("ar"):
            assert missing.display_title == "التعليق"
        with translation.override("en"):
            assert missing.display_title == "Suspension — AJi global position"


class TestListsAndFigures:
    def test_contract_types_on_the_arabic_search_page(self, signed_in, contract_type):
        contract_type.name_ar = "استشارات تصميم"
        contract_type.save(update_fields=["name_ar"])
        page = signed_in.get(reverse("contracts:search") + "?q=x").content.decode()
        assert ">استشارات تصميم</option>" in page
        assert 'for="sf-type"' in page and 'id="sf-type"' in page

    def test_amounts_are_grouped(self):
        assert amount(4850000) == "4,850,000"
        assert amount("1234.5", 2) == "1,234.50"
        assert amount("") == ""

    def test_a_page_past_the_end_shows_the_last_page(self, signed_in, contract):
        response = signed_in.get(reverse("contracts:list") + "?page=999")
        assert response.status_code == 200
        assert contract.project_name in response.content.decode()


class TestTheReportPage:
    def test_a_contracts_own_report_keeps_the_contract_picker(self, signed_in, run_with_finding):
        contract = run_with_finding.contract
        page = signed_in.get(reverse("reports:detail", args=[contract.pk])).content.decode()
        assert f'<option value="{contract.pk}" selected' in page
        assert "لا توجد عقود" not in page

    def test_standards_are_named_not_coded(self, signed_in, run_with_finding):
        run_with_finding.standards_snapshot = {"delay": {"version": 1, "scope": "global"}}
        run_with_finding.save(update_fields=["standards_snapshot"])
        ClauseCategory.objects.filter(code="delay").update(name_ar="غرامات التأخير")
        page = signed_in.get(reverse("reports:detail", args=[run_with_finding.contract_id])).content.decode()
        assert "غرامات التأخير" in page and "معيار AJi العام" in page
        assert "delay v1 (global)" not in page

    def test_date_and_time_read_left_to_right(self, signed_in, run_with_finding):
        from django.utils import timezone

        run_with_finding.finished_at = timezone.now()
        run_with_finding.save(update_fields=["finished_at"])
        page = signed_in.get(reverse("reports:detail", args=[run_with_finding.contract_id])).content.decode()
        assert f'<bdi dir="ltr">{timezone.localtime(run_with_finding.finished_at):%Y-%m-%d %H:%M}</bdi>' in page


class TestTheContractPicker:
    """An English name in the Arabic page's right-to-left picker lost its
    start ("17 — Qamar…"); the picker now takes the name's own direction."""

    def test_report_picker_follows_an_english_name(self, signed_in, run_with_finding):
        page = signed_in.get(reverse("reports:detail", args=[run_with_finding.contract_id])).content.decode()
        assert '<span style="flex:1 1 320px;min-width:0" dir="ltr">' in page
        assert 'title="' in page and "Riyadh Business District" in page

    def test_an_arabic_name_keeps_the_page_direction(self, signed_in, run_with_finding):
        contract = run_with_finding.contract
        contract.project_name = "مشروع حي الأعمال"
        contract.save(update_fields=["project_name"])
        page = signed_in.get(reverse("reports:detail", args=[contract.pk])).content.decode()
        assert '<span style="flex:1 1 320px;min-width:0">' in page

    def test_comparison_picker_and_reference(self, signed_in, run_with_finding):
        contract = run_with_finding.contract
        page = signed_in.get(reverse("analysis:comparison") + f"?contract={contract.pk}").content.decode()
        assert '<span style="display:block" dir="ltr">' in page
        assert f'<bdi class="mono" style="white-space:nowrap">{contract.reference}</bdi>' in page


class TestTheSanadLogo:
    """The site's logo is Sanad, in the page's language, light and dark artwork both present."""

    def test_arabic_pages(self, signed_in):
        page = signed_in.get(reverse("contracts:dashboard")).content.decode()
        assert "img/sanad-ar-light.png" in page and "img/sanad-ar-dark.png" in page
        assert "img/sanad-mark-light.png" in page and "sanad-en-" not in page
        assert "aji-logo.png" not in page

    def test_english_pages(self, signed_in, reviewer):
        _english(reviewer)
        page = signed_in.get(reverse("contracts:dashboard")).content.decode()
        assert "img/sanad-en-light.png" in page and "img/sanad-en-dark.png" in page
        assert "sanad-ar-" not in page and 'alt="Sanad"' in page

    def test_the_sign_in_page(self, client):
        page = client.get(reverse("accounts:login")).content.decode()
        assert 'class="auth__logo" src="/static/img/sanad-ar-dark.png' in page
        assert "auth__phonelogo" in page


class TestThePdfReport:
    """The exported PDF carries Sanad on its black strip, and is laid out in
    the language its words are in (the English one was printed right to left)."""

    def _html(self, signed_in, contract, monkeypatch):
        import apps.reports.views as report_views
        from apps.accounts import permissions as perms

        seen = {}
        monkeypatch.setattr(report_views, "render_pdf", lambda html: seen.setdefault("html", html) and b"%PDF-1.7")
        monkeypatch.setattr(perms, "require_capability", lambda user, cap: None)
        response = signed_in.get(reverse("reports:pdf", args=[contract.pk]))
        assert response.status_code == 200
        return seen["html"]

    def test_arabic(self, signed_in, run_with_finding, monkeypatch):
        html = self._html(signed_in, run_with_finding.contract, monkeypatch)
        assert '<html lang="ar" dir="rtl"' in html
        assert 'class="brandbar__logo" src="data:image/png;base64,' in html and 'alt="سند"' in html

    def test_english(self, signed_in, reviewer, run_with_finding, monkeypatch):
        _english(reviewer)
        html = self._html(signed_in, run_with_finding.contract, monkeypatch)
        assert '<html lang="en" dir="ltr"' in html
        assert 'class="brandbar__logo" src="data:image/png;base64,' in html and 'alt="Sanad"' in html


class TestSanadInPlaceOfAji:
    def test_the_report_masthead_is_sanad(self, signed_in, run_with_finding):
        page = signed_in.get(reverse("reports:detail", args=[run_with_finding.contract_id])).content.decode()
        masthead = page.split('class="report__logo"', 1)[1][:900]
        assert "img/sanad-ar-light.png" in masthead and "img/sanad-ar-dark.png" in masthead
        assert 'alt="AJi"' not in page and "data:image/png" not in page

    def test_the_pdf_has_one_logo_on_its_strip(self, signed_in, run_with_finding, monkeypatch):
        html = TestThePdfReport()._html(signed_in, run_with_finding.contract, monkeypatch)
        assert html.count("<img") == 1 and 'class="brandbar__logo"' in html
        assert 'class="report__logo"' not in html

    def test_phones_have_the_mark_in_the_top_bar(self, signed_in):
        page = signed_in.get(reverse("contracts:dashboard")).content.decode()
        bar = page.split('class="topbar__brand"', 1)[1][:600]
        assert "img/sanad-mark-light.png" in bar and "img/sanad-mark-dark.png" in bar


class TestTheSiteTypefaces:
    """The site is set in its original two faces again: Noto Kufi Arabic for
    the text in both languages, Plus Jakarta Sans for figures and labels. The
    AJi brand faces stay in the PDF and Word exports only."""

    def test_the_stacks(self):
        from pathlib import Path

        from django.conf import settings

        css = Path(settings.BASE_DIR, "static/css/aji.css").read_text(encoding="utf-8")
        assert 'font-family: "Noto Kufi Arabic", "Plus Jakarta Sans", system-ui, sans-serif;' in css
        assert '.num  { font-family: "Plus Jakarta Sans", "Noto Kufi Arabic", sans-serif; }' in css
        assert '"Draft B"' not in css and '"IBM Plex Sans Arabic"' not in css

    def test_every_page_loads_them_in_both_languages(self, signed_in, reviewer):
        for _ in ("ar", "en"):
            page = signed_in.get(reverse("contracts:dashboard")).content.decode()
            assert "family=Noto+Kufi+Arabic" in page and "family=Plus+Jakarta+Sans" in page
            assert "draft-b-" not in page and "ibm-plex-sans-arabic-" not in page
            _english(reviewer)


class TestTheExportTypefaces:
    """The PDF is set in the site's own two faces again -- Noto Kufi Arabic
    for the text, Plus Jakarta Sans for figures -- in both languages, and no
    brand face is inlined any more."""

    def test_both_pdfs_use_the_site_faces(self, signed_in, reviewer, run_with_finding, monkeypatch):
        for _ in ("ar", "en"):
            html = TestThePdfReport()._html(signed_in, run_with_finding.contract, monkeypatch)
            # Noto Kufi inlined from static/fonts (a real TrueType in the PDF,
            # no network); Plus Jakarta Sans from Google Fonts.
            assert '@font-face { font-family: "Noto Kufi Arabic"; font-weight: 400' in html
            assert "data:font/ttf;base64," in html and "family=Plus+Jakarta+Sans" in html
            assert "family=Noto+Kufi+Arabic" not in html
            assert 'font-family: "Noto Kufi Arabic", "Plus Jakarta Sans", sans-serif;' in html
            assert "data:font/otf" not in html and "Draft B" not in html and "IBM Plex Sans Arabic" not in html
            _english(reviewer)

    def test_the_word_redline_names_noto_kufi(self):
        from apps.reports import redline

        assert redline.LATIN_FONT == redline.ARABIC_FONT == "Noto Kufi Arabic"
        assert [f for _, _, f in redline.EMBEDDED_FONTS] == ["noto-kufi-arabic-400.ttf", "noto-kufi-arabic-700.ttf"]


class TestRecommendedActionsAreNumberedWithoutGaps:
    """Counted over every finding, the list began at 2 and skipped each
    finding that had no action (the report page and the PDF alike)."""

    def test_one_two_three(self, signed_in, finding):
        import re

        run = finding.analysis
        for score, action in ((19, ""), (18, "Add a cap on delay damages."), (17, ""), (16, "Name the arbitral seat.")):
            Finding.objects.create(analysis=run, contract=finding.contract, risk_level="medium",
                                   impact=4, probability=4, risk_score=score, title=f"Finding {score}",
                                   clause_number=f"{score}.1", recommended_action=action)
        finding.recommended_action = "Raise the PI cover."
        finding.save(update_fields=["recommended_action"])
        html = signed_in.get(reverse("reports:detail", args=[finding.contract.pk])).content.decode()
        panel = html.split('id="card-actions"', 1)[1].split("</ol>", 1)[0]
        numbers = re.findall(r'class="steps__n num">(\d+)<', panel)
        assert numbers == ["1", "2", "3"]
