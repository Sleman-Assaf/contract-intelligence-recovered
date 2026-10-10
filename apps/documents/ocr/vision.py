"""OCR through the configured vision model — a fallback, not a preference (§7, §10).

This exists because Tesseract cannot be installed on every host: it needs
administrator rights, and this deployment has none. Rather than leaving scanned
contracts unreadable, the OCR abstraction takes another adapter.

It is deliberately marked as lower trust than a real OCR engine:

* A language model transcribing a page can hallucinate. Tesseract garbles text
  visibly; a model produces fluent, plausible, wrong text. Everything this
  engine returns is therefore capped below the auto-accept threshold, so the
  quality gate always routes it for verification.
* It returns no word-level boxes, so the source viewer can highlight the page
  but not a precise region. That limitation is recorded, not hidden.

Prefer Tesseract or PaddleOCR wherever they can be installed. §10 is explicit
that vision is not the primary OCR engine, and this adapter does not change
that — it only makes a scanned document readable at all on a host that cannot
run one.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils.translation import gettext as _

from .base import BBox, OCRBlock, OCREngine, OCREngineNotAvailable, OCRLine, OCRPageResult, OCRWord

logger = logging.getLogger(__name__)

#: Ceiling on reported confidence. Kept below OCR_THRESHOLD_ACCEPT so a page
#: read this way can never be auto-accepted without a second look.
MAX_REPORTED_CONFIDENCE = 75.0


class VisionOCREngine(OCREngine):
    name = "vision"

    def is_available(self) -> tuple[bool, str]:
        from apps.ai.registry import get_ai_provider

        try:
            provider = get_ai_provider()
        except Exception as exc:
            return False, _("No AI provider configured: %(error)s") % {"error": exc}

        # Reachable is not the same as able to see. The provider being up says
        # nothing about whether it can read a page, and a status board showing
        # "ready" for an engine that fails on every page is worse than one
        # showing it plainly as unavailable.
        if not provider.supports_vision:
            return False, _(
                "Vision OCR is unavailable: the %(provider)s provider cannot read "
                "images. Install Tesseract and set OCR_ENGINE=tesseract."
            ) % {"provider": provider.name}

        ok, detail = provider.health_check()
        if not ok:
            return False, _("Vision OCR needs a working AI provider. %(detail)s") % {"detail": detail}
        return True, _(
            "Vision OCR via %(provider)s. Fallback only - no word-level "
            "coordinates, and output is always routed for verification."
        ) % {"provider": provider.name}

    def recognise_page(
        self, image_bytes: bytes, *, page_number: int, languages: str
    ) -> OCRPageResult:
        from apps.ai.providers.base import AIProviderError, ImageRegion
        from apps.ai.services import ai_service

        available, detail = self.is_available()
        if not available:
            raise OCREngineNotAvailable(detail)

        language = "ar" if "ara" in (languages or "") else "en"

        try:
            call = ai_service.verify_text_region(
                region=ImageRegion(image_bytes=image_bytes, page_number=page_number),
                language=language,
            )
        except AIProviderError as exc:
            raise OCREngineNotAvailable(f"Vision OCR failed: {exc}") from exc

        text = (call.parsed.text or "").strip()
        if not text:
            return OCRPageResult(
                page_number=page_number, blocks=[], mean_confidence=0.0,
                language=language, engine=self.name,
            )

        # The model reports its own confidence; it is capped, because a
        # self-reported score is the one signal that cannot be checked (§24).
        confidence = min(float(call.parsed.confidence) * 100.0, MAX_REPORTED_CONFIDENCE)

        # One block per paragraph, with no real geometry. A synthetic full-page
        # box is used so downstream code has something consistent to store —
        # extraction_method records that it came from vision, so nothing
        # mistakes these for measured coordinates.
        blocks: list[OCRBlock] = []
        for index, paragraph in enumerate(p for p in text.split("\n\n") if p.strip()):
            words = [
                OCRWord(text=token, bbox=BBox(0, 0, 0, 0), confidence=confidence)
                for token in paragraph.split()
            ]
            line = OCRLine(
                text=paragraph.strip(), bbox=BBox(0, 0, 0, 0),
                confidence=confidence, words=words,
            )
            blocks.append(OCRBlock(
                text=paragraph.strip(), bbox=BBox(0, 0, 0, 0),
                confidence=confidence, lines=[line], reading_order=index,
            ))

        logger.info(
            "Vision OCR read page %s (%s blocks, capped confidence %.0f)",
            page_number, len(blocks), confidence,
        )
        return OCRPageResult(
            page_number=page_number, blocks=blocks,
            mean_confidence=confidence, language=language, engine=self.name,
        )
