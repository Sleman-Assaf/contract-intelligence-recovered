"""Key facts held for confirmation before analysis (key_facts.py,
confirm_views.py, tasks.py)."""
from __future__ import annotations

from unittest import mock

import pymupdf
import pytest
from django.urls import reverse

from apps.accounts.models import Country, Entity, Role, User
from apps.contracts.models import Contract, ContractStatus, ContractType
from apps.documents.key_facts import analyse_document
from apps.documents.models import (
    Block,
    Document,
    ExtractionMethod,
    KeyFact,
    Page,
    PageKind,
    SourceFile,
    VerificationStatus,
)
from apps.documents.queueing import analysis_limits, extraction_limits

pytestmark = pytest.mark.django_db


@pytest.fixture
def entity():
    return Entity.objects.create(name="AJi - Jordan", code="aji-jo", country=Country.JORDAN)


@pytest.fixture
def reviewer(entity):
    return User.objects.create_user(email="rev@aji.test", password="test-password-1234",
                                    role=Role.CONTRACT_REVIEWER, entity=entity)


@pytest.fixture
def contract(entity, reviewer):
    kind = ContractType.objects.create(code="design", name="Design Services")
    return Contract.objects.create(project_name="Ain Al Basha Networks", contract_title="Design",
                                   client_name="Miyahuna", country=Country.JORDAN, entity=entity,
                                   contract_type=kind, uploaded_by=reviewer)


def make_document(contract, pages: dict[int, list[str]], *, scanned=False, corrections=None):
    source = SourceFile.objects.create(contract=contract, original_filename="c.pdf",
                                       stored_path="contracts/x/c.pdf", content_type="application/pdf",
                                       size_bytes=1, sha256="0" * 64, page_count=len(pages))
    from django.utils import timezone

    document = Document.objects.create(source_file=source, contract=contract, page_count=len(pages),
                                       extraction_finished_at=timezone.now())
    method = ExtractionMethod.OCR if scanned else ExtractionMethod.NATIVE_PDF
    for number, blocks in pages.items():
        page = Page.objects.create(document=document, page_number=number, width=595, height=842,
                                   has_native_text=not scanned, extraction_method=method,
                                   ocr_confidence=88 if scanned else None)
        for i, text in enumerate(blocks):
            Block.objects.create(page=page, document=document, block_index=i, original_source_text=text,
                                 bbox_x0=50, bbox_y0=50 + 40 * i, bbox_x1=500, bbox_y1=80 + 40 * i,
                                 extraction_method=method, min_word_confidence=91 if scanned else None,
                                 verification_status=VerificationStatus.ACCEPTED,
                                 corrections=(corrections or {}).get((number, i), []),
                                 corrected_text="")
    return document


PARTICULARS = ["مدة العقد: 180 يوما تقويمياً", "قيمة العقد المقبولة : (87,792.05) دينار",
               "قيمة غرامة التأخير: 170 دينار /لكل يوم تأخير"]
CONDITIONS = ["7.4 فان على الاستشاري أن يقوم بدفع غرامة التأخير مقدارها (125 دينار) عن كل يوم تأخير غير مبرر",
              "7.5 يحدد للغرامة سقف حده الأعلى (15%) من قيمة العقد المقبولة."]


class TestFacts:
    def test_two_delay_damage_rates_are_a_conflict(self, contract):
        document = make_document(contract, {1: PARTICULARS, 2: CONDITIONS})
        analyse_document(document)
        rate = document.key_facts.get(name=KeyFact.Name.DELAY_DAMAGES_RATE)
        assert rate.status == KeyFact.Status.CONFLICT
        assert set(rate.distinct_values) == {"170", "125"}

    def test_one_consistent_reading_is_found(self, contract):
        document = make_document(contract, {1: PARTICULARS, 2: CONDITIONS})
        analyse_document(document)
        duration = document.key_facts.get(name=KeyFact.Name.DURATION)
        assert (duration.status, duration.value, duration.unit) == (KeyFact.Status.FOUND, "180", "days")
        cap = document.key_facts.get(name=KeyFact.Name.DELAY_DAMAGES_CAP)
        assert (cap.value, cap.unit) == ("15", "%")
        value = document.key_facts.get(name=KeyFact.Name.CONTRACT_VALUE)
        assert value.value == "87792.05"

    def test_fifty_44_months_is_a_conflict(self, contract):
        document = make_document(contract, {1: [
            "14.1 The time period shall be: Fifty (44) months, including 20 months for the works."]})
        analyse_document(document)
        duration = document.key_facts.get(name=KeyFact.Name.DURATION)
        assert duration.status == KeyFact.Status.CONFLICT
        assert set(duration.distinct_values) == {"44", "50"}
        assert any(w["kind"] == "words_vs_figure" for w in document.warnings)

    def test_a_repaired_reading_is_uncertain(self, contract):
        document = make_document(contract, {1: ["وتكون كفالة حسن التنفيذ بنسبة 10% من قيمة العقد"]},
                                 scanned=True, corrections={(1, 0): [
                                     {"kind": "arabic_percent", "from": "9610", "to": "10%"}]})
        analyse_document(document)
        bond = document.key_facts.get(name=KeyFact.Name.PERFORMANCE_BOND)
        assert bond.status == KeyFact.Status.UNCERTAIN and bond.candidates[0]["repaired"]

    def test_european_figures(self, contract):
        document = make_document(contract, {1: [
            "Total Amount of Services of AJi WE (Remuneration + Reimbursable): up to 690.945,38 EUR"]})
        analyse_document(document)
        assert document.key_facts.get(name=KeyFact.Name.CONTRACT_VALUE).value == "690945.38"

    def test_a_value_split_from_its_label_across_blocks(self, contract):
        document = make_document(contract, {1: ["Stage 1 + Stage 2: Lump-sum amount of", "9,940 JOD"]})
        analyse_document(document)
        fact = document.key_facts.get(name=KeyFact.Name.CONTRACT_VALUE)
        assert fact.value == "9940"
        second_block = document.blocks.get(block_index=1)
        assert fact.candidates[0]["block"] == str(second_block.pk)

    def test_a_missing_fact_is_not_found(self, contract):
        document = make_document(contract, {1: PARTICULARS})
        analyse_document(document)
        assert document.key_facts.get(name=KeyFact.Name.RETENTION).status == KeyFact.Status.NOT_FOUND

    def test_placeholder_pages_are_left_out_and_reported(self, contract):
        document = make_document(contract, {1: CONDITIONS, 2: ["Attachment 2: Service Contract",
                                                                "see external pdf file"]})
        analyse_document(document)
        page = document.pages.get(page_number=2)
        assert page.kind == PageKind.PLACEHOLDER and not page.include_in_analysis
        assert any(w["kind"] == "placeholder" and w["page"] == 2 for w in document.warnings)

    def test_a_scanned_file_needs_confirmation(self, contract, settings):
        settings.KEY_FACTS_CONFIRMATION = "scanned"
        scanned = make_document(contract, {1: PARTICULARS}, scanned=True)
        assert analyse_document(scanned).needs_confirmation

    def test_a_clean_native_file_does_not(self, contract, settings):
        settings.KEY_FACTS_CONFIRMATION = "scanned"
        document = make_document(contract, {1: ["مدة العقد: 180 يوما تقويمياً"]})
        assert not analyse_document(document).needs_confirmation


class TestConfirmScreen:
    @pytest.fixture
    def document(self, contract):
        document = make_document(contract, {1: PARTICULARS, 2: CONDITIONS, 3: CONDITIONS[:1]})
        analyse_document(document)
        contract.status = ContractStatus.NEEDS_SOURCE_REVIEW
        contract.save(update_fields=["status"])
        return document

    def test_it_shows_every_reading(self, client, reviewer, contract, document):
        client.force_login(reviewer)
        body = client.get(reverse("documents:confirm", args=[contract.pk])).content.decode()
        assert "170" in body and "125" in body
        assert reverse("documents:region_image", args=[document.pk, 1]) in body

    def test_confirming_saves_values_and_pages_and_starts_the_analysis(self, client, reviewer,
                                                                         contract, document):
        client.force_login(reviewer)
        rate = document.key_facts.get(name=KeyFact.Name.DELAY_DAMAGES_RATE)
        index_125 = next(i for i, c in enumerate(rate.candidates) if c["value"] == "125")
        with mock.patch("apps.analysis.tasks.analyse_contract.apply_async") as queued:
            response = client.post(reverse("documents:confirm", args=[contract.pk]), {
                "fact-delay_damages_rate": f"c:{index_125}",
                "fact-contract_value": "c:0",
                "fact-retention": "none",
                "page-1": "1", "page-2": "1", "page-3": "1",
                "exclude_range": "3",
                "action": "analyse",
            })
        assert response.status_code == 302
        queued.assert_called_once()
        rate.refresh_from_db()
        assert (rate.status, rate.value, rate.confirmed_by) == (KeyFact.Status.CONFIRMED, "125", reviewer)
        assert not document.pages.get(page_number=3).include_in_analysis
        contract.refresh_from_db()
        assert contract.status == ContractStatus.STRUCTURING
        assert str(contract.contract_value) == "87792.05"

    def test_confirming_again_while_it_runs_starts_no_second_analysis(self, client, reviewer, contract, document):
        client.force_login(reviewer)
        with mock.patch("apps.analysis.tasks.analyse_contract.apply_async") as queued:
            client.post(reverse("documents:confirm", args=[contract.pk]), {"action": "analyse", "page-1": "1"})
            client.post(reverse("documents:confirm", args=[contract.pk]), {"action": "analyse", "page-1": "1"})
        assert queued.call_count == 1

    def test_save_without_analysing(self, client, reviewer, contract, document):
        client.force_login(reviewer)
        with mock.patch("apps.analysis.tasks.analyse_contract.apply_async") as queued:
            client.post(reverse("documents:confirm", args=[contract.pk]),
                        {"fact-duration": "other", "fact-duration-other": "200", "fact-duration-unit": "days",
                         "page-1": "1", "action": "save"})
        queued.assert_not_called()
        duration = document.key_facts.get(name=KeyFact.Name.DURATION)
        assert (duration.value, duration.status) == ("200", KeyFact.Status.CONFIRMED)

    def test_a_role_without_run_analysis_cannot_confirm(self, client, entity, contract, document):
        legal = User.objects.create_user(email="legal@aji.test", password="test-password-1234",
                                         role=Role.LEGAL_REVIEWER, entity=entity)
        client.force_login(legal)
        response = client.post(reverse("documents:confirm", args=[contract.pk]), {"action": "analyse"})
        assert response.status_code == 403

    def test_the_progress_card_leads_there(self, client, reviewer, contract, document):
        client.force_login(reviewer)
        state = client.get(reverse("contracts:progress", args=[contract.pk])).json()
        assert state["next"] == reverse("documents:confirm", args=[contract.pk])


class TestExtractionStopsForConfirmation:
    @pytest.fixture
    def native_pdf(self, tmp_path, settings):
        settings.PRIVATE_MEDIA_ROOT = tmp_path
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "4.1 The Consultant shall carry out the Services with skill and care.",
                         fontsize=10)
        page.insert_text((72, 130), "4.2 The time period shall be: Fifty (44) months from commencement.",
                         fontsize=10)
        page.insert_text((72, 160), "4.3 The Client shall pay invoices within thirty (30) days of receipt.",
                         fontsize=10)
        (tmp_path / "contracts").mkdir()
        path = tmp_path / "contracts" / "c.pdf"
        doc.save(str(path))
        return "contracts/c.pdf"

    def test_a_conflict_stops_the_pipeline(self, contract, native_pdf, settings):
        from apps.documents.tasks import extract_source_file

        settings.KEY_FACTS_CONFIRMATION = "scanned"
        source = SourceFile.objects.create(contract=contract, original_filename="c.pdf", stored_path=native_pdf,
                                           content_type="application/pdf", size_bytes=1, sha256="1" * 64)
        with mock.patch("apps.analysis.tasks.analyse_contract.apply_async") as queued:
            result = extract_source_file.run(str(source.pk))
        contract.refresh_from_db()
        assert result["status"] == "needs_source_review"
        assert contract.status == ContractStatus.NEEDS_SOURCE_REVIEW
        queued.assert_not_called()


class TestTimeLimits:
    def test_the_fixed_limit_is_the_floor(self, settings):
        settings.CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60
        assert extraction_limits(5) == (25 * 60, 30 * 60)

    def test_a_long_file_gets_longer(self, settings):
        settings.CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60
        settings.EXTRACTION_SECONDS_PER_PAGE = 20
        settings.ANALYSIS_SECONDS_PER_PAGE = 45
        soft, hard = extraction_limits(179)
        assert soft == 10 * 60 + 179 * 20 and hard == soft + 300
        assert analysis_limits(103)[0] == 10 * 60 + 103 * 45

    def test_there_is_a_ceiling(self, settings):
        settings.TASK_TIME_LIMIT_CEILING = 3600
        assert extraction_limits(100000)[0] == 3600
