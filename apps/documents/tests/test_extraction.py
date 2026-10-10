"""Extraction, critical-value detection and the OCR quality gate (§5–§9, §48)."""
from __future__ import annotations

import pytest

from apps.documents import extractors
from apps.documents.critical_values import (
    CriticalKind,
    contains_critical_value,
    find_critical_values,
    normalise_digits,
    values_agree,
)
from apps.documents.ocr.base import BBox, OCRBlock, OCRLine, OCRPageResult, OCRWord
from apps.documents.quality import GateDecision, evaluate_page


# ── helpers ──────────────────────────────────────────────────────────────

def make_page(words: list[tuple[str, float]], *, block_count: int = 1) -> OCRPageResult:
    """Build an OCRPageResult from (text, confidence) pairs."""
    per_block = max(1, len(words) // block_count)
    blocks = []
    for b in range(block_count):
        chunk = words[b * per_block : (b + 1) * per_block] or words[-1:]
        ocr_words = [
            OCRWord(text=t, bbox=BBox(0, i * 10, 50, i * 10 + 9), confidence=c)
            for i, (t, c) in enumerate(chunk)
        ]
        line = OCRLine(
            text=" ".join(w.text for w in ocr_words),
            bbox=BBox(0, 0, 50, 100),
            confidence=sum(w.confidence for w in ocr_words) / len(ocr_words),
            words=ocr_words,
        )
        blocks.append(
            OCRBlock(text=line.text, bbox=line.bbox, confidence=line.confidence,
                     lines=[line], reading_order=b)
        )
    all_conf = [w.confidence for b in blocks for line in b.lines for w in line.words]
    return OCRPageResult(
        page_number=1, blocks=blocks,
        mean_confidence=sum(all_conf) / len(all_conf), engine="test",
    )


# ── critical values ──────────────────────────────────────────────────────

class TestArabicDigits:
    """Arabic contracts use Arabic-Indic digits. A detector that only knows
    ASCII would miss every number in the document."""

    def test_normalises_arabic_indic(self):
        assert normalise_digits("١٠٪") == "10٪"
        assert normalise_digits("٢٥٠٠٠٠٠٠") == "25000000"

    def test_normalises_eastern_arabic_indic(self):
        assert normalise_digits("۱۰") == "10"

    def test_detects_arabic_percentage(self):
        found = find_critical_values("لا تتجاوز ١٠٪ من قيمة العقد")
        assert any(m.kind is CriticalKind.PERCENTAGE for m in found)

    def test_detects_ascii_percentage(self):
        found = find_critical_values("shall not exceed 10% of the contract value")
        assert any(m.kind is CriticalKind.PERCENTAGE for m in found)


class TestCriticalValueDetection:
    @pytest.mark.parametrize(
        "text,kind",
        [
            ("SAR 25,000,000", CriticalKind.MONEY),
            ("25,000,000 ر.س", CriticalKind.MONEY),
            ("within 90 days", CriticalKind.DURATION),
            ("خلال ٣٠ يوماً", CriticalKind.DURATION),
            ("01/09/2026", CriticalKind.DATE),
            ("Clause 12.3", CriticalKind.CLAUSE_REFERENCE),
            ("البند ١٢.٣", CriticalKind.CLAUSE_REFERENCE),
        ],
    )
    def test_kinds(self, text, kind):
        assert any(m.kind is kind for m in find_critical_values(text))

    def test_plain_prose_has_none(self):
        assert not contains_critical_value("The Consultant shall provide services.")

    def test_longest_match_wins(self):
        """'SAR 25,000,000' should win over the bare number inside it."""
        found = find_critical_values("SAR 25,000,000")
        assert len(found) == 1


class TestValuesAgree:
    def test_same_values_agree_despite_prose_noise(self):
        assert values_agree(
            "damages shall not exceed 10% of value",
            "damages shalI not exceed 10% of value",   # OCR letter noise
        )

    def test_different_numbers_disagree(self):
        """The failure this whole mechanism exists to catch."""
        assert not values_agree("cap of 10%", "cap of 15%")

    def test_arabic_and_ascii_digits_agree(self):
        assert values_agree("١٠٪", "10%")


# ── quality gate ─────────────────────────────────────────────────────────

class TestQualityGate:
    @pytest.fixture(autouse=True)
    def _thresholds(self, settings):
        settings.OCR_THRESHOLDS = {
            "accept": 90, "validate": 80, "vision": 70, "critical_value": 90,
        }

    def test_high_confidence_accepts(self):
        page = make_page([("Consultant", 97), ("shall", 96), ("provide", 98)])
        assert evaluate_page(page).decision is GateDecision.ACCEPT

    def test_mid_band_validates(self):
        page = make_page([("Consultant", 85), ("shall", 86), ("provide", 84)])
        assert evaluate_page(page).decision is GateDecision.ACCEPT_WITH_VALIDATION

    def test_low_confidence_needs_vision(self):
        page = make_page([("Consultant", 75), ("shall", 74), ("provide", 76)])
        assert evaluate_page(page).decision is GateDecision.VISION_VERIFY

    def test_very_low_confidence_needs_retry(self):
        page = make_page([("Cnsltnt", 55), ("shll", 52), ("prvde", 58)])
        assert evaluate_page(page).decision is GateDecision.VISION_VERIFY_AND_RETRY

    def test_empty_page_is_worst_case(self):
        page = OCRPageResult(page_number=1, blocks=[], mean_confidence=0.0)
        result = evaluate_page(page)
        assert result.decision is GateDecision.VISION_VERIFY_AND_RETRY
        assert "no_text_recognised" in result.signals.reasons

    def test_critical_value_below_bar_flags_its_block_even_on_a_good_page(self):
        """The §9 scenario: page mean 96%, but '10%' is at 72%."""
        page = make_page([
            ("The", 99), ("cap", 98), ("is", 99), ("10%", 72),
            ("of", 99), ("value", 98),
        ])
        result = evaluate_page(page)
        assert result.signals.mean_confidence > 90          # page looks fine
        assert result.decision is GateDecision.ACCEPT       # …by the mean alone
        assert result.blocks_needing_vision == [0]          # …but the block is flagged
        assert result.needs_vision is True
        assert result.signals.critical_values_below_bar >= 1

    def test_confident_critical_value_is_not_flagged(self):
        page = make_page([("The", 99), ("cap", 98), ("is", 99), ("10%", 97)])
        result = evaluate_page(page)
        assert result.blocks_needing_vision == []
        assert result.needs_vision is False

    def test_malformed_characters_escalate_a_good_average(self):
        page = make_page([("Consul�tant", 96), ("sh�all", 97), ("a", 98)])
        result = evaluate_page(page)
        assert "malformed_characters" in result.signals.reasons
        assert result.decision is not GateDecision.ACCEPT

    def test_severity_is_never_downgraded(self):
        """A structural signal must not soften a low-confidence verdict."""
        page = make_page([("x�", 55), ("y", 52)])
        assert evaluate_page(page).decision is GateDecision.VISION_VERIFY_AND_RETRY

    def test_signals_are_recorded_for_the_page(self):
        page = make_page([("Consultant", 91), ("shall", 89)])
        signals = evaluate_page(page).signals.as_dict()
        for key in (
            "mean_confidence", "min_word_confidence", "low_confidence_word_ratio",
            "malformed_char_ratio", "numeric_corruption_count", "word_count",
        ):
            assert key in signals


def test_thresholds_are_configuration_not_constants(settings):
    """The same page must be judged differently under different thresholds.

    Under the default bar this page accepts outright; raise the bar and the
    identical page needs validation. Proves the gate reads configuration
    rather than baked-in constants.
    """
    page = make_page([("Consultant", 92), ("shall", 93), ("provide", 91)])

    settings.OCR_THRESHOLDS = {
        "accept": 90, "validate": 80, "vision": 70, "critical_value": 90,
    }
    assert evaluate_page(page).decision is GateDecision.ACCEPT

    settings.OCR_THRESHOLDS = {
        "accept": 95, "validate": 90, "vision": 85, "critical_value": 95,
    }
    assert evaluate_page(page).decision is GateDecision.ACCEPT_WITH_VALIDATION


# ── native PDF extraction, against a real file ───────────────────────────

@pytest.fixture
def sample_pdf(tmp_path):
    """A genuine two-page PDF with a realistic amount of contract text.

    Page length matters: the extractor treats a page with only a handful of
    characters as image-only, because scanned pages often carry a stray header.
    """
    import pymupdf

    body = [
        "Clause 12.3 Liquidated Damages",
        "The Consultant shall pay liquidated damages for any delay in the",
        "delivery of the Deliverables. Damages shall not exceed 10% of the",
        "total contract value. Such damages shall be the Client's exclusive",
        "remedy in respect of delay, and shall be deducted from amounts",
        "otherwise payable to the Consultant under Clause 5.1.",
    ]
    doc = pymupdf.open()
    page1 = doc.new_page()
    for i, line in enumerate(body):
        page1.insert_text((72, 100 + i * 18), line, fontsize=11)

    page2 = doc.new_page()
    tail = [
        "Clause 18.4 Governing Law and Jurisdiction",
        "This Agreement shall be governed by and construed in accordance",
        "with the laws of the Kingdom of Saudi Arabia. Any dispute arising",
        "out of or in connection with this Agreement shall be referred to",
        "the competent courts, subject to Clause 12.3 above.",
    ]
    for i, line in enumerate(tail):
        page2.insert_text((72, 100 + i * 18), line, fontsize=11)

    path = tmp_path / "contract.pdf"
    doc.save(str(path))
    doc.close()
    return path


class TestNativePdfExtraction:
    def test_extracts_both_pages(self, sample_pdf):
        parsed = extractors.extract_pdf(sample_pdf)
        assert parsed.page_count == 2
        assert len(parsed.pages) == 2

    def test_preserves_text_exactly(self, sample_pdf):
        """Source text is reproduced verbatim, including the critical value."""
        parsed = extractors.extract_pdf(sample_pdf)
        text = parsed.pages[0].text
        assert "Damages shall not exceed 10% of the" in text
        assert "total contract value." in text
        assert "Clause 12.3 Liquidated Damages" in text

    def test_critical_value_survives_extraction(self, sample_pdf):
        """The 10% cap must be detectable in what the extractor produced."""
        from apps.documents.critical_values import contains_critical_value

        parsed = extractors.extract_pdf(sample_pdf)
        assert contains_critical_value(parsed.pages[0].text)

    def test_blocks_carry_bounding_boxes(self, sample_pdf):
        """Without coordinates the source viewer cannot highlight anything."""
        parsed = extractors.extract_pdf(sample_pdf)
        block = parsed.pages[0].blocks[0]
        assert block.bbox is not None
        assert block.bbox.width > 0 and block.bbox.height > 0

    def test_rasterise_produces_a_png(self, sample_pdf):
        png = extractors.rasterise_page(sample_pdf, 1)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_crop_region_is_smaller_than_the_page(self, sample_pdf):
        """Vision must verify a region, not re-read the whole page (§10)."""
        parsed = extractors.extract_pdf(sample_pdf)
        bbox = parsed.pages[0].blocks[0].bbox.as_tuple()
        crop = extractors.crop_region(sample_pdf, 1, bbox)
        full = extractors.rasterise_page(sample_pdf, 1)
        assert crop[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(crop) < len(full)

    def test_page_with_text_needs_no_ocr(self, sample_pdf):
        parsed = extractors.extract_pdf(sample_pdf)
        assert parsed.pages[0].has_native_text is True

    def test_out_of_range_page_is_rejected(self, sample_pdf):
        with pytest.raises(extractors.ExtractionError):
            extractors.rasterise_page(sample_pdf, 99)

    def test_unsupported_type_is_rejected(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text("hello")
        with pytest.raises(extractors.ExtractionError, match="Unsupported file type"):
            extractors.extract(path)


class TestImageOnlyPdf:
    def test_page_without_text_is_marked_for_ocr(self, tmp_path):
        import pymupdf

        doc = pymupdf.open()
        doc.new_page()          # blank: no text layer
        path = tmp_path / "scan.pdf"
        doc.save(str(path))
        doc.close()

        parsed = extractors.extract_pdf(path)
        assert parsed.pages[0].has_native_text is False
        assert parsed.native_text_coverage == 0.0
        assert parsed.requires_ocr is True


class TestVisionOCRAvailability:
    """Vision OCR must not advertise itself when the provider cannot see.

    The provider being reachable says nothing about whether it can read a
    page, and a text-only model shown an image answers from the words alone
    -- so an engine that checked only health would show "ready" on the
    status board and verify nothing. Whether the deployment can see is one
    setting, `AJI_BRAIN_VISION_MODEL`: empty means no.
    """

    def test_unavailable_when_no_vision_model_is_configured(self):
        from django.test import override_settings

        from apps.documents.ocr import _cached, get_ocr_engine

        with override_settings(OCR_ENGINE="vision", AI_PROVIDER="ajibrain",
                               AJI_BRAIN_VISION_MODEL=""):
            _cached.cache_clear()
            ok, detail = get_ocr_engine().is_available()
        _cached.cache_clear()

        assert ok is False
        # Asserted on the untranslated part: the UI runs in Arabic, and what
        # matters is that the message names the fix, not its wording.
        assert "OCR_ENGINE=tesseract" in detail

    def test_the_provider_declares_the_capability_from_the_setting(self):
        """AJi Brain's general chat forwards images since 2026-09-27, so the
        wire format no longer decides; the configured vision model does."""
        from django.test import override_settings

        from apps.ai.providers.ajibrain import AjiBrainProvider

        with override_settings(AJI_BRAIN_WIRE_FORMAT="aji_brain", AJI_BRAIN_VISION_MODEL=""):
            assert AjiBrainProvider().supports_vision is False
        with override_settings(AJI_BRAIN_WIRE_FORMAT="aji_brain", AJI_BRAIN_VISION_MODEL="qwen3.8:27b"):
            assert AjiBrainProvider().supports_vision is True


class TestTesseractDiscovery:
    """Finding the binary must work on a machine nobody has configured.

    Tesseract is the only route from a scanned page to text now that vision
    OCR is withdrawn, and it is routinely installed per-user on a locked-down
    Windows host, where it is present but not on PATH. An install that exists
    must never look like one that does not.
    """

    def test_an_explicit_setting_wins(self):
        from django.test import override_settings

        from apps.documents.ocr.tesseract import find_tesseract

        with override_settings(TESSERACT_CMD=r"C:\somewhere\tesseract.exe"):
            assert find_tesseract() == r"C:\somewhere\tesseract.exe"

    def test_an_explicit_setting_is_not_second_guessed(self, monkeypatch):
        """A wrong path set by hand must produce an error naming that path,
        not a silent fallback to a different copy on the machine."""
        from django.test import override_settings

        from apps.documents.ocr import tesseract as mod

        monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/tesseract")
        with override_settings(TESSERACT_CMD="/typo/tesseract"):
            assert mod.find_tesseract() == "/typo/tesseract"

    def test_falls_back_to_path(self, monkeypatch):
        from django.test import override_settings

        from apps.documents.ocr import tesseract as mod

        monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/tesseract")
        with override_settings(TESSERACT_CMD=""):
            assert mod.find_tesseract() == "/usr/bin/tesseract"

    def test_searches_the_usual_install_locations(self, monkeypatch, tmp_path):
        """The per-user Windows install is the case that matters: no admin
        rights needed, so it is where a locked-down machine puts it."""
        from django.test import override_settings

        from apps.documents.ocr import tesseract as mod

        installed = tmp_path / "tesseract.exe"
        installed.write_text("")
        monkeypatch.setattr(mod.shutil, "which", lambda _n: None)
        monkeypatch.setattr(mod, "_candidate_paths",
                            lambda: [tmp_path / "absent.exe", installed])
        with override_settings(TESSERACT_CMD=""):
            assert mod.find_tesseract() == str(installed)

    def test_returns_none_when_nothing_is_installed(self, monkeypatch):
        from django.test import override_settings

        from apps.documents.ocr import tesseract as mod

        monkeypatch.setattr(mod.shutil, "which", lambda _n: None)
        monkeypatch.setattr(mod, "_candidate_paths", lambda: [])
        with override_settings(TESSERACT_CMD=""):
            assert mod.find_tesseract() is None


class TestOCRDeploymentCheck:
    """`manage.py check` must tell a fresh machine it cannot read scans."""

    def test_silent_when_the_engine_works(self):
        from apps.documents.checks import check_ocr_engine

        assert check_ocr_engine(None) == []

    def test_warns_when_the_binary_is_missing(self, monkeypatch):
        from django.test import override_settings

        from apps.documents.checks import W002, check_ocr_engine
        from apps.documents.ocr import _cached

        with override_settings(TESSERACT_CMD=r"C:\definitely\not\here.exe"):
            _cached.cache_clear()
            warnings = check_ocr_engine(None)
        _cached.cache_clear()

        assert [w.id for w in warnings] == [W002]
        # The warning has to be actionable, not merely accurate.
        assert "Tesseract" in warnings[0].hint

    def test_warns_on_an_unknown_engine_name(self):
        from django.test import override_settings

        from apps.documents.checks import W001, check_ocr_engine
        from apps.documents.ocr import _cached

        with override_settings(OCR_ENGINE="not-an-engine"):
            _cached.cache_clear()
            warnings = check_ocr_engine(None)
        _cached.cache_clear()

        assert [w.id for w in warnings] == [W001]
