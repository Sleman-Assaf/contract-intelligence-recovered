"""Analysis detail, risk matrix, clause comparison, source viewer and advisor."""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from django.contrib import messages
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, TemplateView

from apps.accounts import permissions as perms
from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.contracts import services as contract_services

from . import scoring
from .models import AnalysisRun, Finding, ReviewDecision, VerificationOutcome

logger = logging.getLogger(__name__)

#: The matrix is 5x5 because impact and probability are 1-5 (§25).
MATRIX_SIZE = 5


class AnalysisDetailView(LoginRequiredMixin, DetailView):
    template_name = "analysis/detail.html"
    context_object_name = "run"

    #: The page shows one of these at a time; anything else falls back to the
    #: summary rather than rendering a blank page.
    SECTIONS = ("overview", "findings", "missing", "provenance")

    #: A real contract produces dozens of findings, and each one is now a tall
    #: card. Paginated so the tab opens on something readable rather than a
    #: page the reviewer has to scroll for a minute to reach the end of.
    FINDINGS_PER_PAGE = 5

    def get_queryset(self):
        allowed = contract_services.contracts_for(self.request.user)
        return AnalysisRun.objects.filter(contract__in=allowed).select_related(
            "contract", "document"
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        run = self.object
        findings = (
            run.findings.select_related("category", "clause", "standard_version")
            .prefetch_related("decisions")
            .order_by("-risk_score", "-created_at")
        )
        ctx["active_page"] = "contracts"
        ctx["contract"] = run.contract
        if run.contract.is_in_pipeline:
            from apps.contracts.progress import progress_for

            ctx["progress"] = progress_for(run.contract)
        # The contract's record, for the "بيانات العقد" tab. See
        # contracts/_versions.html for why it lives here as well.
        ctx["source_files"] = run.contract.source_files.all()
        ctx["runs"] = run.contract.analysis_runs.order_by("-version")[:10]
        ctx["findings"] = findings
        ctx["conflicts"] = [f for f in findings if f.needs_human]
        ctx["missing"] = run.missing_clauses.select_related(
            "category", "standard_version__standard__category")
        requested = self.request.GET.get("tab", "overview")
        ctx["section"] = requested if requested in self.SECTIONS else "overview"

        # `findings` stays the whole set: the tab label counts all of them, and
        # the summary cards read across all of them. Only the list is paged.
        paginator = Paginator(findings, self.FINDINGS_PER_PAGE)
        page = paginator.get_page(self.request.GET.get("page"))
        ctx["paginator"] = paginator
        ctx["page_obj"] = page
        ctx["findings_page"] = page
        ctx["is_paginated"] = page.has_other_pages()
        # Numbered links, elided around the current page so a contract with
        # eighty findings does not print sixteen page numbers in a row.
        ctx["page_range"] = (
            list(paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1))
            if page.has_other_pages() else []
        )
        ctx["page_ellipsis"] = paginator.ELLIPSIS
        ctx["uses_demo_standards"] = any(
            entry.get("is_demo_fixture") for entry in (run.standards_snapshot or {}).values()
        )
        return ctx


class RiskMatrixView(LoginRequiredMixin, TemplateView):
    """Impact x probability, populated from real findings.

    The whole portfolio the reader may see by default, narrowed by the filter
    bar: contracts (one or several), AJi company, country, contract type,
    status and upload dates. Only each contract's current analysis counts.
    Every filter is in the URL, and the band links and the pager carry them,
    so a filtered view can be bookmarked or shared and never quietly widens.
    """

    template_name = "analysis/risk_matrix.html"

    #: Rows of the findings log per page.
    LOG_PER_PAGE = 15
    #: Contracts offered in the picker; its search narrows them further.
    PICKER_LIMIT = 300

    def _filters(self, allowed):
        """The filter bar's values, each checked against what exists and what
        this reader may see -- a hand-edited URL narrows nothing it should not."""
        from datetime import date

        from apps.accounts.models import Country, Entity
        from apps.contracts.models import ContractStatus, ContractType

        get = self.request.GET
        wanted = {v for v in get.getlist("contract") if v}
        chosen = []
        if wanted:
            for pk in wanted:
                try:
                    if allowed.filter(pk=pk).exists():
                        chosen.append(pk)
                except (ValidationError, ValueError):
                    continue

        def one_of(name, values):
            value = (get.get(name) or "").strip()
            return value if value in values else ""

        def day(name):
            try:
                return date.fromisoformat((get.get(name) or "").strip())
            except ValueError:
                return None

        return {
            "contract": sorted(chosen),
            "entity": one_of("entity", {str(pk) for pk in Entity.objects.values_list("pk", flat=True)}),
            "country": one_of("country", set(Country.values)),
            "type": one_of("type", set(ContractType.objects.values_list("code", flat=True))),
            "status": one_of("status", set(ContractStatus.values)),
            "from": day("from"),
            "to": day("to"),
        }

    @staticmethod
    def _narrow(contracts, f):
        if f["contract"]:
            contracts = contracts.filter(pk__in=f["contract"])
        if f["entity"]:
            contracts = contracts.filter(entity_id=f["entity"])
        if f["country"]:
            contracts = contracts.filter(country=f["country"])
        if f["type"]:
            contracts = contracts.filter(contract_type__code=f["type"])
        if f["status"]:
            contracts = contracts.filter(status=f["status"])
        if f["from"]:
            contracts = contracts.filter(created_at__date__gte=f["from"])
        if f["to"]:
            contracts = contracts.filter(created_at__date__lte=f["to"])
        return contracts

    @staticmethod
    def _query(f, **extra) -> str:
        """The filters as a query string, plus `extra` (risk, page)."""
        from urllib.parse import urlencode

        pairs = [("contract", pk) for pk in f["contract"]]
        pairs += [(k, f[k]) for k in ("entity", "country", "type", "status") if f[k]]
        pairs += [(k, f[k].isoformat()) for k in ("from", "to") if f[k]]
        pairs += [(k, v) for k, v in extra.items() if v]
        return urlencode(pairs)

    def get_context_data(self, **kwargs):
        from apps.accounts.models import Country, Entity
        from apps.contracts.models import ContractStatus, ContractType

        ctx = super().get_context_data(**kwargs)
        allowed = contract_services.contracts_for(self.request.user)
        f = self._filters(allowed)
        contracts = self._narrow(allowed, f)
        # Each contract's current analysis only: a re-analysis supersedes the
        # last, and counting every run counted one contract six times over.
        findings = Finding.objects.current().filter(contract__in=contracts).select_related(
            "contract", "category"
        )

        # Normalised here rather than where it is used: an unrecognised band
        # was left in the context verbatim, so `?risk=purple` filtered nothing
        # (right) and highlighted no button at all (wrong) -- the page looked
        # filtered by something with no way back to "الكل".
        risk_filter = self.request.GET.get("risk")
        if risk_filter not in {"high", "medium", "low"}:
            risk_filter = "all"

        # Counted before the filter narrows anything. These numbers label the
        # buttons that *apply* the filter, so they have to describe the whole
        # log: counting the filtered set made "مرتفعة" show its own total and
        # the other two show zero, as though the findings had gone.
        totals = findings.aggregate(
            high=Count("id", filter=Q(risk_level="high")),
            medium=Count("id", filter=Q(risk_level="medium")),
            low=Count("id", filter=Q(risk_level="low")),
        )
        # Counted, not added up. `RiskLevel` has a fourth state -- UNKNOWN,
        # which §54 says must never be quietly folded into another band -- and
        # summing the three visible ones would leave an unclassified finding
        # out of "الكل" while "الكل" still listed it. The label would be short
        # by exactly the findings nobody has classified yet, which are the
        # ones worth noticing.
        totals["all"] = findings.count()

        if risk_filter != "all":
            findings = findings.filter(risk_level=risk_filter)

        # Build the grid from real impact/probability pairs. Because the two
        # axes are independent, cells off the diagonal are actually populated.
        grid = {
            (impact, probability): 0
            for impact in range(1, MATRIX_SIZE + 1)
            for probability in range(1, MATRIX_SIZE + 1)
        }
        for row in (
            findings.exclude(impact__isnull=True)
            .exclude(probability__isnull=True)
            .values("impact", "probability")
            .annotate(n=Count("id"))
        ):
            key = (row["impact"], row["probability"])
            if key in grid:
                grid[key] = row["n"]

        rows = []
        for impact in range(MATRIX_SIZE, 0, -1):
            cells = []
            for probability in range(1, MATRIX_SIZE + 1):
                score = impact * probability
                cells.append({
                    "impact": impact,
                    "probability": probability,
                    "score": score,
                    "count": grid[(impact, probability)],
                    "band": ("high" if score >= scoring.HIGH_THRESHOLD
                             else "medium" if score >= scoring.MEDIUM_THRESHOLD else "low"),
                })
            rows.append({"impact": impact, "cells": cells})

        # Which categories the risk actually sits in. The grid says how severe
        # the findings are; it cannot say what they are about, and the space
        # beside it was otherwise empty.
        by_category = (
            findings.exclude(category=None)
            .values("category__name", "category__name_ar")
            .annotate(
                n=Count("id"),
                high=Count("id", filter=Q(risk_level="high")),
            )
            .order_by("-high", "-n")[:8]
        )
        # The bar is the fraction the badge prints, and nothing else.
        # `values()` bypasses the model, so it cannot use the bilingual
        # `display_name` property. Both columns are fetched and the label is
        # chosen here; picking the Arabic one whenever it exists put Arabic
        # category names on the English page.
        from django.utils.translation import get_language

        arabic = (get_language() or "").startswith("ar")
        ctx["categories"] = [
            {
                "label": (row["category__name_ar"] if arabic else "")
                         or row["category__name"],
                "n": row["n"],
                "high": row["high"],
                # Whatever the bar is measured against has to be the
                # number sitting next to it, or a reader cannot check it --
                # and both of the other things it could measure were tried
                # and were wrong. Against the largest *total* it drew a full
                # bar for "0 من 7", the busiest category with nothing severe
                # in it. Against the largest *high count* it drew a full bar
                # for "6 من 7", because some other category had 6 out of 6.
                # Six sevenths of the bar is the only reading that cannot
                # contradict the label.
                "percent": round(row["high"] * 100 / row["n"]) if row["n"] else 0,
            }
            for row in by_category
        ]

        ctx["active_page"] = "matrix"
        ctx["rows"] = rows
        # The log is the long part of this page; the matrix above it is a
        # fixed size whatever the data does.
        log = findings.order_by("-risk_score", "-created_at")
        log_paginator = Paginator(log, self.LOG_PER_PAGE)
        log_page = log_paginator.get_page(self.request.GET.get("page"))
        ctx["findings"] = log_page
        ctx["paginator"] = log_paginator
        ctx["page_obj"] = log_page
        ctx["is_paginated"] = log_page.has_other_pages()
        ctx["page_range"] = (
            list(log_paginator.get_elided_page_range(
                log_page.number, on_each_side=2, on_ends=1))
            if log_page.has_other_pages() else []
        )
        ctx["page_ellipsis"] = log_paginator.ELLIPSIS
        ctx["risk_filter"] = risk_filter
        ctx["totals"] = totals

        # The filter bar, and links that keep it: a band card or a page number
        # that dropped the filters would silently show the whole portfolio.
        band = risk_filter if risk_filter != "all" else ""
        ctx["filters"] = f
        ctx["filtered"] = any(f[k] for k in f)
        ctx["band_links"] = {name: "?" + self._query(f, risk=name if name != "all" else "")
                             for name in ("all", "high", "medium", "low")}
        page_query = self._query(f, risk=band)
        ctx["page_prefix"] = "?" + (page_query + "&" if page_query else "")
        ctx["contract_count"] = contracts.exclude(current_analysis=None).count()
        ctx["contract_options"] = list(
            allowed.order_by("-created_at").only("pk", "project_name", "reference")[: self.PICKER_LIMIT])
        ctx["entities"] = Entity.objects.order_by("pk")
        ctx["countries"] = Country.choices
        ctx["contract_types"] = ContractType.objects.filter(is_active=True)
        ctx["statuses"] = ContractStatus.choices
        return ctx


class ComparisonView(LoginRequiredMixin, TemplateView):
    """Uploaded wording beside the AJi standard, with the reviewer's decision."""

    template_name = "analysis/comparison.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        allowed = contract_services.contracts_for(self.request.user)
        ctx["active_page"] = "comparison"
        ctx["contracts"] = allowed.order_by("-created_at")[:200]

        contract_id = self.request.GET.get("contract")
        finding_id = self.request.GET.get("finding")

        contract = None
        if contract_id:
            contract = allowed.filter(pk=contract_id).first()
        elif ctx["contracts"]:
            contract = ctx["contracts"][0]

        if contract is None:
            return ctx

        run = contract.current_analysis
        findings = (
            Finding.objects.filter(analysis=run).select_related(
                "category", "clause", "standard_version"
            ).order_by("-risk_score")
            if run else Finding.objects.none()
        )

        ctx["contract"] = contract
        ctx["run"] = run
        ctx["findings"] = findings
        ctx["finding"] = (
            findings.filter(pk=finding_id).first() if finding_id else findings.first()
        )
        return ctx


@require_POST
@login_required
def forget_conversations(request):
    """Stop later questions drawing on anything asked so far.

    The threads stay. Memory here is a property of a conversation, not a
    separate store, and a reader asking the assistant to forget is asking it
    to stop using their history -- not to destroy their record of what they
    asked. Undoing this would only need the flag set back.
    """
    from apps.ai.models import AdvisorConversation

    changed = (
        AdvisorConversation.objects
        .filter(user=request.user, remembered=True)
        .update(remembered=False)
    )
    if changed:
        audit.record(
            AuditAction.ADVISOR_MEMORY_CLEARED, actor=request.user,
            details={"conversations": changed},
        )
        messages.success(request, _(
            "مُسحت الذاكرة. لن تعتمد الإجابات على محادثاتك السابقة، وهي "
            "محفوظة كما هي."
        ))
    # Back to the page the control lives on. A reverse rather than the
    # referer: a redirect that trusts a request header is one an attacker
    # can aim.
    return redirect(f"{reverse('accounts:settings')}?tab=assistant")


@require_POST
@login_required
def delete_conversation(request, pk):
    """Remove one of the reader's own advisor threads.

    Scoped to the owner by the queryset, not by a check afterwards: a thread
    can quote a contract the next reviewer is not cleared to see, so someone
    else's id must find nothing rather than be refused -- the refusal itself
    would confirm the thread exists.

    POST only. A link that deletes is a link a browser can follow while
    prefetching.
    """
    if request.method != "POST":
        return redirect("analysis:advisor")

    from apps.ai.models import AdvisorConversation

    deleted, _counts = (
        AdvisorConversation.objects
        .filter(pk=pk, user=request.user)
        .delete()
    )
    # Deliberately no success banner: the thread disappearing from the list is
    # the confirmation, and a message about it would outlive the action on
    # every page that follows.
    if deleted and request.session.get(AdvisorView.SESSION_KEY) == str(pk):
        # Leaving the id in the session would reopen a thread that is gone.
        request.session.pop(AdvisorView.SESSION_KEY, None)
    return redirect("analysis:advisor")


def decide_finding(request, pk):
    """Record Accept / Reject / Escalate on one finding (§28, §29)."""
    if request.method != "POST":
        return redirect("analysis:comparison")

    finding = get_object_or_404(
        Finding.objects.select_related("contract"), pk=pk
    )
    perms.require_contract_access(request.user, finding.contract)

    from apps.workflow import services as wf

    decision = request.POST.get("decision", "")
    if decision not in {"accept", "reject", "escalate", "request_correction", "policy_exception"}:
        messages.error(request, _("قرار غير معروف."))
        return redirect("analysis:comparison")

    try:
        wf.record_decision(
            finding=finding,
            user=request.user,
            decision=decision,
            comment=request.POST.get("comment", ""),
            edited_recommendation=request.POST.get("edited_recommendation", ""),
            edited_fallback=request.POST.get("edited_fallback", ""),
        )
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, str(exc))
        return redirect(f"/comparison/?contract={finding.contract_id}&finding={finding.pk}")

    messages.success(request, _("تم تسجيل القرار: %(decision)s.") % {
        "decision": ReviewDecision.Decision(decision).label})
    return redirect(f"/comparison/?contract={finding.contract_id}&finding={finding.pk}")


def finding_source(request, pk):
    """Where a finding came from: page, coordinates and how the text was read.

    This is what makes a citation checkable rather than decorative — the gap
    that made the previous system's "clause 12.3, page 14" unverifiable.
    """
    if not request.user.is_authenticated:
        raise PermissionDenied

    finding = get_object_or_404(
        Finding.objects.select_related("contract", "clause"), pk=pk
    )
    perms.require_contract_access(request.user, finding.contract)

    blocks = finding.source_blocks.select_related("page").order_by(
        "page__page_number", "block_index"
    )
    return JsonResponse({
        "finding_id": str(finding.pk),
        "clause_number": finding.clause_number,
        "page_number": finding.page_number,
        "original_wording": finding.original_wording,
        "blocks": [
            {
                "page": b.page.page_number,
                "bbox": b.bbox,
                "text": b.original_source_text,
                "extraction_method": b.extraction_method,
                "ocr_confidence": b.ocr_confidence,
                "verification_status": b.verification_status,
                "vision_text": b.vision_text or None,
            }
            for b in blocks
        ],
        "source_file": (
            str(finding.contract.source_files.first().pk)
            if finding.contract.source_files.exists() else None
        ),
    })


class AdvisorView(LoginRequiredMixin, TemplateView):
    """Contract assistant, grounded in stored canonical text (§38)."""

    template_name = "analysis/advisor.html"

    #: The labels the prompt numbers its excerpts with, and the model answers
    #: in. They are an internal addressing scheme, never something to show.
    _REFERENCE = re.compile(r"\b(SOURCE|STANDARD)\s*(\d+)", re.I)

    #: A bracketed excerpt label sitting in the prose, e.g. "[SOURCE 3]".
    _INLINE_LABEL = re.compile(r"\s*\[\s*(?:SOURCE|STANDARD)\s*\d+\s*\]", re.I)

    @classmethod
    def _clean_answer(cls, text: str) -> str:
        """Remove the excerpt labels the model sometimes writes into its answer.

        They are this prompt's internal numbering. A reader seeing "[SOURCE 3]"
        mid-sentence has no way to resolve it, and the excerpts are listed under
        the answer anyway. The prompt asks the model not to, which is a request
        rather than a guarantee.
        """
        cleaned = cls._INLINE_LABEL.sub("", text or "")
        # The label often sat between a word and its punctuation.
        cleaned = re.sub(r" +([،.,;:؛])", r"\1", cleaned)
        return re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    @classmethod
    def _cited_text(cls, references, excerpts, standards):
        """Resolve the model's labels back to the text they point at.

        The prompt hands the model numbered excerpts and it answers citing
        "SOURCE 3". Printed as-is under a heading that reads "from the contract
        text", that tells the reader nothing and implies a quotation that is
        not on the page. A label is also no evidence: only by substituting the
        excerpt can a reader check the answer against the document.

        Anything that is not a label is passed through -- the model may have
        quoted directly -- and a label pointing outside the excerpts it was
        given is dropped rather than shown as a dangling reference.
        """
        sources: list[str] = []
        positions: list[str] = []
        for reference in references or []:
            text = (reference or "").strip()
            match = cls._REFERENCE.search(text)
            if match is None:
                # The model sometimes pastes an excerpt's text instead of
                # citing its label. Filed by what it matches, not by which of
                # its two lists it arrived in -- otherwise an AJi standard is
                # printed under the heading "from the contract text", which
                # says the document contains wording it does not.
                if not text:
                    continue
                if cls._matches_any(text, standards):
                    positions.append(text)
                else:
                    sources.append(text)
                continue
            pool, bucket = (
                (excerpts, sources) if match.group(1).upper() == "SOURCE"
                else (standards, positions)
            )
            index = int(match.group(2)) - 1
            if 0 <= index < len(pool):
                bucket.append(pool[index])
        return sources, positions

    @staticmethod
    def _matches_any(text: str, pool: list[str]) -> bool:
        """Whether this text is one of the excerpts in `pool`.

        Either direction: the model may echo an excerpt in full or trim it. A
        length floor keeps a short common phrase from matching everything.
        """
        needle = " ".join(text.split())
        if len(needle) < 25:
            return False
        for candidate in pool:
            hay = " ".join((candidate or "").split())
            if needle in hay or hay in needle:
                return True
        return False

    @staticmethod
    def _unique(items):
        """Order-preserving dedupe: one excerpt cited twice is one quotation."""
        seen, out = set(), []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    #: Quotations kept under one answer, from the contract and from the standards.
    MAX_QUOTES = 12

    #: Kept in the session, not the URL: a conversation id in a link that gets
    #: shared would hand someone else's exchange to whoever opened it.
    SESSION_KEY = "advisor.conversation"

    #: How far back the rail goes. Older threads are still on the record;
    #: this is the list, not the archive.
    HISTORY_LIMIT = 40

    def _conversations(self, request):
        """This reader's own threads, newest first.

        Scoped to the user throughout. A conversation can quote a contract the
        next reviewer is not cleared to see, so the rail is never a window onto
        somebody else's thread.
        """
        from apps.ai.models import AdvisorConversation

        return (
            AdvisorConversation.objects
            .filter(user=request.user, messages__isnull=False)
            .distinct()
            .select_related("contract")[: self.HISTORY_LIMIT]
        )

    def _open_conversation(self, request):
        """The conversation being continued, or None to start fresh."""
        from apps.ai.models import AdvisorConversation

        owned = AdvisorConversation.objects.filter(user=request.user)

        # Picked from the rail. Filtered by owner, so a link that travels to
        # another reader simply finds nothing rather than opening the thread.
        requested = (request.GET.get("c") or "").strip()
        if requested:
            try:
                picked = owned.filter(pk=requested).prefetch_related("messages").first()
            except (ValidationError, ValueError):
                picked = None
            if picked is not None:
                request.session[self.SESSION_KEY] = str(picked.pk)
                return picked

        key = request.session.get(self.SESSION_KEY)
        if not key:
            return None
        return owned.filter(pk=key).prefetch_related("messages").first()

    @staticmethod
    def _picked_contract(allowed, raw):
        """The chosen contract, or None.

        The picker's "no contract" option posts an empty value, and a UUID
        column raises rather than simply not matching when handed something
        that is not a UUID -- so both the empty choice and a hand-edited id
        would otherwise be a server error instead of an unfiltered question.
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return allowed.filter(pk=raw).first()
        except (ValidationError, ValueError):
            return None

    @staticmethod
    def _chat_files(allowed):
        """The files the chat can answer from: analysed contracts whose RAG
        index is built (apps.analysis.rag). A contract still being analysed
        joins the list when its analysis finishes."""
        return allowed.filter(current_analysis__rag_index__isnull=False)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        allowed = contract_services.contracts_for(self.request.user)
        ctx["active_page"] = "advisor"
        files = self._chat_files(allowed).order_by("-created_at")
        ctx["contracts"] = files[:200]
        conversation = self._open_conversation(self.request)
        # A different file picked while a chat is open starts a new chat about
        # it: one file per conversation, so the old thread is set aside (it
        # stays in the list) rather than continued about another contract.
        picked = self._picked_contract(files, self.request.GET.get("contract"))
        if (picked is not None and conversation is not None
                and conversation.contract_id != picked.pk and not self.request.GET.get("c")):
            self.request.session.pop(self.SESSION_KEY, None)
            conversation = None
        ctx["conversation"] = conversation
        ctx["conversations"] = self._conversations(self.request)
        # Not "messages": that name is taken by the messages framework in
        # every template context.
        ctx["messages_list"] = list(conversation.messages.all()) if conversation else []
        # The file in the URL (a new chat about it), else the one this
        # conversation is about -- only if the chat can answer from it.
        requested = self._picked_contract(allowed, self.request.GET.get("contract"))
        ready = self._picked_contract(files, self.request.GET.get("contract"))
        if requested is not None and ready is None:
            # "Ask AI about the contract" on a file whose analysis has not
            # finished: said, rather than silently opening an empty chat.
            ctx["not_ready"] = requested
        thread_file = conversation.contract if conversation else None
        if thread_file is not None and not files.filter(pk=thread_file.pk).exists():
            thread_file = None
        ctx["contract"] = ready or thread_file
        # An old conversation with no analysed file behind it -- a free chat,
        # or one about a file uploaded into the chat -- stays readable, but
        # nothing more can be asked in it.
        ctx["read_only"] = bool(conversation and ctx["messages_list"] and thread_file is None
                                and ready is None)
        ctx["recent_files"] = files[:8]
        # Shown to the reader, so they follow the reader's language. Each
        # carries its own icon: four identical marks tell a reader nothing
        # about which card does what.
        ctx["suggestions"] = [
            {"icon": "alert", "text": _("حلّل هذا العقد واذكر أهم المخاطر")},
            {"icon": "clipboard", "text": _("ما البنود الناقصة في هذا العقد؟")},
            {"icon": "help", "text": _("اشرح بند تحديد المسؤولية في هذا العقد")},
            {"icon": "compare", "text": _("قارن بند الدفعات في هذا العقد مع معيار AJi")},
        ]
        return ctx

    def post(self, request, *args, **kwargs):
        """Answer a question from the canonical document, not the raw PDF (§38, §51)."""
        from apps.ai.providers.base import AIProviderError
        from apps.ai.services import ai_service

        turn = self._prepare(request)
        if not isinstance(turn, SimpleNamespace):
            return turn                                # a redirect: nothing to ask
        try:
            call = ai_service.answer_question(**turn.ask)
            self._answer(request, turn, call)
        except AIProviderError as exc:
            self._failed(request, turn, exc)
        # Redirect after post, so a refresh does not re-ask the question and
        # spend another call.
        return redirect("analysis:advisor")

    def _prepare(self, request):
        """Everything before the model is asked: the question, the file it is
        about, the passages retrieved for it, and the reader's turn recorded.
        Shared by the page's own POST and by the stream (AdvisorStreamView),
        so both ask the same question of the same material. Returns a
        redirect when there is nothing to ask.

        There is no free chat and no upload here: every question is about one
        analysed contract file, and is answered from that file's RAG index
        and the AJi standards its analysis applied (apps.analysis.rag).
        """
        from apps.ai.models import AdvisorConversation, AdvisorMessage
        from apps.ai import prompts

        from . import advisor_context, rag

        # "New chat" just forgets the current one; the record itself stays.
        if request.POST.get("new") == "1":
            request.session.pop(self.SESSION_KEY, None)
            return redirect("analysis:advisor")

        allowed = contract_services.contracts_for(request.user)
        files = self._chat_files(allowed)
        conversation = self._open_conversation(request)
        contract = self._picked_contract(files, request.POST.get("contract"))
        if contract is None and conversation is not None and conversation.contract_id:
            contract = files.filter(pk=conversation.contract_id).first()
        question = (request.POST.get("question") or "").strip()

        if contract is None:
            messages.error(request, _(
                "اختر ملف عقد محللاً أولاً: المساعد يجيب من ملفات العقود المحللة فقط."))
            return redirect("analysis:advisor")
        if not question:
            messages.error(request, _("اكتب سؤالاً أولاً."))
            return redirect("analysis:advisor")

        index = rag.index_for(contract)
        excerpts, topics = rag.search(index, question)
        standards = advisor_context.standards_for(contract, topics)

        # One file per conversation: asking about another file starts a new
        # one, so an earlier exchange about a different contract is never
        # history for this one.
        if conversation is None or conversation.contract_id != contract.pk:
            conversation = AdvisorConversation.objects.create(
                user=request.user, contract=contract, title=question[:200],
            )
            request.session[self.SESSION_KEY] = str(conversation.pk)

        # What the model sees, taken before this question is recorded.
        history = [
            {"role": m.role, "body": m.body}
            for m in conversation.messages.all() if m.body
        ]
        AdvisorMessage.objects.create(
            conversation=conversation, role=AdvisorMessage.Role.USER, body=question,
        )

        # What this reader has asked before about this same file. Their own
        # threads only, and never the one they are in -- that is already
        # travelling as `history` above.
        from apps.ai.recall import recall_for

        recalled = recall_for(
            request.user, question, exclude_conversation=conversation.pk, contract=contract,
        )

        return SimpleNamespace(
            question=question, contract=contract, excerpts=excerpts, standards=standards,
            conversation=conversation, file=None, doc_label="",
            ask=dict(
                question=question,
                document_excerpts=excerpts,
                standards_excerpts=standards,
                # Answered in the language the question was asked in, not the
                # interface language: a reader who types English wants
                # English back, whatever the menus say.
                language=prompts.language_of(question, default=request.user.preferred_language),
                contract_id=contract.pk,
                history=history,
                recalled=recalled,
            ),
        )

    def _answer(self, request, turn, call):
        """Clean, cite and record the model's answer; returns the saved turn."""
        from apps.ai.models import AdvisorMessage

        contract = turn.contract
        excerpts, standards, doc_label = turn.excerpts, turn.standards, turn.doc_label
        recalled = turn.ask["recalled"]
        answer = call.parsed
        from_sources, from_standards = self._cited_text(
            answer.source_references, excerpts, standards
        )
        also_sources, also_standards = self._cited_text(
            answer.standards_referenced, excerpts, standards
        )
        answer.answer = self._clean_answer(answer.answer)
        # Bucketed by what each citation actually is, not by which list the
        # model happened to put it in -- it mixes the two.
        # Capped: asked for "at most 10", a page-by-page summary of a whole
        # file still cited 30 excerpts -- a wall of quotations under one answer.
        # The chat's own note about the file ("the complete file: all 8
        # pages") is not a quotation from it.
        from .rag import FILE_NOTE

        source_quotes = [q for q in self._unique(from_sources + also_sources)
                         if not q.startswith(FILE_NOTE)][: self.MAX_QUOTES]
        standard_quotes = self._unique(from_standards + also_standards)[: self.MAX_QUOTES]
        return self._record_turn(
            request, turn.conversation,
            role=AdvisorMessage.Role.ASSISTANT,
            body=answer.answer,
            provider=call.provider, model=call.model,
            source_quotes=source_quotes,
            standard_quotes=standard_quotes,
            grounded=bool(excerpts),
            contract_name=(contract.project_name if excerpts and contract
                           else doc_label),
            # Recorded on the turn so a reader can always see what the
            # answer drew on besides their question.
            recalled=recalled,
        )

    def _failed(self, request, turn, exc):
        """Say why there is no answer, as a turn of its own; returns it."""
        from apps.ai.models import AdvisorMessage
        from apps.ai.providers.base import (
            AIPermanentError,
            AIProviderNotConfigured,
            AIProviderUnavailable,
            AIRateLimited,
            AIResponseInvalid,
        )

        excerpts = turn.excerpts
        # The provider's own text is a raw HTTP payload -- a 429 body of
        # JSON tells a reviewer nothing and exposes the backend. It is
        # logged in full for an operator and summarised here.
        logger.warning("Advisor call failed: %s: %s", type(exc).__name__, exc)
        if isinstance(exc, AIRateLimited):
            error = _(
                "المساعد تجاوز الحصة المتاحة حالياً. أعد المحاولة بعد قليل؛ "
                "لم يُحفظ أي شيء ولم يُستهلك من رصيدك."
            )
        elif isinstance(exc, AIProviderNotConfigured):
            error = _(
                "لم تُهيّأ خدمة الذكاء الاصطناعي على هذا الخادم. أبلغ مسؤول النظام."
            )
        elif isinstance(exc, AIProviderUnavailable):
            error = _(
                "تعذّر الوصول إلى خدمة الذكاء الاصطناعي. أعد المحاولة بعد قليل."
            )
        elif isinstance(exc, AIPermanentError):
            error = _(
                "النص المرسل أطول مما يستطيع النموذج قراءته دفعة واحدة، ولم "
                "يُرسل أصلاً حتى لا تُبنى الإجابة على جزء منه. اسأل عن بند "
                "بعينه بدلاً من العقد كاملاً."
            )
        elif isinstance(exc, AIResponseInvalid):
            error = _(
                "ورد رد غير مفهوم من النموذج، ولم يُعرض شيء بدلاً من عرض إجابة "
                "غير موثوقة. أعد صياغة السؤال."
            )
        else:
            error = _(
                "تعذّر الحصول على إجابة. أعد المحاولة، وإن تكرر الأمر أبلغ "
                "مسؤول النظام."
            )
        # Recorded as a turn of its own. A thread that silently drops the
        # question it could not answer misrepresents what happened.
        return self._record_turn(
            request, turn.conversation,
            role=AdvisorMessage.Role.ASSISTANT,
            error=error, grounded=bool(excerpts),
        )

    def _record_turn(self, request, conversation, **fields):
        """Save the assistant's turn -- unless the thread is gone.

        The model call above can take a couple of minutes, and a reader who
        gives up on it deletes the conversation from the sidebar while it is
        still running. Writing the answer into a row that no longer exists
        was a foreign-key error, and the reader got a 500 page for having
        cleaned up. The turn is dropped instead: they threw the thread away,
        and with it the answer to a question they no longer wanted.
        """
        from apps.ai.models import AdvisorConversation, AdvisorMessage

        if not AdvisorConversation.objects.filter(pk=conversation.pk).exists():
            logger.info("Advisor thread %s was deleted mid-call; turn dropped",
                        conversation.pk)
            if request.session.get(self.SESSION_KEY) == str(conversation.pk):
                request.session.pop(self.SESSION_KEY, None)
            return None
        return AdvisorMessage.objects.create(conversation=conversation, **fields)


class AdvisorStreamView(AdvisorView):
    """The advisor's answer, sent while the model is still writing it.

    The same question as AdvisorView.post -- same material, same recorded
    turns, same cleaning and citations -- answered as a text/event-stream:

        data: {"type": "chunk", "text": "..."}     the answer so far, in pieces
        data: {"type": "done",  "html": "..."}     the finished turn, as the page renders it
        data: {"type": "error", "html": "..."}     the failure turn, as the page renders it

    What streams is a draft: the finished turn replaces it, so a reader never
    keeps words the validation would not have shown. composer.js uses this
    when the browser can read a stream, and posts the page's form otherwise.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        from django.http import StreamingHttpResponse
        from django.utils import translation

        turn = self._prepare(request)
        if not isinstance(turn, SimpleNamespace):
            return turn                                # a redirect: the script follows it
        response = StreamingHttpResponse(
            self._events(request, turn, translation.get_language()),
            content_type="text/event-stream; charset=utf-8",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"           # a proxy must not hold the pieces back
        return response

    @staticmethod
    def _event(payload: dict) -> str:
        import json

        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    def _turn_html(self, request, message) -> str:
        from django.template.loader import render_to_string

        if message is None:                            # the thread was deleted meanwhile
            return ""
        return render_to_string("analysis/_advisor_turn.html", {"m": message}, request=request)

    #: Seconds between keep-alive signals while the model has not written yet.
    #: A write is how a closed connection is noticed, so this is also how soon
    #: Stop takes effect while the model is still reading the prompt -- about
    #: two intervals, since the first write after a close still succeeds.
    HEARTBEAT = 0.5          # two writes: Windows reports a closed connection on the second

    def _events(self, request, turn, language):
        import queue
        import threading

        from django.db import connections
        from django.utils import translation

        from apps.ai.providers.base import AICallResult, AIProviderError
        from apps.ai.services import ai_service
        from apps.ai.streaming import AnswerTap, Cancel

        cancel = Cancel()
        pieces: queue.Queue = queue.Queue()

        def read_model():
            """The model is read on a thread of its own, so this response can
            keep writing while it waits -- and notice a reader who has gone
            before there is a single word to send them."""
            try:
                for item in ai_service.answer_question_stream(**turn.ask, cancel=cancel):
                    pieces.put(("item", item))
                    if cancel.is_set:
                        break
                pieces.put(("end", None))
            except BaseException as exc:               # noqa: BLE001 - handed to the response
                pieces.put(("error", exc))
            finally:
                connections.close_all()                # this thread's own database connection

        # The body is sent after the view has returned; the reader's language
        # is put back for the turn rendered at the end.
        with translation.override(language):
            tap, call, shown, recorded = AnswerTap(), None, [], False
            threading.Thread(target=read_model, name="advisor-stream", daemon=True).start()
            try:
                while True:
                    try:
                        kind, value = pieces.get(timeout=self.HEARTBEAT)
                    except queue.Empty:
                        # An SSE comment: the page ignores it, but writing it
                        # fails at once if the reader has pressed Stop.
                        yield ": waiting\n\n"
                        continue
                    if kind == "error":
                        raise value
                    if kind == "end":
                        break
                    if isinstance(value, AICallResult):
                        call = value
                        break
                    text = tap.feed(value)
                    if text:
                        shown.append(text)
                        yield self._event({"type": "chunk", "text": text})
                message = self._answer(request, turn, call)
                recorded = True
                yield self._event({"type": "done", "html": self._turn_html(request, message)})
            except GeneratorExit:
                # The reader pressed Stop (or left): the connection closed at
                # the last piece or keep-alive sent. The request to the model is
                # aborted, and what they saw -- perhaps nothing yet -- is kept,
                # marked stopped, unless the turn was already recorded.
                cancel.set()
                if not recorded:
                    self._stopped(request, turn, "".join(shown))
                raise
            except AIProviderError as exc:
                message = self._failed(request, turn, exc)
                recorded = True
                yield self._event({"type": "error", "html": self._turn_html(request, message)})
            except Exception:                          # noqa: BLE001 - the page must not hang
                logger.exception("Advisor stream failed")
                yield self._event({"type": "error", "html": "", "reload": True})
            finally:
                # Nothing more will be read: a stopped answer stops being
                # written, which frees a server that answers one at a time.
                cancel.set()

    def _stopped(self, request, turn, shown: str):
        """Record a stopped answer: what the reader had seen, cleaned of the
        prompt's excerpt labels, flagged as stopped. Never validated or cited
        -- the model had not finished -- and the page says so beside it."""
        from apps.ai.models import AdvisorMessage

        return self._record_turn(
            request, turn.conversation,
            role=AdvisorMessage.Role.ASSISTANT,
            body=self._clean_answer(shown),
            stopped=True,
            grounded=bool(turn.excerpts),
            contract_name=(turn.contract.project_name if turn.excerpts and turn.contract
                           else turn.doc_label),
        )


def advisor_photo(request, pk):
    """The photo attached to one of the reader's own turns. Owner only: the
    path is private media and the URL says nothing about whose it is."""
    from pathlib import Path

    from django.http import FileResponse, Http404

    from apps.ai.models import AdvisorMessage

    if not request.user.is_authenticated:
        raise PermissionDenied
    turn = get_object_or_404(
        AdvisorMessage, pk=pk, conversation__user=request.user)
    relative = turn.image_path or turn.document_path
    if not relative:
        raise Http404
    root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise Http404
    mime = {".png": "image/png", ".pdf": "application/pdf"}.get(target.suffix, "image/jpeg")
    response = FileResponse(target.open("rb"), content_type=mime)
    if mime == "application/pdf":
        # Shown in the browser's viewer, under the name the reader gave it.
        from django.utils.http import content_disposition_header

        # Escaped and RFC 6266-encoded: an Arabic or quoted name broke the header.
        response["Content-Disposition"] = content_disposition_header(False, turn.document_name or "document.pdf")
    return response
