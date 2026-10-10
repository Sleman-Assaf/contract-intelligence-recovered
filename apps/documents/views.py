"""Serving page images for the source viewer (§26).

Page renders are cached beside the private file: rasterising a PDF page on
every request would make the viewer unusable on a long contract. The cache
lives inside PRIVATE_MEDIA_ROOT, so a rendered page is no more public than the
document it came from.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import cache_control

from apps.accounts import permissions as perms

from . import extractors, storage
from .models import Document, Page

#: Rendering width in device pixels. 1400 keeps Arabic diacritics legible when
#: a reviewer zooms, without producing files too large to send.
RENDER_DPI = 150


def _cache_path(document: Document, page_number: int) -> Path:
    digest = hashlib.sha256(
        f"{document.pk}:{page_number}:{RENDER_DPI}".encode()
    ).hexdigest()[:24]
    return (
        Path(settings.PRIVATE_MEDIA_ROOT)
        / "page-cache"
        / str(document.contract_id)
        / f"{digest}.png"
    )


@cache_control(private=True, max_age=3600)
def page_image(request, document_id, page_number: int):
    """Rendered image of one page. Authenticated and access-checked."""
    if not request.user.is_authenticated:
        raise PermissionDenied

    document = get_object_or_404(
        Document.objects.select_related("contract", "source_file"), pk=document_id
    )
    perms.require_contract_access(request.user, document.contract)
    perms.require_capability(request.user, perms.DOWNLOAD_SOURCE_FILE)

    cached = _cache_path(document, page_number)
    if cached.exists():
        return FileResponse(cached.open("rb"), content_type="image/png")

    try:
        source = storage.absolute_path(document.source_file.stored_path)
    except ValidationError as exc:
        raise Http404(str(exc)) from exc
    if not source.exists():
        raise Http404("The stored file is missing.")

    if not str(source).lower().endswith(".pdf"):
        # Word documents carry no page geometry, so there is nothing to render
        # and nothing to highlight. Say so rather than failing obscurely.
        return HttpResponse(
            "This document has no page images. Highlighting is only available "
            "for PDF sources.",
            status=415, content_type="text/plain; charset=utf-8",
        )

    try:
        png = extractors.rasterise_page(source, int(page_number), dpi=RENDER_DPI)
    except extractors.ExtractionError as exc:
        raise Http404(str(exc)) from exc

    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(png)
    return HttpResponse(png, content_type="image/png")


def page_blocks(request, document_id, page_number: int):
    """Every block on a page with its coordinates, as percentages.

    Percentages rather than pixels so the overlay stays correct at any zoom or
    render resolution.
    """
    from django.http import JsonResponse

    if not request.user.is_authenticated:
        raise PermissionDenied

    document = get_object_or_404(
        Document.objects.select_related("contract"), pk=document_id
    )
    perms.require_contract_access(request.user, document.contract)

    page = get_object_or_404(Page, document=document, page_number=page_number)
    width = page.width or 1
    height = page.height or 1

    blocks = []
    for block in page.blocks.order_by("block_index"):
        bbox = block.bbox
        blocks.append({
            "id": block.pk,
            "text": block.original_source_text,
            "extraction_method": block.extraction_method,
            "ocr_confidence": block.ocr_confidence,
            "verification_status": block.verification_status,
            "contains_critical_value": block.contains_critical_value,
            "rect": None if bbox is None else {
                "left": round(bbox[0] / width * 100, 3),
                "top": round(bbox[1] / height * 100, 3),
                "width": round((bbox[2] - bbox[0]) / width * 100, 3),
                "height": round((bbox[3] - bbox[1]) / height * 100, 3),
            },
        })

    return JsonResponse({
        "page": page.page_number,
        "page_count": document.page_count,
        "width": page.width,
        "height": page.height,
        "extraction_method": page.extraction_method,
        "ocr_confidence": page.ocr_confidence,
        "verification_status": page.verification_status,
        "blocks": blocks,
    })
