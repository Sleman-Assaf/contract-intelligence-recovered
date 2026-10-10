"""OCR engine selection (§7).

`OCR_ENGINE` in settings picks the adapter. Adding PaddleOCR or Azure Document
Intelligence later means a new module here and one environment change — no
caller is bound to Tesseract.
"""
from __future__ import annotations

from functools import lru_cache

from django.conf import settings

from .base import (
    BBox,
    OCRBlock,
    OCREngine,
    OCREngineNotAvailable,
    OCRError,
    OCRLine,
    OCRPageResult,
    OCRWord,
)

_ENGINES: dict[str, str] = {
    "tesseract": "apps.documents.ocr.tesseract.TesseractEngine",
    # Fallback for hosts where no OCR binary can be installed. Lower trust by
    # design — see apps/documents/ocr/vision.py.
    "vision": "apps.documents.ocr.vision.VisionOCREngine",
}


@lru_cache(maxsize=None)
def _cached(name: str) -> OCREngine:
    from django.utils.module_loading import import_string

    try:
        dotted = _ENGINES[name]
    except KeyError as exc:
        raise OCREngineNotAvailable(
            f"Unknown OCR_ENGINE '{name}'. Known engines: {', '.join(sorted(_ENGINES))}."
        ) from exc
    return import_string(dotted)()


def get_ocr_engine(name: str | None = None) -> OCREngine:
    return _cached(name or settings.OCR_ENGINE)


def reset_engine_cache() -> None:
    _cached.cache_clear()


__all__ = [
    "BBox", "OCRBlock", "OCREngine", "OCREngineNotAvailable", "OCRError",
    "OCRLine", "OCRPageResult", "OCRWord",
    "get_ocr_engine", "reset_engine_cache",
]
