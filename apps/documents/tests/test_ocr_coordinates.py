"""OCR boxes are stored in PDF points, and a crop shows the text it names.

Tesseract reads a page rendered at 180 dpi and reports pixels of that image.
They were stored as they came, while everything that reads a box -- the vision
crop, the source viewer, native text -- works in PDF points (72 per inch). On
a scanned A4 page the "4.3 Delay Damages" line was stored at y = 1832-1932 on
a page 842 points tall: the crop failed with a renderer error nothing caught,
and boxes higher up the page cropped the wrong text.
"""
from __future__ import annotations

import math

import pymupdf
import pytest

from apps.documents import extractors
from apps.documents.extractors import ExtractionError, OCR_RENDER_DPI, crop_region, pixels_to_points
from apps.documents.ocr import get_ocr_engine

LINE = "Clause 4.3 Delay damages of 0.5% per week"


def _engine_or_skip():
    engine = get_ocr_engine()
    ok, detail = engine.is_available()
    if not ok:
        pytest.skip(f"OCR engine not available: {detail}")
    return engine


@pytest.fixture
def scanned_pdf(tmp_path):
    """An image-only page -- a scan -- with one known line at a known place,
    and where that line sits in points on the original."""
    original = pymupdf.open()
    page = original.new_page(width=595, height=842)
    page.insert_text((72, 520), LINE, fontsize=16)
    where = page.search_for("Delay damages")[0]            # points
    pix = page.get_pixmap(dpi=150)
    scan = pymupdf.open()
    scan_page = scan.new_page(width=595, height=842)
    scan_page.insert_image(scan_page.rect, pixmap=pix)
    path = tmp_path / "scan.pdf"
    scan.save(str(path))
    scan.close(); original.close()
    return path, where


class TestTheConversion:
    def test_pixels_of_a_180_dpi_render_become_points(self):
        assert pixels_to_points((180, 360, 540, 720)) == pytest.approx((72, 144, 216, 288))

    def test_any_render_resolution(self):
        assert pixels_to_points((300, 300, 600, 600), dpi=300) == pytest.approx((72, 72, 144, 144))

    def test_the_formula(self):
        box = (162.0, 1832.0, 1325.0, 1932.0)                 # the measured case
        converted = pixels_to_points(box)
        assert converted == pytest.approx(tuple(v * 72 / 180 for v in box))
        assert converted[3] < 842                              # now on the page


class TestTheCropShowsTheText:
    def test_the_stored_box_lands_on_the_line_it_came_from(self, scanned_pdf):
        engine = _engine_or_skip()
        path, where = scanned_pdf
        image = extractors.rasterise_page(path, 1, dpi=OCR_RENDER_DPI)
        result = engine.recognise_page(image, page_number=1, languages="eng")
        block = next(b for b in result.blocks if "Delay" in b.text)
        box = pixels_to_points(block.bbox.as_tuple())
        # The converted box overlaps where the text really is on the page.
        overlap = pymupdf.Rect(box) & where
        assert not overlap.is_empty and overlap.get_area() > 0.5 * where.get_area()

    def test_reading_the_crop_gives_back_the_same_words(self, scanned_pdf):
        engine = _engine_or_skip()
        path, _where = scanned_pdf
        image = extractors.rasterise_page(path, 1, dpi=OCR_RENDER_DPI)
        result = engine.recognise_page(image, page_number=1, languages="eng")
        block = next(b for b in result.blocks if "Delay" in b.text)
        crop = crop_region(path, 1, pixels_to_points(block.bbox.as_tuple()))
        again = engine.recognise_page(crop, page_number=1, languages="eng")
        text = " ".join(b.text for b in again.blocks)
        assert "Delay" in text and "0.5%" in text

    def test_the_pipeline_stores_points(self, scanned_pdf, db):
        """Through `ocr_page` itself: every stored OCR box is on the page."""
        _engine_or_skip()
        from apps.accounts.models import Country, Entity, Role, User
        from apps.contracts.models import Contract, ContractType
        from apps.documents.models import Document, Page, SourceFile
        from apps.documents.services import ocr_page

        path, where = scanned_pdf
        entity = Entity.objects.create(name="E", code="e", country=Country.JORDAN)
        user = User.objects.create_user(email="u@aji.test", password="x-long-passphrase-42",
                                        role=Role.CONTRACT_REVIEWER, entity=entity)
        contract = Contract.objects.create(project_name="P", contract_title="T", client_name="C",
                                           country=Country.JORDAN, entity=entity,
                                           contract_type=ContractType.objects.create(code="x", name="X"),
                                           uploaded_by=user)
        source = SourceFile.objects.create(contract=contract, original_filename="scan.pdf",
                                           stored_path="scan.pdf", content_type="application/pdf",
                                           size_bytes=1, sha256="0" * 64)
        document = Document.objects.create(source_file=source, contract=contract)
        page = Page.objects.create(document=document, page_number=1, width=595, height=842,
                                   extraction_method="ocr")
        import apps.documents.services as services
        original = services.absolute_path
        services.absolute_path = lambda _p: path
        try:
            ocr_page(document, page, languages="eng")
        finally:
            services.absolute_path = original
        blocks = list(page.blocks.all())
        assert blocks
        for b in blocks:
            assert 0 <= b.bbox_x0 < b.bbox_x1 <= 595 and 0 <= b.bbox_y0 < b.bbox_y1 <= 842
        line = next(b for b in blocks if "Delay" in b.original_source_text)
        assert not (pymupdf.Rect(line.bbox) & where).is_empty


class TestABadRegionIsRefusedNotCrashed:
    @pytest.mark.parametrize("box", [
        (162, 1832, 1325, 1932),          # the old pixel box: below the page
        (700, 100, 900, 200),             # right of the page
        (100, 100, 100, 200),             # zero width
        (100, 200, 300, 100),             # upside down
        (math.nan, 1, 2, 3),
        ("a", 1, 2, 3),
    ])
    def test_crop_region(self, scanned_pdf, box):
        path, _where = scanned_pdf
        with pytest.raises(ExtractionError):
            crop_region(path, 1, box)

    def test_a_page_that_does_not_exist(self, scanned_pdf):
        path, _where = scanned_pdf
        with pytest.raises(ExtractionError):
            crop_region(path, 9, (10, 10, 50, 50))

    def test_crop_image(self):
        import io

        from PIL import Image

        buf = io.BytesIO(); Image.new("RGB", (200, 100), "white").save(buf, "PNG")
        assert extractors.crop_image(buf.getvalue(), (10, 10, 60, 40)).startswith(b"\x89PNG")
        with pytest.raises(ExtractionError):
            extractors.crop_image(buf.getvalue(), (500, 500, 600, 600))
