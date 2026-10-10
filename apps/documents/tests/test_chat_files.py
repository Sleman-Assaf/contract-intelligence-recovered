"""Chat attachments are read once and answered from what was stored.

Also the contract pipeline's side of the same rules: native text skips OCR
and vision, a provider outage keeps the OCR text, and the vision limit holds
on a bad scan.
"""
from __future__ import annotations

import pymupdf
import pytest

from apps.accounts.models import Country, Entity, Role, User
from apps.documents import chat_files
from apps.documents.models import ExtractedFile, ExtractedRegion, ExtractionMethod, VerificationStatus

pytestmark = pytest.mark.django_db

LINES = [
    "4.3 Delay Damages",
    "The Consultant shall pay 0.5% per week.",
    "There is no maximum limit on delay damages.",
    "9.1 Payment",
    "Invoices are paid within 30 days of receipt.",
]


def _engine_or_skip():
    from apps.documents.ocr import get_ocr_engine

    ok, detail = get_ocr_engine().is_available()
    if not ok:
        pytest.skip(f"OCR engine not available: {detail}")


def native_pdf(lines=LINES) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 90
    for line in lines:
        page.insert_text((72, y), line, fontsize=12)
        y += 60
    data = doc.tobytes(); doc.close()
    return data


def scanned_pdf(lines=LINES, pages=1) -> bytes:
    """Image-only pages: a scan, with known words at known places."""
    original = pymupdf.open()
    for _ in range(pages):
        page = original.new_page(width=595, height=842)
        y = 90
        for line in lines:
            page.insert_text((72, y), line, fontsize=16)
            y += 90
    scan = pymupdf.open()
    for page in original:
        pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
        target = scan.new_page(width=595, height=842)
        target.insert_image(target.rect, stream=pix.tobytes("png"))
    data = scan.tobytes(deflate=True); scan.close(); original.close()
    return data


@pytest.fixture
def private_media(settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.EXTRACTION_ASYNC = False
    settings.OCR_LANGUAGES = "eng"
    return settings.PRIVATE_MEDIA_ROOT


@pytest.fixture
def entity():
    return Entity.objects.create(name="E", code="e", country=Country.JORDAN)


@pytest.fixture
def alice(entity):
    return User.objects.create_user(email="alice@aji.test", password="x-long-passphrase-42",
                                    role=Role.CONTRACT_REVIEWER, entity=entity)


@pytest.fixture
def bob(entity):
    return User.objects.create_user(email="bob@aji.test", password="x-long-passphrase-42",
                                    role=Role.CONTRACT_REVIEWER, entity=entity)


@pytest.fixture
def no_vision(monkeypatch):
    """Vision must not be called; a call fails the test."""
    from apps.ai.services import ai_service

    def refuse(**_kw):
        raise AssertionError("vision was called")

    monkeypatch.setattr(ai_service, "verify_text_region", refuse)


@pytest.fixture
def no_ocr(monkeypatch):
    def refuse():
        raise AssertionError("OCR was run")

    monkeypatch.setattr(chat_files, "_ocr_engine", refuse)


@pytest.fixture
def confident_gate(settings):
    """OCR accepted as read: no region is flagged for vision."""
    settings.OCR_THRESHOLDS = {"accept": 0, "validate": 0, "vision": 0, "critical_value": 0}


@pytest.fixture
def counted(monkeypatch):
    runs = []
    real = chat_files.process

    def wrapped(file_id):
        runs.append(str(file_id))
        return real(file_id)

    monkeypatch.setattr(chat_files, "process", wrapped)
    return runs


class TestNativeText:
    def test_a_text_pdf_needs_no_ocr_and_no_vision(self, private_media, alice, no_ocr, no_vision):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        assert file.status == ExtractedFile.Status.READY and file.native_text
        assert file.vision_calls == 0
        regions = list(file.regions.all())
        assert regions and all(r.extraction_method == ExtractionMethod.NATIVE_PDF for r in regions)
        assert "0.5% per week" in file.text

    def test_it_is_stored_as_clauses(self, private_media, alice):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        numbers = [c.clause_number for c in file.chunks.order_by("ordinal")]
        assert numbers == ["4.3", "9.1"]
        assert "0.5%" in file.chunks.get(clause_number="4.3").text


class TestSha256Reuse:
    def test_the_same_person_and_file_is_read_once_under_any_name(self, private_media, alice, counted):
        data = native_pdf()
        first = chat_files.ingest(alice, data, "agreement.pdf")
        second = chat_files.ingest(alice, data, "renamed copy.pdf")
        assert first.pk == second.pk and len(counted) == 1
        assert ExtractedFile.objects.count() == 1
        # And one copy on disk, not one per upload.
        assert len(list((private_media / "advisor" / str(alice.pk)).iterdir())) == 1

    def test_another_persons_extraction_is_never_reused(self, private_media, alice, bob, counted):
        data = native_pdf()
        mine = chat_files.ingest(alice, data, "a.pdf")
        theirs = chat_files.ingest(bob, data, "a.pdf")
        assert mine.pk != theirs.pk and len(counted) == 2
        assert theirs.owner == bob and mine.sha256 == theirs.sha256

    def test_a_failed_reading_is_tried_again(self, private_media, alice, counted):
        data = native_pdf()
        file = chat_files.ingest(alice, data, "a.pdf")
        ExtractedFile.objects.filter(pk=file.pk).update(status=ExtractedFile.Status.FAILED)
        again = chat_files.ingest(alice, data, "a.pdf")
        assert len(counted) == 2 and again.status == ExtractedFile.Status.READY

    def test_what_is_refused(self, private_media, alice):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            chat_files.ingest(alice, b"GIF89a....", "x.gif")
        with pytest.raises(ValidationError):
            chat_files.ingest(alice, b"%PDF-1.7 not really", "x.pdf")
        assert not ExtractedFile.objects.exists()


class TestAScan:
    def test_is_ocrd_and_stored_as_text(self, private_media, alice, confident_gate, no_vision):
        _engine_or_skip()
        file = chat_files.ingest(alice, scanned_pdf(), "scan.pdf")
        assert file.status == ExtractedFile.Status.READY and not file.native_text
        assert "0.5%" in file.text and "30 days" in file.text
        region = file.regions.filter(source_text__contains="0.5%").first()
        assert region.extraction_method == ExtractionMethod.OCR
        assert region.bbox_units == "pt" and region.bbox[3] <= 842     # points, on the page
        assert region.word_confidences

    def test_the_vision_limit_holds_and_critical_values_go_first(
            self, private_media, alice, settings, monkeypatch):
        _engine_or_skip()
        from apps.ai.providers.base import AICallResult
        from apps.ai.schemas import TextRegionVerification
        from apps.ai.services import ai_service

        # Every page is a bad scan; vision reads something unhelpful.
        settings.OCR_THRESHOLDS = {"accept": 101, "validate": 101, "vision": 101, "critical_value": 101}
        settings.VISION_MAX_CALLS_PER_PAGE = 2
        settings.VISION_MAX_CALLS_PER_FILE = 30
        calls = []

        def fake(**kw):
            calls.append(kw)
            return AICallResult(parsed=TextRegionVerification(text="unreadable", confidence=0.2),
                                provider="f", model="v", request_type="verify_text_region",
                                latency_ms=1, prompt_tokens=1, completion_tokens=1, raw_text="{}")

        monkeypatch.setattr(ai_service, "verify_text_region", fake)
        file = chat_files.ingest(alice, scanned_pdf(), "bad-scan.pdf")
        assert len(calls) == 2 and file.vision_calls == 2
        assert file.status == ExtractedFile.Status.NEEDS_REVIEW
        read = file.regions.exclude(vision_pass1_text="")
        assert read.exists() and all(r.contains_critical_value for r in read)
        leftovers = file.regions.filter(mismatch_reason__contains="vision_limit")
        assert leftovers.exists()
        # Nothing was lost: every region keeps its OCR reading.
        assert all(r.source_text for r in file.regions.all())


class TestAnsweringFromIt:
    def test_a_follow_up_uses_the_stored_text_not_the_file(self, private_media, alice, confident_gate, monkeypatch):
        _engine_or_skip()
        file = chat_files.ingest(alice, scanned_pdf(), "scan.pdf")

        # From here on, reading the file again in any way fails the test.
        monkeypatch.setattr(chat_files, "_ocr_engine", lambda: pytest.fail("OCR ran again"))
        monkeypatch.setattr(chat_files.extractors, "rasterise_page",
                            lambda *a, **k: pytest.fail("the page was rendered again"))
        from apps.ai.services import ai_service

        monkeypatch.setattr(ai_service, "verify_text_region",
                            lambda **k: pytest.fail("vision ran again"))
        ctx = chat_files.context_for(file, "What are the delay damages?")
        assert ctx.images == [] and not ctx.fallback
        assert any("0.5%" in x for x in ctx.excerpts)

    def test_a_visual_question_sends_the_page(self, private_media, alice):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        assert chat_files.context_for(file, "What is clause 4.3?").images == []
        ctx = chat_files.context_for(file, "Is this contract signed and stamped?")
        assert len(ctx.images) == 1 and ctx.images[0].image_bytes.startswith(b"\x89PNG")
        assert chat_files.context_for(file, "هل يوجد ختم على العقد؟").images

    def test_ordinary_arabic_phrasing_is_not_a_visual_question(self, private_media, alice):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        for q in ("اشرح البند بشكل مبسط", "هل الدفع بصورة شهرية؟", "ما هو الموقف الرسمي؟"):
            assert chat_files.context_for(file, q).images == [], q

    def test_an_unresolved_value_is_marked_and_its_crop_sent(self, private_media, alice):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        region = file.regions.get(source_text__contains="0.5%")
        region.status = VerificationStatus.NEEDS_HUMAN_REVIEW
        region.final_text = ""
        region.save()
        file.chunks.all().delete()
        chat_files._store_text_and_chunks(file)
        ctx = chat_files.context_for(file, "What is clause 4.3?")
        joined = "\n".join(ctx.excerpts)
        assert chat_files.UNVERIFIED_OPEN + "The Consultant shall pay 0.5% per week." in joined
        assert "[VERIFICATION NOTE]" in joined and "human review" in joined
        assert len(ctx.images) == 1                        # the crop of exactly that line

    def test_continue_picks_up_after_the_last_chunk(self, private_media, alice):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        ctx = chat_files.context_for(file, "continue", last_sent=0, continuing=True)
        assert ctx.chunks_sent == [1]
        end = chat_files.context_for(file, "continue", last_sent=1, continuing=True)
        assert end.excerpts[0].startswith("[END OF DOCUMENT]")

    def test_a_page_reference(self, private_media, alice):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        assert chat_files.select_chunks(file, "what is on page 1?")[1] == "page"

    def test_a_file_nothing_could_be_read_from_goes_as_pictures(self, private_media, alice):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        ExtractedFile.objects.filter(pk=file.pk).update(status=ExtractedFile.Status.FAILED)
        file.refresh_from_db()
        ctx = chat_files.context_for(file, "What is clause 4.3?")
        assert ctx.fallback and len(ctx.images) == 1 and ctx.excerpts == []


class TestTheContractPipeline:
    @pytest.fixture
    def contract_file(self, alice, entity, tmp_path, monkeypatch):
        from apps.contracts.models import Contract, ContractType
        from apps.documents.models import SourceFile
        import apps.documents.services as services

        def make(data: bytes):
            path = tmp_path / "contract.pdf"
            path.write_bytes(data)
            contract = Contract.objects.create(
                project_name="P", contract_title="T", client_name="TempData Technologies",
                country=Country.JORDAN, entity=entity,
                contract_type=ContractType.objects.create(code="x", name="X"), uploaded_by=alice)
            source = SourceFile.objects.create(
                contract=contract, original_filename="contract.pdf", stored_path="contract.pdf",
                content_type="application/pdf", size_bytes=len(data), sha256="a" * 64)
            monkeypatch.setattr(services, "absolute_path", lambda _p: path)
            return source
        return make

    def test_native_text_skips_ocr_and_vision(self, contract_file, monkeypatch, no_vision):
        import apps.documents.services as services

        monkeypatch.setattr(services, "get_ocr_engine", lambda: pytest.fail("OCR ran"))
        outcome = services.run_extraction(contract_file(native_pdf()))
        assert outcome.pages_native == 1 and outcome.pages_ocr == 0
        assert outcome.regions_sent_to_vision == 0

    def test_a_provider_outage_keeps_the_ocr_text(self, contract_file, settings, monkeypatch):
        _engine_or_skip()
        import apps.documents.services as services
        from apps.ai.providers.base import AIProviderUnavailable
        from apps.ai.services import ai_service
        from apps.documents.models import Block, VisionVerification

        settings.OCR_LANGUAGES = "eng"
        settings.OCR_THRESHOLDS = {"accept": 101, "validate": 101, "vision": 101, "critical_value": 101}

        def down(**_kw):
            raise AIProviderUnavailable("AI server unreachable")

        monkeypatch.setattr(ai_service, "verify_text_region", down)
        outcome = services.run_extraction(contract_file(scanned_pdf()))   # does not raise
        assert outcome.needs_human_review and outcome.regions_sent_to_vision > 0
        block = Block.objects.filter(original_source_text__contains="0.5%").get()
        assert block.verification_status == VerificationStatus.NEEDS_HUMAN_REVIEW
        assert block.final_text == "" and block.usable_text == block.original_source_text
        assert "vision_unavailable" in block.mismatch_reason
        row = VisionVerification.objects.filter(block=block).get()
        assert row.outcome == VisionVerification.Outcome.ERROR and row.pass_number == 1
        assert row.bbox and row.page_number == 1

    def test_the_same_file_again_in_the_same_contract_is_not_read_again(self, contract_file, monkeypatch):
        import apps.documents.services as services
        from apps.documents.models import SourceFile

        first = contract_file(native_pdf())
        services.run_extraction(first)
        again = SourceFile.objects.create(
            contract=first.contract, original_filename="round 2.pdf", stored_path="contract.pdf",
            content_type="application/pdf", size_bytes=first.size_bytes, sha256=first.sha256,
            round_number=2)
        monkeypatch.setattr(services.extractors, "extract", lambda *a, **k: pytest.fail("read again"))
        outcome = services.run_extraction(again)
        assert outcome.blocks_created == first.document.blocks.count() > 0


class TestUnresolvedValuesStayUnresolved:
    @pytest.fixture
    def file_with_an_unresolved_rate(self, private_media, alice):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        region = file.regions.get(source_text__contains="0.5%")
        region.status = VerificationStatus.NEEDS_HUMAN_REVIEW
        region.final_text = ""
        region.vision_pass1_text = "The Consultant shall pay 5% per week."
        region.save()
        file.chunks.all().delete()
        chat_files._store_text_and_chunks(file)
        return file

    def test_both_readings_travel_inside_the_marker(self, file_with_an_unresolved_rate):
        text = file_with_an_unresolved_rate.chunks.get(clause_number="4.3").text
        assert (chat_files.UNVERIFIED_OPEN + "OCR read: The Consultant shall pay 0.5% per week. || "
                "a vision reading gave: The Consultant shall pay 5% per week." + chat_files.UNVERIFIED_CLOSE) in text

    def test_an_unrelated_question_gets_the_note_but_no_picture(self, file_with_an_unresolved_rate):
        ctx = chat_files.context_for(file_with_an_unresolved_rate, "Summarise the file")
        assert any(x.startswith("[VERIFICATION NOTE]") for x in ctx.excerpts)
        assert ctx.images == []

    def test_a_question_about_it_gets_the_crop(self, file_with_an_unresolved_rate):
        ctx = chat_files.context_for(file_with_an_unresolved_rate, "How much does the Consultant pay per week?")
        assert len(ctx.images) == 1

    def test_an_answer_stating_it_is_flagged(self, file_with_an_unresolved_rate):
        file = file_with_an_unresolved_rate
        assert chat_files.unverified_values(file, "The rate is 0.5% per week, paid within 30 days.") == ["0.5%"]
        assert chat_files.unverified_values(file, "Invoices are paid within 30 days.") == []
        assert chat_files.unverified_values(file, "Clause 4.3 covers delay.") == []

    def test_nothing_is_flagged_when_everything_was_verified(self, private_media, alice):
        file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
        assert chat_files.unverified_values(file, "It is 7% and SAR 1,000.") == []


def test_a_footer_inside_a_clause_is_only_pictured_when_asked_about(private_media, alice):
    """Measured live: a question about clause 4.3 was sent pictures of the
    page footer (party names), because the footer fell inside 4.3's chunk."""
    lines = ["4.3 Delay Damages", "The Consultant shall pay 0.5% per week.",
             "TempData Technologies L.L.C. / AJi Engineering Consultants - Page 3 of 11"]
    file = chat_files.ingest(alice, native_pdf(lines), "agreement.pdf")
    footer = file.regions.get(source_text__contains="TempData")
    footer.status, footer.final_text = VerificationStatus.NEEDS_HUMAN_REVIEW, ""
    footer.save()
    file.chunks.all().delete()
    chat_files._store_text_and_chunks(file)
    assert chat_files.context_for(file, "What does clause 4.3 say?").images == []
    assert len(chat_files.context_for(file, "Who are the parties?").images) == 1


def _file_of(alice, texts):
    """A stored reading made of `texts`, one chunk per page."""
    from apps.documents.models import ExtractedChunk

    file = chat_files.ingest(alice, native_pdf(), "agreement.pdf")
    file.chunks.all().delete()
    for n, text in enumerate(texts, 1):
        ExtractedChunk.objects.create(file=file, ordinal=n, page_start=n, page_end=n, text=text,
                                      clause_number=f"{n}.1")
    return file


class TestAWholeFileIsRead:
    """An 11-page contract was answered from its first three pages: only the
    start (6,000 characters) went with any question that named no page."""

    ELEVEN_PAGES = [f"{n}.1 Page {n} of the agreement. " + "Text. " * 350 for n in range(1, 12)]

    @pytest.mark.parametrize("question", [
        "قديش صفحة هذا العقد", "قراتهم كلهم ؟", "اقراهم", "لخص العقد",
        "ما سقف المسؤولية؟",            # Arabic question, English contract: no word matches
    ])
    def test_a_small_file_goes_whole(self, private_media, alice, question):
        file = _file_of(alice, self.ELEVEN_PAGES)
        chunks, mode = chat_files.select_chunks(file, question)
        assert mode == "whole" and [c.page_start for c in chunks] == list(range(1, 12))

    def test_a_named_page_or_clause_still_goes_alone(self, private_media, alice):
        file = _file_of(alice, self.ELEVEN_PAGES)
        assert chat_files.select_chunks(file, "what is on page 9?")[1] == "page"
        chunks, mode = chat_files.select_chunks(file, "اشرح البند 7.1")
        assert mode == "clause" and [c.page_start for c in chunks] == [7]

    def test_a_translation_still_goes_in_parts(self, private_media, alice):
        file = _file_of(alice, self.ELEVEN_PAGES)
        chunks, mode = chat_files.select_chunks(file, "ترجم العقد")
        assert mode == "in_order" and sum(len(c.text) for c in chunks) <= chat_files.FULL_MAX_CHARS

    def test_a_large_file_is_still_chosen_from(self, private_media, alice):
        big = [f"{n}.1 Page {n}. " + "Text. " * 900 for n in range(1, 20)]
        file = _file_of(alice, big)
        assert sum(len(t) for t in big) > chat_files.WHOLE_FILE_MAX_CHARS
        chunks, mode = chat_files.select_chunks(file, "اقرأ الكل")
        assert mode == "in_order"                          # "read it all": in order, in parts
        assert chat_files.select_chunks(file, "anything")[1] == "start"


class TestTheModelIsToldItHasEverything:
    def test_a_whole_file_ends_with_the_complete_document_note(self, private_media, alice):
        file = _file_of(alice, TestAWholeFileIsRead.ELEVEN_PAGES)
        ExtractedFile.objects.filter(pk=file.pk).update(page_count=11)
        file.refresh_from_db()
        ctx = chat_files.context_for(file, "لخصلي اياهم صفحة صفحة")
        assert ctx.excerpts[-1].startswith("[COMPLETE DOCUMENT]") and "all 11 page(s)" in ctx.excerpts[-1]
        assert ctx.excerpts[0].startswith("[Clause 1.1 p.1]")          # numbering of the excerpts unchanged
        assert len(ctx.excerpts) == 12

    def test_a_selection_does_not_claim_to_be_complete(self, private_media, alice):
        file = _file_of(alice, TestAWholeFileIsRead.ELEVEN_PAGES)
        ctx = chat_files.context_for(file, "what is on page 9?")
        assert not any(x.startswith("[COMPLETE DOCUMENT]") for x in ctx.excerpts)
