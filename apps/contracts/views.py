"""Dashboard, contract register, upload and secure file access.

Every query goes through `services.contracts_for(user)`, which applies the
queryset scoping. Dashboard aggregates are computed by the database over the
*authorised* set — not over the first 200 rows fetched into the browser, which
is how the version being replaced produced its numbers (§40).
"""
from __future__ import annotations

from django.contrib import messages
from django.utils.translation import gettext as _
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, FormView, ListView, TemplateView

from apps.accounts import permissions as perms
from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.documents import storage
from apps.documents.models import SourceFile

from . import services
from .forms import ContractUploadForm
from .search import SearchFilters, search
from .models import Contract, ContractStatus, RiskLevel


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "contracts/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        qs = services.contracts_for(user)

        # Aggregated in SQL over everything this user may see.
        counts = qs.aggregate(
            total=Count("id"),
            high=Count("id", filter=Q(risk_level=RiskLevel.HIGH)),
            medium=Count("id", filter=Q(risk_level=RiskLevel.MEDIUM)),
            low=Count("id", filter=Q(risk_level=RiskLevel.LOW)),
            unknown=Count("id", filter=Q(risk_level=RiskLevel.UNKNOWN)),
            approved=Count("id", filter=Q(status=ContractStatus.APPROVED)),
            in_review=Count("id", filter=Q(status__in=[
                ContractStatus.PENDING_CONTRACT_REVIEW,
                ContractStatus.LEGAL_REVIEW,
                ContractStatus.MANAGEMENT_REVIEW,
            ])),
            needs_attention=Count("id", filter=Q(status__in=[
                ContractStatus.NEEDS_HUMAN_REVIEW,
                ContractStatus.NEEDS_SOURCE_REVIEW,
                ContractStatus.EXTRACTION_FAILED,
                ContractStatus.ANALYSIS_FAILED,
            ])),
        )
        ctx["counts"] = counts

        ctx["by_country"] = (
            qs.values("country").annotate(n=Count("id")).order_by("-n")
        )
        # `values()` bypasses the model, so it cannot use the bilingual
        # `display_name` property. Both columns are fetched and the label is
        # resolved here, otherwise an Arabic page shows English type names.
        from django.utils.translation import get_language

        arabic = (get_language() or "").startswith("ar")
        ctx["by_type"] = [
            {
                "label": (row["contract_type__name_ar"] if arabic else "")
                         or row["contract_type__name"],
                "n": row["n"],
            }
            for row in qs.values("contract_type__name", "contract_type__name_ar")
                         .annotate(n=Count("id")).order_by("-n")[:6]
        ]
        ctx["recent"] = (
            qs.select_related("contract_type", "entity").order_by("-created_at")[:6]
        )
        ctx["upcoming"] = (
            qs.filter(expiry_date__gte=timezone.localdate())
              .order_by("expiry_date")[:6]
        )

        from apps.analysis.models import Finding, VerificationOutcome

        # The current analysis only: a superseded run's conflicts were
        # listed beside the current ones, some of them already resolved.
        ctx["conflicts"] = (
            Finding.objects.current().filter(
                contract__in=qs,
                verification_status__in=[
                    VerificationOutcome.SYSTEM_AI_CONFLICT,
                    VerificationOutcome.SOURCE_CONFLICT,
                    VerificationOutcome.NEEDS_HUMAN_REVIEW,
                ],
            )
            .select_related("contract", "category")
            .order_by("-risk_score")[:6]
        )
        # Contract expiries, as the design's deadline card wants them: the
        # date split into day and month, and how long is left.
        from datetime import date

        today = timezone.localdate()
        ctx["deadlines"] = [
            {
                "contract": c,
                "day": c.expiry_date.strftime("%d"),
                "month": c.expiry_date.strftime("%b").upper(),
                "days_left": (c.expiry_date - today).days,
            }
            for c in qs.filter(expiry_date__gte=today).order_by("expiry_date")[:3]
        ]

        # Recent workflow movement, read from the immutable transition log
        # rather than reconstructed from contract state.
        from apps.workflow.models import WorkflowTransition

        ctx["activity"] = (
            WorkflowTransition.objects.filter(contract__in=qs)
            .select_related("contract", "actor")
            .order_by("-created_at")[:3]
        )

        ctx["active_page"] = "dashboard"
        return ctx


class ContractListView(LoginRequiredMixin, ListView):
    template_name = "contracts/list.html"
    context_object_name = "contracts"
    paginate_by = 25          # real pagination, not a 100-row fetch (§40)

    def get_queryset(self):
        qs = services.contracts_for(self.request.user).select_related(
            "contract_type", "entity", "reviewer"
        )
        params = self.request.GET

        if q := params.get("q", "").strip():
            qs = qs.filter(
                Q(project_name__icontains=q)
                | Q(client_name__icontains=q)
                | Q(reference__icontains=q)
                | Q(contract_title__icontains=q)
            )
        if value := params.get("type"):
            qs = qs.filter(contract_type__code=value)
        if value := params.get("risk"):
            qs = qs.filter(risk_level=value)
        if value := params.get("status"):
            qs = qs.filter(status=value)
        if value := params.get("country"):
            qs = qs.filter(country=value)
        return qs.order_by("-created_at")

    def paginate_queryset(self, queryset, page_size):
        # ListView answers a page past the end with a 404, so a bookmarked
        # "?page=3" became an error page once contracts were archived. The
        # other lists on the site use `get_page`, which shows the last page.
        paginator = self.get_paginator(queryset, page_size)
        page = paginator.get_page(self.request.GET.get(self.page_kwarg))
        return paginator, page, page.object_list, page.has_other_pages()

    def get_context_data(self, **kwargs):
        from apps.accounts.models import Country

        from .models import ContractType

        ctx = super().get_context_data(**kwargs)
        ctx["active_page"] = "contracts"
        ctx["filters"] = self.request.GET
        # "5 contracts held in the system" was what the pill said with a
        # filter on -- the filtered count in the unfiltered sentence. With a
        # filter active it now says how many matched, out of how many.
        ctx["filtered"] = any(
            v for k, v in self.request.GET.items() if k != "page")
        ctx["total"] = services.contracts_for(self.request.user).count()
        ctx["contract_types"] = ContractType.objects.filter(is_active=True)
        ctx["statuses"] = ContractStatus.choices
        ctx["risk_levels"] = RiskLevel.choices
        ctx["countries"] = Country.choices
        return ctx


class ContractUploadView(LoginRequiredMixin, FormView):
    template_name = "contracts/upload.html"
    form_class = ContractUploadForm

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            perms.require_capability(request.user, perms.UPLOAD_CONTRACT)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_page"] = "upload"
        return ctx

    def form_valid(self, form):
        data = dict(form.cleaned_data)
        uploaded = data.pop("file")
        try:
            contract, source_file = services.create_contract(
                actor=self.request.user, uploaded_file=uploaded, **data
            )
        except ValidationError as exc:
            form.add_error("file", exc)
            return self.form_invalid(form)

        services.queue_processing(contract, source_file, actor=self.request.user)
        # No flash message: the contract page opens on its progress card,
        # which says the same thing and then shows it happening.
        return redirect("contracts:detail", pk=contract.pk)


class ContractDetailView(LoginRequiredMixin, DetailView):
    """The contract record.

    Opening a contract should land on its analysis -- that is the page people
    come here to read, and it is what the approved design shows. The record
    below (parties, source files, run history) stays reachable at
    `?view=record`, and is what you get when there is nothing to analyse yet.
    """

    template_name = "contracts/detail.html"
    context_object_name = "contract"

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        analysis = self.object.current_analysis
        if analysis is not None and request.GET.get("view") != "record":
            return redirect("analysis:detail", pk=analysis.pk)
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_queryset(self):
        return services.contracts_for(self.request.user).select_related(
            "contract_type", "entity", "uploaded_by", "reviewer", "current_analysis"
        )

    def get_context_data(self, **kwargs):
        from apps.workflow import services as wf

        ctx = super().get_context_data(**kwargs)
        contract = self.object
        ctx["active_page"] = "contracts"
        ctx["parties"] = contract.parties.all()
        ctx["source_files"] = contract.source_files.all()
        ctx["runs"] = contract.analysis_runs.order_by("-version")[:10]
        ctx["workflow"] = wf.get_or_create_workflow(contract)
        ctx["actions"] = wf.available_actions(self.request.user, contract)
        ctx["transitions"] = contract.transitions.select_related("actor")[:20]
        document = contract.documents.order_by("-created_at").first()
        ctx["document"] = document
        ctx["key_facts"] = ([f for f in document.key_facts.all() if f.status != "not_found"]
                            if document is not None else [])
        from apps.documents.selectors import file_notes

        ctx["file_notes"] = file_notes(document)
        if self.object.is_in_pipeline:
            from .progress import progress_for

            ctx["progress"] = progress_for(self.object)
        return ctx


def download_source_file(request, pk):
    """Authenticated download. There is no public URL for a contract file (§47)."""
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())       # a link followed signed out

    source_file = get_object_or_404(
        SourceFile.objects.select_related("contract"), pk=pk
    )
    perms.require_contract_access(request.user, source_file.contract)
    perms.require_capability(request.user, perms.DOWNLOAD_SOURCE_FILE)

    try:
        path = storage.absolute_path(source_file.stored_path)
    except ValidationError as exc:
        raise Http404(str(exc)) from exc
    if not path.exists():
        raise Http404("The stored file is missing.")

    audit.record(
        AuditAction.CONTRACT_DOWNLOADED,
        actor=request.user,
        entity_type="SourceFile",
        entity_id=source_file.pk,
        contract_id=source_file.contract_id,
        details={"filename": source_file.original_filename},
    )

    return FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename=source_file.original_filename,
        content_type=source_file.content_type or "application/octet-stream",
    )


def reanalyse(request, pk):
    """Queue a fresh analysis. Creates a new version; never overwrites (§32)."""
    if request.method != "POST":
        return redirect("contracts:detail", pk=pk)

    contract = get_object_or_404(services.contracts_for(request.user), pk=pk)
    perms.require_capability(request.user, perms.RUN_ANALYSIS)

    from apps.documents.queueing import queue_analysis

    document = contract.documents.order_by("-created_at").first()
    queue_analysis(contract, document, actor_id=request.user.pk)
    messages.success(request, _("أُدرجت إعادة التحليل في قائمة الانتظار، وسيُنشأ إصدار جديد."))
    return redirect("contracts:detail", pk=contract.pk)


class SearchView(LoginRequiredMixin, TemplateView):
    """Cross-entity search over contracts, clause text and findings (§39).

    Scoped before it searches, so it can never surface a contract the user
    cannot open.
    """

    template_name = "contracts/search.html"

    def get_context_data(self, **kwargs):
        from apps.accounts.models import Country

        from .models import ContractType

        ctx = super().get_context_data(**kwargs)
        filters = SearchFilters.from_request(self.request.GET)

        ctx["active_page"] = "contracts"
        ctx["filters"] = filters
        ctx["params"] = self.request.GET
        ctx["results"] = search(self.request.user, filters)
        ctx["countries"] = Country.choices
        ctx["statuses"] = ContractStatus.choices
        ctx["risk_levels"] = RiskLevel.choices
        ctx["contract_types"] = ContractType.objects.filter(is_active=True)
        return ctx


#: How many of each kind the header's live list shows; the page has the rest.
SUGGEST_LIMITS = {"contracts": 5, "findings": 4, "clauses": 3}


def _snippet(text: str, q: str, width: int = 70) -> str:
    """The words around the first match, so a clause hit shows why it matched."""
    text = " ".join((text or "").split())
    at = text.lower().find(q.lower())
    if at < 0:
        return text[:width * 2]
    start = max(0, at - width)
    end = min(len(text), at + len(q) + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _risk_label(level: str) -> str:
    """The badge's words, as on every other page ("مخاطر مرتفعة")."""
    from .models import risk_badge_label

    return risk_badge_label(level) if level in RiskLevel.values else ""


def search_suggest(request):
    """The header search's live list: the best matches as the reader types.

    The same search as the results page -- same scoping, so it can never
    surface a contract the reader cannot open -- with a few of each kind, and
    a link to every result.
    """
    from urllib.parse import urlencode

    from django.http import JsonResponse

    from .search import search_clauses, search_contracts, search_findings

    if not request.user.is_authenticated:
        return JsonResponse({"error": "login required"}, status=401)
    q = (request.GET.get("q") or "").strip()[:100]
    out = {"q": q, "groups": [], "total": 0,
           "all": reverse("contracts:search") + "?" + urlencode({"q": q})}
    if len(q) < 2:
        return JsonResponse(out)

    f = SearchFilters(q=q)
    n, contracts = search_contracts(request.user, f, limit=SUGGEST_LIMITS["contracts"])
    if n:
        out["groups"].append({"kind": "contracts", "label": _("العقود"), "count": n, "items": [
            {"title": c.project_name,
             "meta": " · ".join(x for x in (c.reference, c.client_name) if x),
             "badge": c.risk_level, "badge_label": _risk_label(c.risk_level),
             "url": reverse("contracts:detail", args=[c.pk])}
            for c in contracts]})
    n_f, findings = search_findings(request.user, f, limit=SUGGEST_LIMITS["findings"])
    if n_f:
        out["groups"].append({"kind": "findings", "label": _("الملاحظات"), "count": n_f, "items": [
            {"title": x.display_title,
             "meta": " · ".join(v for v in (x.contract.project_name,
                                             _("البند") + " " + x.clause_number if x.clause_number else "") if v),
             "badge": x.risk_level, "badge_label": _risk_label(x.risk_level),
             "url": reverse("analysis:detail", args=[x.analysis_id]) + "?tab=findings"}
            for x in findings]})
    n_c, clauses = search_clauses(request.user, f, limit=SUGGEST_LIMITS["clauses"])
    if n_c:
        out["groups"].append({"kind": "clauses", "label": _("البنود"), "count": n_c, "items": [
            {"title": (_("البند") + " " + cl.clause_number) if cl.clause_number else (cl.heading or _("البنود")),
             "meta": cl.contract.project_name, "snippet": _snippet(cl.text, q),
             "url": reverse("contracts:detail", args=[cl.contract_id])}
            for cl in clauses]})
    out["total"] = n + n_f + n_c
    return JsonResponse(out)


def new_round(request, pk):
    """Upload a revised draft as the next negotiation round (§41)."""
    if request.method != "POST":
        return redirect("contracts:detail", pk=pk)
    # Uploading a draft is uploading a contract: the same right as the
    # upload form, not merely being able to see the contract.
    perms.require_capability(request.user, perms.UPLOAD_CONTRACT)

    contract = get_object_or_404(services.contracts_for(request.user), pk=pk)
    uploaded = request.FILES.get("file")
    if uploaded is None:
        messages.error(request, _("اختر ملف المسودة الجديدة."))
        return redirect("contracts:rounds", pk=contract.pk)

    source_file = services.start_new_round(
        contract=contract,
        uploaded_file=uploaded,
        actor=request.user,
        note=request.POST.get("note", ""),
    )
    services.queue_processing(contract, source_file, actor=request.user)
    messages.success(
        request,
        _("تم رفع الجولة %(n)s. سيبدأ التحليل في الخلفية.")
        % {"n": source_file.round_number},
    )
    return redirect("contracts:rounds", pk=contract.pk)


class ContractRoundsView(LoginRequiredMixin, TemplateView):
    """Every negotiation round, and what moved between the last two."""

    template_name = "contracts/rounds.html"

    def get_context_data(self, **kwargs):
        from apps.analysis import rounds as rounds_service

        ctx = super().get_context_data(**kwargs)
        contract = get_object_or_404(
            services.contracts_for(self.request.user), pk=kwargs["pk"]
        )
        ctx["active_page"] = "contracts"
        ctx["contract"] = contract
        ctx["rounds"] = rounds_service.rounds_for(contract)
        ctx["comparison"] = rounds_service.latest_comparison(contract)
        ctx["can_upload"] = perms.has_capability(
            self.request.user, perms.UPLOAD_CONTRACT
        )
        return ctx


def progress(request, pk):
    """Where the contract's processing has got to, for the progress card."""
    from django.http import JsonResponse

    from .progress import progress_for

    if not request.user.is_authenticated:
        raise PermissionDenied
    contract = get_object_or_404(services.contracts_for(request.user), pk=pk)
    return JsonResponse(progress_for(contract))
