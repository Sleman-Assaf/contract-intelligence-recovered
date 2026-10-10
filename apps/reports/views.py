"""Executive contract report (§37).

Real PDF generation, not `window.print()` — the previous system's "Export PDF"
button only opened the browser dialog, so there was no controlled document and
no Arabic layout guarantee.
"""
from __future__ import annotations

from base64 import b64encode
from functools import lru_cache
from pathlib import Path

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.views.generic import DetailView, TemplateView

from apps.accounts import permissions as perms
from apps.reports import redline
from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.contracts import services as contract_services
from apps.contracts.models import Contract

from .renderer import PdfUnavailable, render_pdf


@lru_cache(maxsize=16)
def _static_data_uri(folder: str, name: str, mime: str) -> str:
    """A file from static/<folder> as a data URI.

    The PDF is rendered from a temp file with no access to /static/, so a
    normal static URL resolves to nothing and the document loses its logos
    and its Arabic typeface. Inlining keeps one report template serving both
    the screen and the export.
    """
    from django.conf import settings

    for root in list(settings.STATICFILES_DIRS) + [settings.STATIC_ROOT]:
        if not root:
            continue
        candidate = Path(root) / folder / name
        if candidate.is_file():
            encoded = b64encode(candidate.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{encoded}"
    return ""


def _image_data_uri(name: str) -> str:
    return _static_data_uri("img", name, "image/png")


def _noto_kufi_faces() -> str:
    """@font-face rules for Noto Kufi Arabic, inlined from static/fonts.

    Loaded from Google Fonts, the face came as a variable font that Chromium
    embedded as an unnamed Type3 font: the Arabic text layer came out with its
    words reversed, and an export made while Google was unreachable fell back
    to another face. The static files embed as real TrueType, with no network.
    """
    faces = []
    for weight in (400, 700):
        uri = _static_data_uri("fonts", f"noto-kufi-arabic-{weight}.ttf", "font/ttf")
        if uri:
            faces.append('@font-face { font-family: "Noto Kufi Arabic"; font-weight: %d; '
                         "font-style: normal; src: url(%s) format('truetype'); }" % (weight, uri))
    return "\n".join(faces)


def _sanad_logo_data_uri() -> str:
    """Sanad, for the PDF's black brand strip: the yellow-and-white artwork,
    in the language the document's words are in."""
    from django.utils.translation import get_language

    lang = "en" if (get_language() or "").startswith("en") else "ar"
    return _image_data_uri(f"sanad-{lang}-dark.png")


def _css_string(text: str) -> str:
    """`text` as a quoted CSS string, for a `content:` inside the <style> block.

    Quotes, backslashes, line breaks and angle brackets are hex-escaped, so a
    project name can neither end the string nor close the <style> element.
    """
    out = []
    for ch in str(text or ""):
        out.append("\\%x " % ord(ch) if ch in '"\\<>&' or ord(ch) < 32 else ch)
    return '"' + "".join(out) + '"'


def _pdf_footer(contract, run) -> dict:
    """The running footer's two halves, as CSS strings for the page margin boxes.

    A fixed element was the running footer. Current Chromium prints one with a
    negative offset at the top of every page after the first -- over the first
    lines of text -- and none at all on page one. The page's own margin boxes
    sit outside the content box, so they cannot run into it on any page.
    """
    from django.utils import timezone
    from django.utils.translation import gettext as _

    # Each machine string in a left-to-right isolate (LRI ... PDI): after an
    # Arabic word the digits of 2026-09-28 count as Arabic numbers, and the
    # three groups were printed in reverse, as 28-09-2026.
    def ltr(value: str) -> str:
        return f"⁦{value}⁩"

    finished = timezone.localtime(run.finished_at).strftime("%Y-%m-%d") if run and run.finished_at else "—"
    version = f"v{run.version}" if run else "—"
    return {
        "pdf_footer_start": _css_string(f"{ltr(contract.reference or '—')} · {contract.project_name}"),
        "pdf_footer_end": _css_string(f"{_('أُصدر')} {ltr(finished)} · {_('إصدار التحليل')} {ltr(version)}"),
    }


def _report_context(contract):
    from apps.documents.selectors import file_notes

    run = contract.current_analysis
    findings = (
        run.findings.select_related("category", "standard_version", "clause")
        .order_by("-risk_score")
        if run else []
    )
    return {
        "contract": contract,
        "run": run,
        "findings": findings,
        "actions": [f for f in findings if f.display_recommended_action],
        "critical": [f for f in findings if f.risk_level == "high"],
        "unresolved": [f for f in findings if f.needs_human],
        "missing": run.missing_clauses.select_related(
            "category", "standard_version__standard__category") if run else [],
        "parties": contract.parties.all(),
        "transitions": contract.transitions.select_related("actor")[:50],
        "standards_snapshot": (run.standards_snapshot if run else {}) or {},
        "standards_applied": _standards_applied(run),
        "file_notes": file_notes(run.document if run and run.document_id
                                 else contract.documents.order_by("-created_at").first()),
    }


def _standards_applied(run) -> list[dict]:
    """The snapshot's standards as the reader reads them.

    The snapshot keys a standard by its category code and says its scope as a
    code ("scope v1 (global)"), which is what the Arabic report printed. The
    category's name and the scope's label are looked up here; an entry whose
    category has since gone keeps its code.
    """
    from apps.standards.models import ClauseCategory, StandardScope

    snapshot = (run.standards_snapshot if run else {}) or {}
    names = {c.code: c.display_name for c in ClauseCategory.objects.filter(code__in=snapshot)}
    rows = []
    for code, entry in snapshot.items():
        scope = entry.get("scope", "")
        rows.append({
            **entry,
            "name": names.get(code, code),
            "scope_label": StandardScope(scope).label if scope in StandardScope.values else scope,
        })
    return rows


def _can_send_to_legal(user, contract) -> bool:
    """Whether the workflow would accept "send to legal" from this reader now.

    The report page offered the button to everyone, and it led an
    administrator -- who by design holds no review authority -- to a workflow
    page that said no action was available. The same rule the workflow page
    uses decides here, so the two pages cannot disagree.
    """
    from apps.workflow import services as wf

    return any(a.key == "send_to_legal" for a in wf.available_actions(user, contract))


class ReportIndexView(LoginRequiredMixin, TemplateView):
    template_name = "reports/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        allowed = contract_services.contracts_for(self.request.user)
        ctx["active_page"] = "reports"
        ctx["contracts"] = allowed.order_by("-created_at")[:200]

        contract_id = self.request.GET.get("contract")
        contract = (
            allowed.filter(pk=contract_id).first() if contract_id
            else allowed.filter(current_analysis__isnull=False).first()
        )
        if contract is not None:
            ctx.update(_report_context(contract))
            ctx["can_send_to_legal"] = _can_send_to_legal(self.request.user, contract)
        return ctx


class ReportDetailView(LoginRequiredMixin, DetailView):
    template_name = "reports/index.html"
    model = Contract

    def get_queryset(self):
        return contract_services.contracts_for(self.request.user)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_page"] = "reports"
        # The same page as the index, so the same picker: without the list it
        # read "no contracts" above a contract's own report.
        ctx["contracts"] = contract_services.contracts_for(self.request.user).order_by("-created_at")[:200]
        ctx.update(_report_context(self.object))
        ctx["can_send_to_legal"] = _can_send_to_legal(self.request.user, self.object)
        return ctx


def report_pdf(request, pk):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())       # a link followed signed out
    contract = get_object_or_404(contract_services.contracts_for(request.user), pk=pk)
    perms.require_capability(request.user, perms.EXPORT_REPORT)

    # Laid out in the language its words are in: the English export was
    # printed right to left, labels on the right and "information .1".
    from django.utils.translation import get_language

    lang = "en" if (get_language() or "").startswith("en") else "ar"
    html = render_to_string(
        "reports/report_pdf.html",
        {**_report_context(contract), "ui_lang": lang, "ui_dir": "ltr" if lang == "en" else "rtl",
         "sanad_logo_src": _sanad_logo_data_uri(), "for_pdf": True,
         "arabic_font_faces": _noto_kufi_faces(),
         **_pdf_footer(contract, contract.current_analysis)},
        request=request,
    )

    try:
        pdf = render_pdf(html)
    except PdfUnavailable as exc:
        return HttpResponse(
            f"PDF generation is unavailable on this host: {exc}",
            status=503, content_type="text/plain; charset=utf-8",
        )

    audit.record(
        AuditAction.REPORT_GENERATED,
        actor=request.user, entity_type="Contract", entity_id=contract.pk,
        contract_id=contract.pk, details={"format": "pdf"},
    )

    filename = f"{contract.reference or str(contract.pk)[:8]}-report.pdf"
    response = HttpResponse(pdf, content_type="application/pdf")
    from django.utils.http import content_disposition_header

    # Escaped and RFC 6266-encoded: an Arabic or quoted name broke the header.
    response["Content-Disposition"] = content_disposition_header(True, filename)
    return response


def report_redline(request, pk):
    """The proposed clause changes as a tracked-changes .docx (§37).

    Behind the same capability as the PDF: this file leaves AJi and carries
    AJi's proposed wording, so whoever may export a report may export this.
    """
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())       # a link followed signed out
    contract = get_object_or_404(contract_services.contracts_for(request.user), pk=pk)
    perms.require_capability(request.user, perms.EXPORT_REPORT)

    run = contract.current_analysis
    if run is None:
        return HttpResponse(
            "This contract has no analysis to redline yet.",
            status=409, content_type="text/plain; charset=utf-8",
        )

    changes = redline.changes_for_run(run)
    document = redline.build_redline(contract=contract, run=run, changes=changes)

    audit.record(
        AuditAction.REPORT_GENERATED,
        actor=request.user, entity_type="Contract", entity_id=contract.pk,
        contract_id=contract.pk,
        details={
            "format": "redline_docx",
            "changes": len(changes),
            # Worth recording separately: entries with no approved wording went
            # out as flags, not proposals.
            "actionable": sum(1 for c in changes if c.is_actionable),
        },
    )

    filename = f"{contract.reference or str(contract.pk)[:8]}-redline.docx"
    response = HttpResponse(
        document,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    from django.utils.http import content_disposition_header

    # Escaped and RFC 6266-encoded: an Arabic or quoted name broke the header.
    response["Content-Disposition"] = content_disposition_header(True, filename)
    return response
