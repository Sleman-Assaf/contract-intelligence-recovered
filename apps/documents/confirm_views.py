"""The reviewer's look at a read file before it is analysed.

After extraction a scanned file, or one whose figures disagree, stops here
(apps/documents/tasks.py): the key figures with every place the file states
them -- each with the image of that spot -- the pages that will and will not
be analysed, and what is wrong with the file itself. Confirming saves the
values and the page choices and starts the analysis.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import cache_control

from apps.accounts import permissions as perms

from . import extractors, storage
from .models import Document, KeyFact, Page, PageKind
from .page_kinds import ANALYSED_KINDS


@cache_control(private=True, max_age=3600)
def region_image(request, document_id, page_number: int):
    """One region of a page -- where a key fact was read -- turned the way OCR
    turned the page, so a sideways table is shown upright."""
    if not request.user.is_authenticated:
        raise PermissionDenied
    document = get_object_or_404(
        Document.objects.select_related("contract", "source_file"), pk=document_id)
    perms.require_contract_access(request.user, document.contract)
    perms.require_capability(request.user, perms.DOWNLOAD_SOURCE_FILE)
    page = get_object_or_404(Page, document=document, page_number=page_number)
    try:
        bbox = [float(v) for v in request.GET.get("b", "").split(",")]
        if len(bbox) != 4:
            raise ValueError
    except ValueError as exc:
        raise Http404("Invalid region.") from exc
    try:
        source = storage.absolute_path(document.source_file.stored_path)
        png = extractors.crop_region(source, page.page_number, bbox, dpi=170, padding=28,
                                     rotate=page.ocr_rotation)
    except (ValidationError, extractors.ExtractionError) as exc:
        raise Http404(str(exc)) from exc
    return HttpResponse(png, content_type="image/png")


def parse_ranges(text: str, last: int) -> set[int]:
    """"6-94, 160" -> {6, ..., 94, 160}, clipped to the document."""
    out: set[int] = set()
    for a, b in re.findall(r"(\d+)\s*(?:[-–]\s*(\d+))?", text or ""):
        start, end = int(a), int(b or a)
        if start > end:
            start, end = end, start
        out.update(n for n in range(start, end + 1) if 1 <= n <= last)
    return out


def _save(request, contract, document, facts, pages) -> dict:
    """Store the reviewer's choices; returns what was confirmed."""
    now = timezone.now()
    confirmed = {}
    for fact in facts:
        choice = request.POST.get(f"fact-{fact.name}", "")
        if choice.startswith("c:"):
            try:
                picked = fact.candidates[int(choice[2:])]
            except (ValueError, IndexError):
                continue
            fact.value, fact.unit = str(picked.get("value", "")), picked.get("unit", "") or ""
        elif choice == "other":
            fact.value = request.POST.get(f"fact-{fact.name}-other", "").strip()[:255]
            fact.unit = request.POST.get(f"fact-{fact.name}-unit", "").strip()[:32]
            if not fact.value:
                continue
        elif choice == "none":
            fact.value, fact.unit = "", ""
        else:
            continue
        fact.status = KeyFact.Status.CONFIRMED
        fact.confirmed_by, fact.confirmed_at = request.user, now
        fact.note = "not in the contract" if choice == "none" else ""
        fact.save(update_fields=["value", "unit", "status", "confirmed_by", "confirmed_at",
                                 "note", "updated_at"])
        confirmed[fact.name] = {"value": fact.value, "unit": fact.unit}

    last = pages[-1].page_number if pages else 0
    chosen = {p.page_number for p in pages if request.POST.get(f"page-{p.page_number}")}
    chosen |= parse_ranges(request.POST.get("include_range", ""), last)
    chosen -= parse_ranges(request.POST.get("exclude_range", ""), last)
    for page in pages:
        include = page.page_number in chosen
        if include != page.include_in_analysis:
            page.include_in_analysis = include
            page.save(update_fields=["include_in_analysis", "updated_at"])

    # The confirmed value fills the contract's own record when it was left
    # empty at upload.
    value = confirmed.get(KeyFact.Name.CONTRACT_VALUE, {})
    if value.get("value") and contract.contract_value is None:
        try:
            contract.contract_value = Decimal(value["value"].replace(",", ""))
            fields = ["contract_value", "updated_at"]
            if value.get("unit") in dict(contract._meta.get_field("currency").choices):
                contract.currency = value["unit"]
                fields.append("currency")
            contract.save(update_fields=fields)
        except (InvalidOperation, ValueError):
            pass
    return {"facts": confirmed, "pages_analysed": len(chosen), "pages": last}


def confirm_facts(request, contract_id):
    from apps.audit import services as audit
    from apps.audit.models import AuditAction
    from apps.contracts import services as contract_services
    from apps.contracts.models import ContractStatus

    from .key_facts import analyse_document

    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())       # a link followed signed out
    contract = get_object_or_404(contract_services.contracts_for(request.user), pk=contract_id)
    document = contract.documents.order_by("-created_at").first()
    if document is None or document.extraction_finished_at is None:
        messages.info(request, _("لم تكتمل قراءة الملف بعد."))
        return redirect("contracts:detail", pk=contract.pk)
    if not document.key_facts.exists():
        analyse_document(document, keep_page_choices=True)

    order = list(KeyFact.Name.values)
    facts = sorted(document.key_facts.all(), key=lambda f: order.index(f.name))
    pages = list(document.pages.order_by("page_number"))

    if request.method == "POST":
        perms.require_capability(request.user, perms.RUN_ANALYSIS)
        details = _save(request, contract, document, facts, pages)
        audit.record(AuditAction.CONTRACT_UPDATED, actor=request.user, entity_type="Document",
                     entity_id=document.pk, contract_id=contract.pk,
                     details={"stage": "key_facts_confirmed", **details})
        if request.POST.get("action") == "analyse":
            from .queueing import queue_analysis

            missing = [f.get_name_display() for f in document.key_facts.filter(status=KeyFact.Status.NEEDS_INPUT)]
            if missing:
                messages.error(request, _("أدخل هذه القيم، أو اختر «غير موجودة في العقد»، ثم ابدأ التحليل: %(facts)s")
                               % {"facts": "، ".join(str(m) for m in missing)})
                return redirect("documents:confirm", contract_id=contract.pk)

            # Pressed again while the analysis it started is still queued or
            # running (two reviewers on the same screen, or a second click):
            # one analysis, not two.
            if contract.status in (ContractStatus.STRUCTURING, ContractStatus.AI_ANALYSIS) or \
                    contract.analysis_runs.filter(status__in=["queued", "running"]).exists():
                messages.info(request, _("التحليل قيد التشغيل بالفعل؛ لم يُبدأ تحليل آخر."))
                return redirect("contracts:detail", pk=contract.pk)
            contract.status = ContractStatus.STRUCTURING
            contract.save(update_fields=["status", "updated_at"])
            queue_analysis(contract, document, actor_id=request.user.pk)
            messages.success(request, _("تم تأكيد البيانات وبدأ التحليل."))
            return redirect("contracts:detail", pk=contract.pk)
        messages.success(request, _("حُفظت التغييرات."))
        return redirect("documents:confirm", contract_id=contract.pk)

    rows = []
    for fact in facts:
        options = []
        for index, c in enumerate(fact.candidates):
            bbox = c.get("bbox")
            options.append({**c, "index": index,
                            "region": ",".join(f"{v:.1f}" for v in bbox) if bbox else "",
                            "is_value": str(c.get("value")) == fact.value})
        if fact.status in (KeyFact.Status.CONFIRMED, KeyFact.Status.SETTLED):
            match = next((o for o in options if o["is_value"]), None)
            default = f"c:{match['index']}" if match else ("other" if fact.value else "none")
        elif fact.status == KeyFact.Status.NEEDS_INPUT:
            default = "other"                              # typed in, not picked
        elif options:
            preferred = next((o for o in options if o.get("included")), options[0])
            default = f"c:{preferred['index']}"
        else:
            default = "none"
        for o in options:
            o["checked"] = default == f"c:{o['index']}"
        rows.append({"fact": fact, "options": options, "default": default,
                     "values": fact.distinct_values,
                     "ask": fact.status == KeyFact.Status.NEEDS_INPUT})
    # What the site could not read comes first; what it settled is folded away.
    to_enter = [r for r in rows if r["ask"]]
    settled = [r for r in rows if not r["ask"]]

    warnings = [w for w in (document.warnings or []) if w.get("kind") != "ocr_repair"]
    repairs = [w for w in (document.warnings or []) if w.get("kind") == "ocr_repair"]
    return render(request, "documents/confirm.html", {
        "active_page": "contracts",
        "contract": contract,
        "document": document,
        "rows": rows,
        "to_enter": to_enter,
        "settled": settled,
        "pages": pages,
        "warnings": warnings,
        "repairs": repairs,
        "can_confirm": perms.has_capability(request.user, perms.RUN_ANALYSIS),
        "pages_included": sum(1 for p in pages if p.include_in_analysis),
        "pages_scanned": sum(1 for p in pages if not p.has_native_text),
        "pages_rotated": [p.page_number for p in pages if p.ocr_rotation],
        "attention": sum(1 for f in facts if f.needs_attention),
        "excluded_kinds": [PageKind(k).label for k in PageKind.values if k not in ANALYSED_KINDS],
    })
