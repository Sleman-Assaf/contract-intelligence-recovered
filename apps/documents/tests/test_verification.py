"""Three-tier OCR / vision verification (apps/documents/verification.py).

The decision is tested on its own, with the crop and the vision reader
supplied by the test: what each tier requires, what it refuses, and that a
critical value no reader could settle is never chosen.
"""
from __future__ import annotations

import inspect

import pytest

from apps.documents.critical_values import CriticalKind, critical_signature, find_critical_values, values_agree
from apps.documents.extractors import ExtractionError
from apps.documents.models import VerificationStatus as S
from apps.documents.verification import (
    PassRecord,
    Region,
    VisionBudget,
    normalise_for_compare,
    priority,
    reconcile,
    similarity,
)

OCR_LINE = "4.3 Delay Damages: the Consultant shall pay 0.5% per week, with no maximum limit."


@pytest.fixture(autouse=True)
def thresholds(settings):
    settings.VISION_AUTO_RESOLVE_SIMILARITY = 0.97
    settings.VISION_REVERIFY_OCR_CONFIDENCE = 80.0
    settings.VISION_MAX_CALLS_PER_PAGE = 8
    settings.VISION_MAX_CALLS_PER_FILE = 30


def region(text=OCR_LINE, *, word_conf=None, min_conf=95.0, bbox=(10, 10, 300, 30)):
    return Region(ocr_text=text, page_number=1, bbox=bbox, ocr_confidence=min_conf,
                  min_word_confidence=min_conf, word_confidences=word_conf or [], language="en")


class Reader:
    """A vision reader that answers pass 1 and pass 2 from a script and
    records what it was asked."""

    def __init__(self, *answers, error=""):
        self.answers = list(answers)
        self.error = error
        self.calls = []

    def __call__(self, image, strict, number):
        self.calls.append({"image": image, "strict": strict, "number": number})
        if self.error:
            return PassRecord(number=number, error=self.error)
        return PassRecord(number=number, text=self.answers[number - 1], confidence=0.9,
                          provider="fake", model="v")


class Crops:
    def __init__(self, fail_tight=False, fail=False):
        self.asked = []
        self.fail_tight, self.fail = fail_tight, fail

    def __call__(self, tight):
        self.asked.append(tight)
        if self.fail or (tight and self.fail_tight):
            raise ExtractionError("outside the page")
        return b"tight" if tight else b"crop"


class TestTheTiers:
    def test_the_same_reading_is_verified(self):
        read = Reader(OCR_LINE)
        r = reconcile(region(), Crops(), read)
        assert r.status == S.VERIFIED and r.final_text == OCR_LINE
        assert len(read.calls) == 1 and read.calls[0]["strict"] is False

    def test_harmless_differences_still_count_as_the_same(self):
        # Case, quotes and spacing are normalised away before comparing.
        r = reconcile(region(), Crops(), Reader("4.3  Delay damages: the consultant shall pay "
                                               "0.5% per week, with no maximum limit"))
        assert r.status == S.VERIFIED

    def test_a_minor_non_critical_difference_is_auto_resolved_and_logged(self):
        vision = OCR_LINE.replace("Consultant", "Consuitant")
        read = Reader(vision)
        r = reconcile(region(), Crops(), read)
        assert r.status == S.AUTO_RESOLVED
        assert r.final_text == OCR_LINE                        # OCR's text stands
        assert r.mismatch_reason.startswith("minor_difference")  # and the difference is on record
        assert r.passes[0].text == vision
        assert len(read.calls) == 1                            # no second look for this

    def test_a_large_non_critical_difference_is_not_auto_resolved(self):
        r = reconcile(region(), Crops(), Reader("4.3 Something else entirely 0.5% per week, no limit at all"))
        assert r.status == S.NEEDS_HUMAN_REVIEW and r.final_text == ""
        assert r.mismatch_reason.startswith("text_mismatch")

    def test_the_threshold_is_configuration(self, settings):
        settings.VISION_AUTO_RESOLVE_SIMILARITY = 0.999
        r = reconcile(region(), Crops(), Reader(OCR_LINE.replace("Consultant", "Consuitant")))
        assert r.status == S.NEEDS_HUMAN_REVIEW


class TestACriticalMismatch:
    VISION_5 = OCR_LINE.replace("0.5%", "5%")

    def test_triggers_a_second_stricter_pass_on_a_tighter_crop(self):
        crops, read = Crops(), Reader(self.VISION_5, OCR_LINE)
        reconcile(region(word_conf=[["0.5%", 60.0]]), crops, read)
        assert crops.asked == [False, True]                    # the tight crop, second
        assert [c["strict"] for c in read.calls] == [False, True]
        assert read.calls[1]["image"] == b"tight"

    def test_pass_2_confirming_ocr_where_ocr_was_unsure_is_reverified(self):
        r = reconcile(region(word_conf=[["0.5%", 60.0]]), Crops(), Reader(self.VISION_5, OCR_LINE))
        assert r.status == S.VISION_REVERIFIED
        assert r.final_text == OCR_LINE
        assert "pass 2 confirmed OCR" in r.mismatch_reason
        assert [p.text for p in r.passes] == [self.VISION_5, OCR_LINE]   # all evidence kept

    def test_pass_2_confirming_pass_1_where_ocr_was_unsure_is_reverified(self):
        r = reconcile(region(word_conf=[["0.5%", 55.0]]), Crops(), Reader(self.VISION_5, self.VISION_5))
        assert r.status == S.VISION_REVERIFIED
        assert r.final_text == self.VISION_5

    def test_two_agreeing_vision_passes_do_not_beat_a_confident_ocr(self):
        """Not "vision 1 + vision 2 agree, so vision is right"."""
        r = reconcile(region(word_conf=[["0.5%", 96.0]]), Crops(), Reader(self.VISION_5, self.VISION_5))
        assert r.status == S.NEEDS_HUMAN_REVIEW
        assert r.final_text == ""                              # no value is chosen
        assert "OCR was confident" in r.mismatch_reason

    def test_pass_2_matching_neither_reading_goes_to_a_person(self):
        r = reconcile(region(word_conf=[["0.5%", 50.0]]), Crops(),
                      Reader(self.VISION_5, OCR_LINE.replace("0.5%", "0.05%")))
        assert r.status == S.NEEDS_HUMAN_REVIEW and r.final_text == ""
        assert "matched neither" in r.mismatch_reason

    def test_an_unreadable_character_in_pass_2_goes_to_a_person(self):
        r = reconcile(region(word_conf=[["0.5%", 50.0]]), Crops(),
                      Reader(self.VISION_5, OCR_LINE.replace("0.5%", "0.[?]%")))
        assert r.status == S.NEEDS_HUMAN_REVIEW

    def test_the_word_confidence_used_is_the_disputed_words(self):
        # The block's worst word is a harmless "the" at 40; the value was read at 97.
        r = reconcile(region(word_conf=[["the", 40.0], ["0.5%", 97.0]], min_conf=40.0), Crops(),
                      Reader(self.VISION_5, self.VISION_5))
        assert r.status == S.NEEDS_HUMAN_REVIEW

    def test_without_word_confidences_the_worst_word_is_used(self):
        r = reconcile(region(min_conf=60.0), Crops(), Reader(self.VISION_5, OCR_LINE))
        assert r.status == S.VISION_REVERIFIED

    def test_an_invalid_tight_crop_goes_to_a_person(self):
        r = reconcile(region(word_conf=[["0.5%", 60.0]]), Crops(fail_tight=True),
                      Reader(self.VISION_5, OCR_LINE))
        assert r.status == S.NEEDS_HUMAN_REVIEW and "invalid_crop" in r.mismatch_reason
        assert r.calls == 1


class TestNothingBreaksOnTheWay:
    def test_an_invalid_crop_is_refused_not_crashed(self):
        read = Reader(OCR_LINE)
        r = reconcile(region(), Crops(fail=True), read)
        assert r.status == S.NEEDS_HUMAN_REVIEW and r.mismatch_reason.startswith("invalid_crop")
        assert read.calls == []

    def test_no_bounding_box(self):
        r = reconcile(region(bbox=None), Crops(), Reader(OCR_LINE))
        assert r.status == S.NEEDS_HUMAN_REVIEW and r.mismatch_reason == "no_bounding_box"

    def test_the_provider_being_down_keeps_the_ocr_text(self):
        r = reconcile(region(), Crops(), Reader(error="AI server unavailable"))
        assert r.status == S.NEEDS_HUMAN_REVIEW
        assert r.final_text == ""                 # so usable text falls back to OCR
        assert "vision_unavailable" in r.mismatch_reason

    def test_native_text_is_never_sent(self):
        read = Reader(OCR_LINE)
        subject = region(); subject.is_native = True
        r = reconcile(subject, Crops(), read)
        assert r.status == S.ACCEPTED and read.calls == []


class TestTheLimit:
    def test_past_the_limit_nothing_is_sent_and_the_region_goes_to_review(self, settings):
        budget = VisionBudget(per_page=2, per_file=10)
        read = Reader(OCR_LINE)
        statuses = [reconcile(region(), Crops(), read, budget).status for _ in range(4)]
        assert statuses == [S.VERIFIED, S.VERIFIED, S.NEEDS_HUMAN_REVIEW, S.NEEDS_HUMAN_REVIEW]
        assert len(read.calls) == 2

    def test_a_second_pass_counts_against_the_limit(self):
        budget = VisionBudget(per_page=1, per_file=10)
        r = reconcile(region(word_conf=[["0.5%", 60.0]]), Crops(), Reader(TestACriticalMismatch.VISION_5, OCR_LINE), budget)
        assert r.status == S.NEEDS_HUMAN_REVIEW and "vision_limit" in r.mismatch_reason

    def test_critical_values_go_first_then_the_least_confident(self):
        order = sorted([(False, 50.0, 50.0), (True, 85.0, 85.0), (True, 60.0, 60.0), (False, 10.0, 10.0)],
                       key=lambda t: priority(*t))
        assert order == [(True, 60.0, 60.0), (True, 85.0, 85.0), (False, 10.0, 10.0), (False, 50.0, 50.0)]


class TestTheVisionPromptIsIndependent:
    def test_the_prompt_carries_no_ocr_reading(self):
        from apps.ai import prompts

        assert "ocr_text" not in inspect.signature(prompts.verify_text_region).parameters
        text = prompts.verify_text_region(language="en")
        strict = prompts.verify_text_region(language="en", strict=True)
        for p in (text, strict):
            assert "OCR PRODUCED" not in p and "matches_ocr" not in p
            assert "Literal transcription" in p
        assert "[?]" in strict and "[?]" not in text.split("Rules:")[0]

    def test_no_layer_can_pass_it_either(self):
        from apps.ai.providers.ajibrain import AjiBrainProvider
        from apps.ai.providers.base import AIProvider
        from apps.ai.services import AIService

        for fn in (AIProvider.verify_text_region, AjiBrainProvider.verify_text_region,
                   AIService.verify_text_region):
            assert "ocr_text" not in inspect.signature(fn).parameters

    def test_what_reaches_the_provider(self, monkeypatch):
        """Through the pipeline's own reader: the image and the language, nothing of OCR."""
        from apps.ai.providers.base import AICallResult
        from apps.ai.schemas import TextRegionVerification
        from apps.ai.services import ai_service
        from apps.documents.verification import vision_reader

        seen = {}

        def fake(**kwargs):
            seen.update(kwargs)
            return AICallResult(parsed=TextRegionVerification(text="x", confidence=0.9),
                                provider="f", model="m", request_type="verify_text_region",
                                latency_ms=1, prompt_tokens=1, completion_tokens=1, raw_text="{}")

        monkeypatch.setattr(ai_service, "verify_text_region", fake)
        vision_reader(language="en", page_number=1, bbox=(1, 2, 3, 4))(b"png", True, 2)
        assert set(seen) <= {"region", "language", "strict", "contract_id", "analysis_run"}
        assert seen["strict"] is True and seen["region"].image_bytes == b"png"


class TestComparing:
    def test_arabic_variants_normalise(self):
        assert normalise_for_compare("إلتزامات الإستشاري") == normalise_for_compare("التزامات الاستشاري")
        assert normalise_for_compare("مَسْؤُولِيَّة") == normalise_for_compare("مسؤولية")
        assert normalise_for_compare("٠٫٥٪") == normalise_for_compare("0.5%")

    def test_values_are_not_normalised_away(self):
        assert normalise_for_compare("0.5%") != normalise_for_compare("5%")
        assert normalise_for_compare("12-3") != normalise_for_compare("123")

    def test_similarity(self):
        assert similarity("abc", "abc") == 1.0
        assert similarity("", "") == 1.0
        assert similarity("Delay damages", "Delay damagas") > 0.9


class TestNewCriticalValues:
    @pytest.mark.parametrize("text,kind", [
        ("Agreement No. TDT-AJI-2026-041", CriticalKind.CONTRACT_NUMBER),
        ("رقم العقد: ٤٥/٢٠٢٦", CriticalKind.CONTRACT_NUMBER),
        ("a performance guarantee of SAR 250,000", CriticalKind.GUARANTEE),
        ("خطاب ضمان بقيمة 50,000 دينار", CriticalKind.GUARANTEE),
        ("TempData Technologies L.L.C.", CriticalKind.PARTY),
        ("شركة البيانات المؤقتة ذ.م.م", CriticalKind.PARTY),
        ("4.3 Delay Damages", CriticalKind.CLAUSE_REFERENCE),
    ])
    def test_kinds(self, text, kind):
        assert kind in {m.kind for m in find_critical_values(text)}

    @pytest.mark.parametrize("text", [
        "The Company shall notify the Consultant.",
        "Each Party Ltd. obligations continue.",
        "The Consultant shall act in good faith.",
        "1. General",
    ])
    def test_roles_and_prose_are_not_parties_or_numbers(self, text):
        kinds = {m.kind for m in find_critical_values(text)}
        assert CriticalKind.PARTY not in kinds and CriticalKind.CONTRACT_NUMBER not in kinds

    def test_known_parties_from_the_contract_come_first(self):
        assert CriticalKind.PARTY in {m.kind for m in find_critical_values(
            "payment to Ministry of Culture", known_parties=["Ministry of Culture"])}
        assert not values_agree("paid to Ministry of Culture", "paid to Ministry of Cultune",
                                known_parties=["Ministry of Culture"])

    def test_a_changed_party_name_is_a_critical_difference(self):
        assert not values_agree("TempData Technologies L.L.C.", "TempDota Technologies L.L.C.")
        assert values_agree("TempData Technologies L.L.C.", "TempData Technologies LLC")

    def test_found_in_a_live_scan(self):
        """Each of these slipped through on a real scanned page."""
        # A reference as OCR mangled it, read at 47% and never checked.
        assert CriticalKind.CONTRACT_NUMBER in {m.kind for m in find_critical_values(
            "707-401-2026-041 - Engineering Consultancy Services Agreement")}
        assert {m.kind for m in find_critical_values("Due on 2026-09-01.")} == {CriticalKind.DATE}
        # "Adi" for "AJi" was auto-resolved as a minor difference.
        assert not values_agree("TempData Technologies L.L.C. / Adi Engineering Consultants",
                                "TempData Technologies L.L.C. / AJi Engineering Consultants")
        assert not find_critical_values("Engineering Consultants shall attend")
        # "12 Monthly" is not a period of twelve months.
        assert not find_critical_values("12 Monthly progress payments")
        # Prose after a label is not a number.
        assert not find_critical_values("the contract number was extracted via OCR")

    def test_a_confident_misread_name_is_not_reverified(self):
        """OCR read "Adi" at 96: two vision passes saying "AJi" do not
        overrule it, whatever a stray ':' was read at."""
        ocr = "TempData Technologies L.L.C. / Adi Engineering Consultants : Page 4"
        vision = "TempData Technologies L.L.C. / AJi Engineering Consultants Page 4"
        words = [["TempData", 80.0], ["Technologies", 93.0], ["Adi", 96.0], ["Engineering", 96.0],
                 ["Consultants", 92.0], [":", 3.0], ["Page", 94.0]]
        r = reconcile(region(ocr, word_conf=words, min_conf=3.0), Crops(), Reader(vision, vision))
        assert r.status == S.NEEDS_HUMAN_REVIEW and "OCR was confident (92)" in r.mismatch_reason

    def test_a_disputed_value_is_held_wherever_else_it_appears(self):
        from apps.documents.verification import disputed_values, shares_a_disputed_value

        disputed = disputed_values([("Adi Engineering Consultants — Page 4",
                                     "AJi Engineering Consultants Page 4", 2)])
        assert shares_a_disputed_value("TempData / Adi Engineering Consultants - Page 3", disputed)
        assert not shares_a_disputed_value("Invoices within 30 days", disputed)

    def test_a_contract_number_compares_on_its_code(self):
        assert values_agree("Agreement No. TDT-AJI-2026-041", "Agreement No TDT-AJI-2026-041")
        assert not values_agree("Agreement No. TDT-AJI-2026-041", "Agreement No. TDT-AJI-2026-047")
        assert critical_signature("Agreement No. TDT-AJI-2026-041")
