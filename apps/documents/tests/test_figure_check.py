"""The OCR + vision figure check, settled by rules (figure_check.py).

Built from a test on six real scanned contracts: vision kept a fee table's
rows whole where OCR broke them, but wrote confident guesses where a stamp
or a signature covered a digit. Vision wins only where a rule backs it.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from apps.documents import figure_check as fc
from apps.documents.key_facts import analyse_document
from apps.documents.models import KeyFact, Page

from .test_key_facts import CONDITIONS, PARTICULARS, contract, entity, make_document, reviewer  # noqa: F401

pytestmark = pytest.mark.django_db


def reading(text: str):
    return SimpleNamespace(ok=True, text=text, error="")


class TestFigures:
    def test_european_and_english_forms(self):
        assert fc.parse_figure("213.435,00")[0] == 213435.0
        assert fc.parse_figure("1,234.56")[0] == 1234.56
        assert fc.parse_figure("382,50")[0] == 382.5
        assert set(fc.parse_figure("8.740")[1]) == {8.74, 8740.0}

    def test_letterhead_and_stamp_lines_are_never_compared(self):
        figures = fc.figures_in(["Registration No.4504", "T:+962 6 585 7162", "Amman 11191-Jordan",
                                 "Page 5", "Total 645.585,38"])
        assert [f.raw for f in figures] == ["645.585,38"]

    def test_a_row_that_adds_up(self):
        row = fc.figures_in(["Expert 5 Energy Processes Expert 558 382,50 213.435,00"])
        assert {row[i].raw for i in fc.row_arithmetic(row)} == {"558", "382,50", "213.435,00"}

    def test_a_percentage_rounded_to_the_unit(self):
        row = fc.figures_in(["Bond 10% of 87,792.05 = 8,780"])
        assert {row[i].raw for i in fc.row_arithmetic(row)} == {"10", "87,792.05", "8,780"}

    def test_a_row_that_does_not(self):
        row = fc.figures_in(["Expert 5 Energy Processes Expert 558 382,50 213.436,00"])
        assert fc.row_arithmetic(row) == set()

    def test_a_column_that_adds_up_to_its_total(self):
        figures = fc.figures_in(["Expert 7 97.990,38", "Expert 8 156.000,00", "253.990,38"])
        assert {figures[i].raw for i in fc.column_totals(figures)} == {"97.990,38", "156.000,00", "253.990,38"}


OCR_TABLE = [
    "Expert 9.b Wastewater Management and Operation 30,00 16.900,00",
    "Experts national experience ESSALAIMEH nee sieened",
    "Registration No.4509",
]
VISION_TABLE = (
    "Expert 9.b Wastewater Management and Operation 130 130,00 16.900,00\n"
    "Expert 5 Energy Processes Expert Sa'ed ESSALAIMEH 558 382,50 213.435,00\n"
    "Expert 11 Hydraulic Expert 412 999,00\n"
    "Registration No.4504"
)


class TestCheckPage:
    @pytest.fixture
    def document(self, contract):  # noqa: F811
        return make_document(contract, {9: OCR_TABLE}, scanned=True)

    def run_page(self, document, vision_text, third=None):
        page = Page.objects.get(document=document, page_number=9)
        readings = [reading(vision_text)] + ([reading(third)] if third is not None else [])
        with mock.patch.object(fc, "_page_image", return_value=b"png"), \
                mock.patch.object(fc, "_read", side_effect=readings), \
                mock.patch("apps.documents.extractors.crop_region", return_value=b"png"):
            return fc.check_page(document, page, set(), {"calls": 0, "tiebreaks": 5})

    def test_vision_wins_where_the_row_adds_up(self, document):
        result = self.run_page(document, VISION_TABLE)
        block = document.blocks.get(block_index=0)
        assert "130,00 16.900,00" in block.usable_text
        assert block.original_source_text == OCR_TABLE[0]           # the evidence is untouched
        assert block.corrections[-1] == {"kind": "vision_figure", "from": "30,00", "to": "130,00",
                                         "note": "row_math"}
        assert result.fixed == [{"from": "30,00", "to": "130,00", "why": "row_math"}]

    def test_a_line_ocr_lost_is_added_only_when_a_rule_backs_it(self, document):
        result = self.run_page(document, VISION_TABLE)
        added = [a["line"] for a in result.added]
        assert any("213.435,00" in line for line in added)          # 558 x 382,50 = 213.435,00
        assert not any("999,00" in line for line in added)          # nothing backs it
        text = "\n".join(document.blocks.values_list("corrected_text", flat=True))
        assert "Expert 5 Energy Processes Expert" in text
        # Placed with the block it belongs to, not at random.
        assert "213.435,00" in document.blocks.get(block_index=1).usable_text

    def test_the_stamp_guess_is_ignored(self, document):
        result = self.run_page(document, VISION_TABLE)
        assert "4504" not in str(result.fixed) + str(result.added) + str(result.unsettled)

    def test_a_disagreement_no_rule_settles_goes_to_a_third_reading(self, document):
        result = self.run_page(document, "Expert 9.b Wastewater Management and Operation 30,00 16.800,00",
                               third="30,00 16.800,00")
        assert result.fixed == [{"from": "16.900,00", "to": "16.800,00", "why": "third_reading"}]

    def test_when_nothing_settles_it_ocr_stands_unverified(self, document):
        result = self.run_page(document, "Expert 9.b Wastewater Management and Operation 30,00 16.800,00",
                               third="unreadable")
        assert result.unsettled == [{"ocr": "16.900,00", "vision": "16.800,00"}]
        assert "16.900,00" in document.blocks.get(block_index=0).usable_text


class TestSettleFacts:
    def test_the_particular_conditions_win_and_the_conflict_is_reported(self, contract):  # noqa: F811
        particulars = ["الشروط الخاصة / بيانات العقد", *PARTICULARS]
        conditions = ["الشروط العامة", *CONDITIONS]
        document = make_document(contract, {1: particulars, 2: conditions}, scanned=True)
        analyse_document(document)
        warnings = fc.settle_facts(document)
        fact = document.key_facts.get(name=KeyFact.Name.DELAY_DAMAGES_RATE)
        assert fact.status == KeyFact.Status.SETTLED and fact.value == "170"
        assert "170 (p1)" in fact.note and "125 (p2)" in fact.note
        assert warnings[0]["kind"] == "figure_conflict" and warnings[0]["why"] == "specific"
        assert fact.confirmed_by is None                            # no one was asked

    def test_a_value_vision_read_too_is_marked_checked(self, contract):  # noqa: F811
        document = make_document(contract, {1: PARTICULARS}, scanned=True)
        analyse_document(document)
        page = Page.objects.get(document=document, page_number=1)
        page.quality_signals = {"figure_check": {"vision_keys": ["180", "87792.05", "170"]}}
        page.save()
        fc.settle_facts(document)
        value = document.key_facts.get(name=KeyFact.Name.CONTRACT_VALUE)
        assert value.value == "87792.05" and value.vision_checked
        assert value.note == "checked by OCR and vision"

    def test_without_the_check_it_is_settled_but_marked_unverified(self, contract):  # noqa: F811
        document = make_document(contract, {1: PARTICULARS}, scanned=True)
        analyse_document(document)
        fc.settle_facts(document)
        duration = document.key_facts.get(name=KeyFact.Name.DURATION)
        assert duration.status == KeyFact.Status.SETTLED and not duration.vision_checked
        assert "not verified" in duration.note


class TestTheFlow:
    def test_auto_mode_analyses_without_asking(self, contract, settings):  # noqa: F811
        from apps.contracts.models import ContractStatus
        from apps.documents.tasks import extract_source_file

        settings.KEY_FACTS_CONFIRMATION = "auto"
        document = make_document(contract, {1: PARTICULARS, 2: CONDITIONS}, scanned=True)
        outcome = SimpleNamespace(document=document, pages_native=0, pages_ocr=2, blocks_created=5,
                                  regions_sent_to_vision=0, needs_human_review=True, conflicts=0,
                                  pages_failed=0)
        with mock.patch("apps.documents.tasks.run_extraction", return_value=outcome), \
                mock.patch.object(fc, "check_figures", return_value=fc.FigureCheck(skipped="disabled")), \
                mock.patch("apps.documents.queueing.queue_analysis") as queued:
            result = extract_source_file.run(str(document.source_file.pk))
        contract.refresh_from_db()
        assert contract.status != ContractStatus.NEEDS_SOURCE_REVIEW
        assert result.get("analysis") == "queued"
        queued.assert_called_once()
        assert all(f.status in (KeyFact.Status.SETTLED, KeyFact.Status.NOT_FOUND)
                   for f in document.key_facts.all())
        assert any(w["kind"] == "figure_conflict" for w in document.warnings)

    def test_the_contract_page_shows_settled_facts(self, client, reviewer, contract):  # noqa: F811
        from django.urls import reverse

        document = make_document(contract, {1: PARTICULARS}, scanned=True)
        analyse_document(document)
        fc.settle_facts(document)
        reviewer.preferred_language = "en"
        reviewer.save(update_fields=["preferred_language"])
        client.force_login(reviewer)
        body = client.get(reverse("contracts:detail", args=[contract.pk])).content.decode()
        assert "Settled by the system" in body
        assert "Unverified" in body


def test_small_figures_vision_read_are_recorded_for_the_facts(contract):  # noqa: F811
    document = make_document(contract, {9: ["Payment within 30 days", "Bond 10%"]}, scanned=True)
    page = Page.objects.get(document=document, page_number=9)
    with mock.patch.object(fc, "_page_image", return_value=b"png"), \
            mock.patch.object(fc, "_read", return_value=reading("Payment within 30 days\nBond 10%")):
        fc.check_page(document, page, set(), {"calls": 0, "tiebreaks": 0})
    page.refresh_from_db()
    assert {"30", "10"} <= set(page.quality_signals["figure_check"]["vision_keys"])


class TestAskingForWhatIsNotClear:
    """What the site cannot read clearly -- handwritten, or unsure and not
    confirmed by vision -- the user types in; nothing else is asked."""

    def test_a_required_value_nowhere_to_be_read_is_asked_for(self, contract):  # noqa: F811
        document = make_document(contract, {1: ["مدة العقد: 180 يوما تقويمياً"]}, scanned=True)
        analyse_document(document)
        fc.settle_facts(document)
        value = document.key_facts.get(name=KeyFact.Name.CONTRACT_VALUE)
        assert value.status == KeyFact.Status.NEEDS_INPUT and value.value == ""
        assert document.key_facts.get(name=KeyFact.Name.DURATION).status == KeyFact.Status.SETTLED
        # Not required, not stated: simply not found, not asked.
        bond = document.key_facts.get(name=KeyFact.Name.PERFORMANCE_BOND)
        assert bond.status == KeyFact.Status.NOT_FOUND

    def test_a_clean_digital_file_is_never_asked(self, contract):  # noqa: F811
        document = make_document(contract, {1: ["مدة العقد: 180 يوما تقويمياً"]})
        analyse_document(document)
        fc.settle_facts(document)
        assert fc.needs_input(document) == 0

    def test_an_unsure_reading_vision_did_not_confirm_is_asked_for(self, contract):  # noqa: F811
        document = make_document(contract, {1: PARTICULARS}, scanned=True)
        document.blocks.filter(original_source_text__contains="مدة العقد").update(
            word_confidences=[["180", 41.0]])
        analyse_document(document)
        fc.settle_facts(document)
        duration = document.key_facts.get(name=KeyFact.Name.DURATION)
        assert duration.status == KeyFact.Status.NEEDS_INPUT
        assert duration.value == ""                                 # typed, not waved through
        assert duration.candidates[0]["value"] == "180"             # still offered with its image

    def test_the_same_unsure_reading_confirmed_by_vision_is_settled(self, contract):  # noqa: F811
        document = make_document(contract, {1: PARTICULARS}, scanned=True)
        document.blocks.filter(original_source_text__contains="مدة العقد").update(
            word_confidences=[["180", 41.0]])
        analyse_document(document)
        page = Page.objects.get(document=document, page_number=1)
        page.quality_signals = {"figure_check": {"vision_keys": ["180"]}}
        page.save()
        fc.settle_facts(document)
        duration = document.key_facts.get(name=KeyFact.Name.DURATION)
        assert duration.status == KeyFact.Status.SETTLED and duration.vision_checked

    def test_the_file_waits_for_the_user_and_is_not_analysed(self, contract, settings):  # noqa: F811
        from apps.contracts.models import ContractStatus
        from apps.documents.tasks import extract_source_file

        settings.KEY_FACTS_CONFIRMATION = "auto"
        document = make_document(contract, {1: ["مدة العقد: 180 يوما تقويمياً"]}, scanned=True)
        outcome = SimpleNamespace(document=document, pages_native=0, pages_ocr=1, blocks_created=1,
                                  regions_sent_to_vision=0, needs_human_review=False, conflicts=0,
                                  pages_failed=0)
        with mock.patch("apps.documents.tasks.run_extraction", return_value=outcome), \
                mock.patch.object(fc, "check_figures", return_value=fc.FigureCheck(skipped="disabled")), \
                mock.patch("apps.documents.queueing.queue_analysis") as queued:
            result = extract_source_file.run(str(document.source_file.pk))
        contract.refresh_from_db()
        assert contract.status == ContractStatus.NEEDS_SOURCE_REVIEW
        assert result["facts_to_confirm"] == 1
        queued.assert_not_called()


class TestTheInputScreen:
    @pytest.fixture
    def document(self, contract):  # noqa: F811
        document = make_document(contract, {1: ["مدة العقد: 180 يوما تقويمياً"]}, scanned=True)
        analyse_document(document)
        fc.settle_facts(document)
        return document

    def test_it_asks_only_for_what_is_not_clear(self, client, reviewer, contract, document):  # noqa: F811
        from django.urls import reverse

        reviewer.preferred_language = "en"
        reviewer.save(update_fields=["preferred_language"])
        client.force_login(reviewer)
        body = client.get(reverse("documents:confirm", args=[contract.pk])).content.decode()
        ask = body.index("Values the system could not read clearly")
        folded = body.index("settled by the system or confirmed by a reviewer")
        assert ask < body.index('id="fact-contract_value"') < folded < body.index('id="fact-duration"')

    def test_analysis_waits_until_it_is_entered(self, client, reviewer, contract, document):  # noqa: F811
        from django.urls import reverse

        client.force_login(reviewer)
        url = reverse("documents:confirm", args=[contract.pk])
        with mock.patch("apps.documents.queueing.queue_analysis") as queued:
            client.post(url, {"action": "analyse", "fact-contract_value": "other",
                              "fact-contract_value-other": "", "page-1": "1"})
            queued.assert_not_called()
            client.post(url, {"action": "analyse", "fact-contract_value": "other",
                              "fact-contract_value-other": "157,372.80", "fact-contract_value-unit": "JOD",
                              "page-1": "1"})
            queued.assert_called_once()
        value = document.key_facts.get(name=KeyFact.Name.CONTRACT_VALUE)
        assert value.status == KeyFact.Status.CONFIRMED and value.value == "157,372.80"

    def test_not_in_the_contract_is_an_answer_too(self, client, reviewer, contract, document):  # noqa: F811
        from django.urls import reverse

        client.force_login(reviewer)
        with mock.patch("apps.documents.queueing.queue_analysis") as queued:
            client.post(reverse("documents:confirm", args=[contract.pk]),
                        {"action": "analyse", "fact-contract_value": "none", "page-1": "1"})
            queued.assert_called_once()


class TestNotesReachTheReader:
    """In the automatic flow no one opens the confirmation screen: a contract
    stating two delay-damage rates must be said on the contract page and in
    the report."""

    @pytest.fixture
    def document(self, contract):  # noqa: F811
        document = make_document(contract, {1: ["الشروط الخاصة", *PARTICULARS], 2: ["الشروط العامة", *CONDITIONS]},
                                 scanned=True)
        analyse_document(document)
        document.warnings = [*document.warnings, *fc.settle_facts(document)]
        document.save()
        return document

    def test_on_the_contract_page(self, client, reviewer, contract, document):  # noqa: F811
        from django.urls import reverse

        reviewer.preferred_language = "en"
        reviewer.save(update_fields=["preferred_language"])
        client.force_login(reviewer)
        body = client.get(reverse("contracts:detail", args=[contract.pk])).content.decode()
        assert "Notes on the file" in body
        assert "170 (p1), 125 (p2)" in body

    def test_in_the_report(self, client, reviewer, contract, document):  # noqa: F811
        from django.urls import reverse

        from apps.analysis.models import AnalysisRun

        AnalysisRun.objects.create(contract=contract, document=document, version=1, status="succeeded")
        contract.current_analysis = AnalysisRun.objects.get(contract=contract)
        contract.save()
        client.force_login(reviewer)
        body = client.get(reverse("reports:index") + f"?contract={contract.pk}").content.decode()
        assert "170 (p1), 125 (p2)" in body


def test_a_garbled_rate_is_still_seen_and_the_contradiction_reported(contract):  # noqa: F811
    """Ain Al Basha: OCR read "غرامة مقدارها (125 دينار)" as "غرامة (pln 125( lalate", so
    the contract's second delay rate was never seen and 170 was settled silently."""
    particulars = ["الشروط الخاصة / بيانات العقد", "قيمة غرامة التأخير: 170 دينار /لكل يوم تأخير"]
    body = "يلتزم الاستشاري بموجب هذا العقد بتقديم الخدمات المطلوبة وفقا للشروط والمواصفات المعتمدة " * 4
    article = ["المادة )7(— سريان العقد", body,
               "فان على الاستشاري أن يقوم بدفع غرامة (pln 125( lalate‏ عن كل يوم تأخير غير مبرر"]
    document = make_document(contract, {126: particulars, 132: article}, scanned=True)
    analyse_document(document)
    conflicts = fc.refresh_facts(document)
    rate = document.key_facts.get(name=KeyFact.Name.DELAY_DAMAGES_RATE)
    assert {c["value"] for c in rate.candidates} == {"170", "125"}
    assert rate.value == "170" and conflicts and conflicts[0]["values"] == "170 (p126), 125 (p132)"


def test_refreshing_keeps_what_a_person_typed(contract):  # noqa: F811
    document = make_document(contract, {1: ["مدة العقد: 180 يوما تقويمياً"]}, scanned=True)
    analyse_document(document)
    fact = document.key_facts.get(name=KeyFact.Name.CONTRACT_VALUE)
    fact.status, fact.value, fact.unit = KeyFact.Status.CONFIRMED, "157372.80", "JOD"
    fact.save()
    fc.refresh_facts(document)
    fact = document.key_facts.get(name=KeyFact.Name.CONTRACT_VALUE)
    assert fact.status == KeyFact.Status.CONFIRMED and fact.value == "157372.80"
