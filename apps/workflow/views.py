"""Workflow screen and transition endpoints."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from apps.contracts import services as contract_services

from . import services as wf


class WorkflowIndexView(LoginRequiredMixin, TemplateView):
    template_name = "workflow/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        allowed = contract_services.contracts_for(self.request.user)
        ctx["active_page"] = "workflow"
        ctx["contracts"] = allowed.order_by("-created_at")[:200]

        contract_id = self.request.GET.get("contract")
        contract = (
            allowed.filter(pk=contract_id).first() if contract_id
            else allowed.order_by("-created_at").first()
        )
        if contract is None:
            return ctx

        workflow = wf.get_or_create_workflow(contract)
        ctx["contract"] = contract
        ctx["workflow"] = workflow
        ctx["stages"] = wf.WorkflowStage.choices
        # The stepper needs to know, per stage, whether it is the current one
        # and whether it has been cleared. Deciding that in the template would
        # mean comparing IntegerChoices there, which Django templates cannot do.
        current = workflow.current_stage
        ctx["steps"] = [
            {
                "value": value,
                "label": label,
                "is_current": value == current,
                "is_done": value < current,
            }
            for value, label in wf.WorkflowStage.choices
        ]
        ctx["actions"] = wf.available_actions(self.request.user, contract)
        ctx["transitions"] = contract.transitions.select_related("actor")[:30]
        ctx["comments"] = workflow.comments.select_related("author")
        return ctx


def do_transition(request, pk):
    if request.method != "POST":
        return redirect("workflow:index")

    contract = get_object_or_404(contract_services.contracts_for(request.user), pk=pk)
    try:
        wf.transition(
            contract=contract,
            user=request.user,
            action_key=request.POST.get("action", ""),
            note=request.POST.get("note", ""),
        )
        messages.success(request, _("تم تحديث مسار المراجعة."))
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, getattr(exc, "message", None) or str(exc))
    return redirect(f"/workflow/?contract={contract.pk}")


def add_comment(request, pk):
    if request.method != "POST":
        return redirect("workflow:index")

    contract = get_object_or_404(contract_services.contracts_for(request.user), pk=pk)
    try:
        wf.add_comment(contract=contract, user=request.user,
                       body=request.POST.get("body", ""))
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, getattr(exc, "message", None) or str(exc))
    return redirect(f"/workflow/?contract={contract.pk}")
