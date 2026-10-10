"""Deployment checks for text extraction (§8).

Reported by `manage.py check`, which runs as part of `runserver` and of any
sane deployment script, so a machine missing a dependency says so at start-up
rather than on the first scanned contract somebody uploads.

These are warnings, not errors. A deployment handling only digital PDFs never
touches OCR and must still be able to start; one handling scans needs to know
immediately that it cannot read them.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning as DjangoWarning

W001 = "documents.W001"
W002 = "documents.W002"


def check_ocr_engine(app_configs, **kwargs):
    """Warn when the configured OCR engine cannot actually run.

    Since vision OCR was withdrawn -- AJi Brain's endpoint discards image
    attachments -- Tesseract is the only path from a scanned page to text.
    There is no longer a fallback to absorb a missing install.
    """
    from .ocr import get_ocr_engine

    try:
        engine = get_ocr_engine()
    except Exception as exc:  # an unknown OCR_ENGINE name
        return [DjangoWarning(
            f"OCR_ENGINE={settings.OCR_ENGINE!r} could not be loaded: {exc}",
            hint="Set OCR_ENGINE=tesseract.",
            id=W001,
        )]

    available, detail = engine.is_available()
    if available:
        return []

    return [DjangoWarning(
        f"OCR engine {engine.name!r} is not usable: {detail}",
        hint=(
            "Scanned PDFs cannot be read until this is fixed; documents with a "
            "native text layer are unaffected. On Windows install Tesseract from "
            "https://github.com/UB-Mannheim/tesseract/wiki -- preferably "
            "machine-wide under Program Files so every account and service can "
            "see it -- and include the Arabic language data. Set TESSERACT_CMD "
            "only if it is installed somewhere unusual."
        ),
        id=W002,
    )]
