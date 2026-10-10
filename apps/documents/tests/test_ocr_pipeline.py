"""Preparing scanned pages before OCR (apps/documents/ocr/pipeline.py).

Each case is one measured on real signed contracts: a fee table scanned
sideways lost every figure; the blue company stamp on every page was read as
text; an email printed with its replies in blue lost them to the stamp
remover; the Arabic model read "180" as "0"; Tesseract's layout skipped the
delay-damages line altogether.
"""
from __future__ import annotations

import io

import pymupdf
import pytest
from PIL import Image, ImageDraw

from apps.documents.ocr import get_ocr_engine
from apps.documents.ocr.base import BBox, OCRBlock, OCRLine, OCRPageResult, OCRWord
from apps.documents.ocr.pipeline import (
    Transform,
    _reread_line,
    _segments,
    mask_stamp_ink,
    prepare_image,
    read_page,
)


def _engine_or_skip():
    engine = get_ocr_engine()
    ok, detail = engine.is_available()
    if not ok:
        pytest.skip(f"OCR engine not available: {detail}")
    return engine


def _png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class TestCoordinates:
    """A box read on the turned image is stored where it is on the page."""

    @pytest.mark.parametrize("rotate", [0, 90, 180, 270])
    @pytest.mark.parametrize("skew", [0.0, 1.5])
    def test_a_marker_maps_back_to_where_it_was(self, rotate, skew):
        w, h, dpi = 600, 800, 72
        page = Image.new("RGB", (w, h), "white")
        ImageDraw.Draw(page).rectangle((100, 200, 140, 230), fill="black")
        turned = page.rotate(-rotate, expand=True, fillcolor="white")
        upright = turned.size
        final = turned.rotate(skew, expand=True, fillcolor="white") if skew else turned
        # Where the marker is in the image the engine would read.
        gray = final.convert("L").point(lambda v: 255 if v < 128 else 0)
        found = gray.getbbox()
        transform = Transform(dpi=dpi, rendered_size=(w, h), rotate=rotate,
                              upright_size=upright, skew=skew, final_size=final.size)
        x0, y0, x1, y1 = transform.to_points(found)
        assert x0 == pytest.approx(100, abs=3)
        assert y0 == pytest.approx(200, abs=3)
        assert x1 == pytest.approx(141, abs=3)
        assert y1 == pytest.approx(231, abs=3)

    def test_pixels_become_points(self):
        transform = Transform(dpi=300, rendered_size=(2480, 3508), final_size=(2480, 3508),
                              upright_size=(2480, 3508))
        assert transform.to_points((300, 300, 600, 600)) == pytest.approx((72, 72, 144, 144))


class TestStampInk:
    def _page(self):
        image = Image.new("RGB", (1200, 1600), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((100, 100, 1100, 120), fill=(20, 20, 20))          # black text line
        draw.rectangle((100, 300, 1100, 320), fill=(40, 90, 200))         # a line of blue text
        draw.ellipse((800, 1300, 1050, 1500), outline=(120, 150, 220), width=18)   # the stamp
        draw.rectangle((100, 500, 600, 520), fill=(25, 40, 90))           # navy heading
        return image

    def test_the_stamp_goes(self):
        cleaned, share = mask_stamp_ink(self._page())
        assert share > 0
        assert cleaned.getpixel((925, 1310)) == (255, 255, 255)

    def test_black_text_stays(self):
        cleaned, _ = mask_stamp_ink(self._page())
        assert cleaned.getpixel((500, 110)) == (20, 20, 20)

    def test_a_line_of_blue_text_stays(self):
        # Blue runs across the page; a stamp is a compact patch.
        cleaned, _ = mask_stamp_ink(self._page())
        assert cleaned.getpixel((500, 310)) != (255, 255, 255)

    def test_dark_coloured_headings_stay(self):
        cleaned, _ = mask_stamp_ink(self._page())
        assert cleaned.getpixel((300, 510)) == (25, 40, 90)

    def test_a_page_without_colour_is_untouched(self):
        image = Image.new("RGB", (300, 300), "white")
        ImageDraw.Draw(image).rectangle((10, 10, 200, 30), fill="black")
        cleaned, share = mask_stamp_ink(image)
        assert share == 0
        assert cleaned.getpixel((50, 20)) == (0, 0, 0)


class TestSideways:
    @pytest.fixture
    def sideways_scan(self, tmp_path):
        """An image-only page whose text runs bottom to top -- a landscape
        table scanned in portrait."""
        text = pymupdf.open()
        page = text.new_page(width=842, height=595)
        for i, line in enumerate(["Total Amount of Services 690.945,38 EUR",
                                  "Expert 5 Energy Processes 558 days 382,50",
                                  "Reimbursable costs per diem 38 EUR per day"]):
            page.insert_text((72, 120 + 60 * i), line, fontsize=20)
        pix = page.get_pixmap(dpi=150)
        landscape = Image.open(io.BytesIO(pix.tobytes("png"))).rotate(90, expand=True)
        scan = pymupdf.open()
        scan_page = scan.new_page(width=595, height=842)
        scan_page.insert_image(scan_page.rect, stream=_png(landscape))
        path = tmp_path / "sideways.pdf"
        scan.save(str(path))
        return path

    def test_the_page_is_turned_and_read(self, sideways_scan, settings):
        _engine_or_skip()
        settings.OCR_RENDER_DPI = 200
        reading = read_page(sideways_scan, 1)
        assert reading.transform.rotate in (90, 270)
        assert "690.945,38" in reading.text.replace(" ", "")

    def test_boxes_stay_on_the_page(self, sideways_scan, settings):
        _engine_or_skip()
        settings.OCR_RENDER_DPI = 200
        reading = read_page(sideways_scan, 1)
        for block in reading.result.blocks:
            x0, y0, x1, y1 = block.bbox.as_tuple()
            assert -2 <= x0 < x1 <= 597 and -2 <= y0 < y1 <= 844

    def test_rotation_can_be_switched_off(self, sideways_scan, settings):
        _engine_or_skip()
        settings.OCR_AUTO_ROTATE = False
        settings.OCR_RENDER_DPI = 150
        assert read_page(sideways_scan, 1).transform.rotate == 0


class _StubEngine:
    """Returns pre-set words for a crop, as Tesseract would."""

    name = "tesseract"

    def __init__(self, words):
        self.words = words

    def is_available(self):
        return True, ""

    def recognise_page(self, image_bytes, *, page_number, languages, single_line=False):
        line = OCRLine(text=" ".join(w.text for w in self.words), bbox=BBox(0, 0, 10, 10),
                       confidence=90, words=self.words)
        return OCRPageResult(page_number=page_number, mean_confidence=90,
                             blocks=[OCRBlock(text=line.text, bbox=line.bbox, confidence=90, lines=[line])])


class TestSecondReading:
    def test_a_figure_the_english_model_reads_differently_is_replaced(self):
        # The Arabic reading: "مدة العقد: 0 يوما" with the "0" at x 1609-1657.
        words = [OCRWord("مدة", BBox(2254, 1946, 2315, 1977), 92),
                 OCRWord("العقد:", BBox(2118, 1945, 2234, 1979), 92),
                 OCRWord("0", BBox(1609, 1944, 1657, 1981), 95),
                 OCRWord("يوما", BBox(1505, 1950, 1600, 1980), 89)]
        line = OCRLine(text=" ".join(w.text for w in words), bbox=BBox(1387, 1935, 2315, 1994),
                       confidence=90, words=words)
        image = Image.new("RGB", (2480, 3508), "white")
        # The window around the figure starts 3.5 line heights to its left; the
        # stub's word positions are relative to that crop, scaled to 30 px.
        english = _StubEngine([OCRWord("180", BBox(0, 0, 0, 0), 90)])
        height = line.bbox.height
        scale = 30 / height
        left = 1609 - 3.5 * height
        english.words = [OCRWord("180", BBox((1590 - left) * scale, 5, (1660 - left) * scale, 25), 90)]
        assert _reread_line(image, line, english, 1) == [(2, "180")]

    def test_the_same_figure_is_left_alone(self):
        words = [OCRWord("مدة", BBox(300, 10, 340, 40), 92), OCRWord("365", BBox(200, 10, 260, 40), 92)]
        line = OCRLine(text="مدة 365", bbox=BBox(200, 10, 340, 40), confidence=92, words=words)
        image = Image.new("RGB", (600, 100), "white")
        scale = 30 / line.bbox.height
        left = 200 - 3.5 * line.bbox.height
        english = _StubEngine([OCRWord("365", BBox((200 - left) * scale, 2, (260 - left) * scale, 28), 90)])
        assert _reread_line(image, line, english, 1) == []

    def test_gaps_split_a_line_into_pieces(self):
        assert _segments([1, 2, 3, 10, 11, 200, 201], gap=50) == [(1, 11), (200, 201)]


class TestPrepare:
    def test_prepare_records_what_it_did(self, settings):
        _engine_or_skip()
        settings.OCR_AUTO_ROTATE = True
        image = Image.new("RGB", (800, 1000), "white")
        _, transform, orientation, share = prepare_image(_png(image), dpi=100)
        assert transform.rotate == 0 and share == 0
