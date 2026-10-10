"""The split-screen source viewer (§26).

A finding cites "clause 12.3, page 14". This is what makes that checkable: the
page is rendered beside the finding with the cited region highlighted, and the
panel states how the text was obtained — native PDF text, OCR, or OCR verified
by vision — along with its confidence.

Without this the citation is decoration, which was the single largest gap in
the system being replaced.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from apps.accounts import permissions as perms
from apps.contracts import services as contract_services

from .models import Finding


@dataclass
class Highlight:
    """One source region, positioned as a percentage of the page."""

    left: float
    top: float
    width: float
    height: float
    text: str
    extraction_method: str
    ocr_confidence: float | None
    verification_status: str
    is_critical: bool
    vision_text: str = ""

    @property
    def is_conflicted(self) -> bool:
        return self.verification_status == "source_conflict"


class SourceViewerView(LoginRequiredMixin, TemplateView):
    template_name = "analysis/source_viewer.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        finding = get_object_or_404(
            Finding.objects.select_related("contract", "clause", "analysis", "category"),
            pk=self.kwargs["pk"],
        )
        perms.require_contract_access(self.request.user, finding.contract)

        blocks = list(
            finding.source_blocks.select_related("page", "document")
            .order_by("page__page_number", "block_index")
        )

        document = blocks[0].document if blocks else (
            finding.contract.documents.order_by("-created_at").first()
        )

        # Prefer the page the evidence is actually on over the page the model
        # reported — the blocks are the record of where the text came from.
        page_number = (
            blocks[0].page.page_number if blocks else (finding.page_number or 1)
        )

        highlights: list[Highlight] = []
        for block in blocks:
            if block.page.page_number != page_number:
                continue
            bbox = block.bbox
            if bbox is None:
                continue
            width = block.page.width or 1
            height = block.page.height or 1
            highlights.append(Highlight(
                left=round(bbox[0] / width * 100, 3),
                top=round(bbox[1] / height * 100, 3),
                width=round((bbox[2] - bbox[0]) / width * 100, 3),
                height=round((bbox[3] - bbox[1]) / height * 100, 3),
                text=block.original_source_text,
                extraction_method=block.extraction_method,
                ocr_confidence=block.ocr_confidence,
                verification_status=block.verification_status,
                is_critical=block.contains_critical_value,
                vision_text=block.vision_text,
            ))

        ctx.update({
            "active_page": "contracts",
            "finding": finding,
            "contract": finding.contract,
            "document": document,
            "page_number": page_number,
            "page_count": document.page_count if document else 0,
            "highlights": highlights,
            "blocks": blocks,
            # A Word source has no geometry, so say so instead of showing an
            # empty frame the reviewer cannot interpret.
            "has_geometry": bool(highlights),
            "no_evidence": not blocks,
        })
        return ctx
