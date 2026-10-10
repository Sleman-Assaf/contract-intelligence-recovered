"""Authentication, UI preferences and settings."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views.generic import CreateView
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from apps.audit import services as audit
from apps.audit.models import AuditAction, AuditEvent

from . import permissions as perms
from .forms import LoginForm, SignUpForm
from .models import Role, User


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        # "Remember me", opt-in. Unticked, the session dies with the browser --
        # the safer default on a shared or site machine, which is what most of
        # these are. Ticked, it lasts SESSION_COOKIE_AGE.
        if not self.request.POST.get("remember"):
            self.request.session.set_expiry(0)
        audit.record(AuditAction.LOGIN, actor=self.request.user)
        return response

    def form_invalid(self, form):
        audit.record(
            AuditAction.LOGIN_FAILED,
            details={"email": form.data.get("username", "")[:160]},
        )
        return super().form_invalid(form)


class LogoutView(auth_views.LogoutView):
    next_page = reverse_lazy("accounts:login")


class SignUpView(CreateView):
    """Self-registration, which grants nothing on its own.

    The account is created inactive, so Django's own authentication refuses it
    at the door until an administrator activates it. Nothing here is a
    security decision the registrant gets to make: role and entity are not on
    the form, and `SignUpForm.save` sets the lowest role regardless of what
    was posted.
    """

    form_class = SignUpForm
    template_name = "accounts/signup.html"
    success_url = reverse_lazy("accounts:login")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("contracts:dashboard")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        audit.record(
            AuditAction.USER_REGISTERED,
            entity_type="user", entity_id=self.object.pk,
            details={"email": self.object.email},
        )
        messages.success(self.request, _(
            "أُنشئ حسابك. لا يمكنك الدخول حتى يفعّله مسؤول النظام، وسيظهر "
            "طلبك له الآن."
        ))
        return response


@require_POST
@login_required
def delete_user(request, pk):
    """Remove an account. MANAGE_USERS only (§34).

    Three things cannot be deleted, and each refusal is deliberate:

    * yourself -- there is no way back in afterwards;
    * the last active administrator, for the same reason;
    * anyone who has uploaded a contract. `Contract.uploaded_by` is PROTECT,
      so who filed a document survives the person leaving. That is provenance,
      not bookkeeping, and deactivating is the right answer there.

    The audit trail is safe either way: it stores `actor_email` at write time
    precisely so it outlives the account.
    """
    from django.db.models import ProtectedError

    perms.require_capability(request.user, perms.MANAGE_USERS)
    account = get_object_or_404(User, pk=pk)
    back = f"{reverse('accounts:settings')}?tab=users"

    if account.pk == request.user.pk:
        messages.error(request, _("لا يمكنك حذف حسابك أنت."))
        return redirect(back)

    last_admin = (
        account.role == Role.ADMIN
        and not User.objects.filter(role=Role.ADMIN, is_active=True)
                            .exclude(pk=account.pk).exists()
    )
    if last_admin:
        messages.error(request, _(
            "هذا آخر حساب مدير نشط. عيّن مديراً آخر قبل حذفه، وإلا لن يبقى "
            "من يدير النظام."
        ))
        return redirect(back)

    email = account.email
    try:
        account.delete()
    except ProtectedError:
        uploaded = account.uploaded_contracts.count()
        messages.error(request, _(
            "لا يمكن حذف %(email)s لأنه رفع %(n)s من العقود، ومَن رفع مستنداً "
            "يبقى مسجلاً عليه. عطّل الحساب بدلاً من ذلك."
        ) % {"email": email, "n": uploaded})
        return redirect(back)

    audit.record(
        AuditAction.USER_DELETED, actor=request.user,
        entity_type="user", entity_id=pk,
        previous_state="active" if account.is_active else "inactive",
        new_state="deleted", details={"email": email},
    )
    messages.success(request, _("حُذف حساب %(email)s.") % {"email": email})
    return redirect(back)


@require_POST
@login_required
def set_user_active(request, pk):
    """Turn an account on or off. MANAGE_USERS only (§34).

    An administrator cannot switch their own account off: locking the last
    administrator out of the system is not a decision worth allowing by
    accident, and there is no second way back in.
    """
    perms.require_capability(request.user, perms.MANAGE_USERS)

    account = get_object_or_404(User, pk=pk)
    activate = request.POST.get("active") == "1"

    if account.pk == request.user.pk and not activate:
        messages.error(request, _("لا يمكنك تعطيل حسابك أنت."))
        return redirect(f"{reverse('accounts:settings')}?tab=users")

    fields = ["is_active"]
    previous_role = account.role
    # A role may be assigned in the same action. Registration always produces
    # a Role.USER, so approving without this leaves an account that can see
    # nothing -- the administrator would have to reach for the Django admin.
    wanted_role = request.POST.get("role") or ""
    if activate and wanted_role and wanted_role in Role.values:
        account.role = wanted_role
        fields.append("role")

    if account.is_active != activate or account.role != previous_role:
        account.is_active = activate
        account.save(update_fields=fields)
        if account.role != previous_role:
            audit.record(
                AuditAction.PERMISSION_CHANGED, actor=request.user,
                entity_type="user", entity_id=account.pk,
                previous_state=previous_role, new_state=account.role,
                details={"email": account.email},
            )
        audit.record(
            AuditAction.USER_ACTIVATED if activate else AuditAction.USER_DEACTIVATED,
            actor=request.user,
            entity_type="user", entity_id=account.pk,
            previous_state="inactive" if activate else "active",
            new_state="active" if activate else "inactive",
            details={"email": account.email},
        )
        messages.success(request, (
            _("فُعّل حساب %(email)s.") if activate
            else _("عُطّل حساب %(email)s.")
        ) % {"email": account.email})

    return redirect(f"{reverse('accounts:settings')}?tab=users")


def _back(request, fallback="contracts:dashboard"):
    from django.urls import reverse

    referer = request.META.get("HTTP_REFERER")
    # Only follow a same-origin referer.
    if referer and referer.startswith(request.build_absolute_uri("/")):
        return referer
    return reverse(fallback)


@require_POST
def toggle_theme(request):
    current = (
        request.user.preferred_theme
        if request.user.is_authenticated
        else request.session.get("ui_theme", "light")
    )
    new = "dark" if current == "light" else "light"
    if request.user.is_authenticated:
        request.user.preferred_theme = new
        request.user.save(update_fields=["preferred_theme"])
    else:
        request.session["ui_theme"] = new
    return HttpResponseRedirect(_back(request))


@require_POST
def toggle_language(request):
    current = (
        request.user.preferred_language
        if request.user.is_authenticated
        else request.session.get("ui_lang", "ar")
    )
    new = "en" if current == "ar" else "ar"
    if request.user.is_authenticated:
        request.user.preferred_language = new
        request.user.save(update_fields=["preferred_language"])
    else:
        request.session["ui_lang"] = new
    return HttpResponseRedirect(_back(request))


class SettingsView(LoginRequiredMixin, TemplateView):
    """Users, audit log and system status."""

    template_name = "accounts/settings.html"

    #: Rows per page. The directory holds fifty, which is more than most
    #: deployments will ever have -- so for most of them there is one page and
    #: no pager at all, and the pager appears only where it earns its place.
    #: The audit log keeps a smaller page: it grows without limit and nobody
    #: reads a year of it in one go.
    USERS_PER_PAGE = 50
    AUDIT_PER_PAGE = 25

    def _paginate(self, queryset, per_page, ctx):
        """Page `queryset` into `ctx`, and return the page.

        One tab renders at a time, so both tables share the context keys and
        the same pager markup rather than each carrying its own.
        """
        paginator = Paginator(queryset, per_page)
        page = paginator.get_page(self.request.GET.get("page"))
        ctx["paginator"] = paginator
        ctx["page_obj"] = page
        ctx["is_paginated"] = page.has_other_pages()
        # Elided around the current page, so a long table does not print
        # seventeen page numbers in a row.
        ctx["page_range"] = (
            list(paginator.get_elided_page_range(
                page.number, on_each_side=2, on_ends=1))
            if page.has_other_pages() else []
        )
        ctx["page_ellipsis"] = paginator.ELLIPSIS
        return page

    def get_context_data(self, **kwargs):
        from django.conf import settings as dj_settings

        from apps.ai.registry import available_providers
        from apps.documents.ocr import get_ocr_engine

        from .models import Role, User

        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        # Landing on "users" is right for an administrator and a dead end for
        # everyone else -- the tab they can actually use is their own.
        tab = self.request.GET.get("tab") or (
            "users" if perms.has_capability(user, perms.MANAGE_USERS)
            else "assistant"
        )
        if tab == "system" and not perms.has_capability(user, perms.MANAGE_USERS):
            # The system tab probes the AI server and shows the OCR and
            # provider set-up: an administrator's page, not every reader's.
            tab = "assistant"
        ctx["active_page"] = "settings"
        ctx["tab"] = tab

        if not perms.has_capability(user, perms.MANAGE_USERS):
            ctx["users_denied"] = True
        elif tab == "users":
            query = (self.request.GET.get("q") or "").strip()
            everyone = User.objects.select_related("entity").order_by("email")
            if query:
                # The name a person chose, their address, and the older
                # name fields accounts may still carry. The role is a coded
                # field whose label is translated at render time, so it is
                # matched separately below rather than with an `icontains`
                # that would only ever hit the stored code.
                matches = (
                    Q(username__icontains=query)
                    | Q(email__icontains=query)
                    | Q(first_name__icontains=query)
                    | Q(last_name__icontains=query)
                    | Q(full_name_ar__icontains=query)
                    | Q(department__icontains=query)
                )
                roles = [
                    value for value, label in Role.choices
                    if query.casefold() in str(label).casefold()
                    or query.casefold() in value.casefold()
                ]
                if roles:
                    matches |= Q(role__in=roles)
                everyone = everyone.filter(matches)
            ctx["user_query"] = query
            ctx["users"] = self._paginate(everyone, self.USERS_PER_PAGE, ctx)
            # Accounts waiting on somebody. Listed separately and unpaged,
            # above the directory: a registration buried on page three of an
            # alphabetical list is a person who cannot work today, and the
            # search box would hide it entirely.
            ctx["pending_users"] = list(
                User.objects.filter(is_active=False).order_by("-date_joined")
            )
            ctx["roles"] = Role.choices

        if tab == "audit":
            if perms.has_capability(user, perms.VIEW_AUDIT_LOG):
                # Paged rather than capped. The previous [:200] slice meant an
                # audit trail quietly stopped at its two hundredth newest
                # entry, with nothing on the page saying so -- the one table
                # here whose whole purpose is that nothing goes unrecorded.
                ctx["events"] = self._paginate(
                    AuditEvent.objects.select_related("actor"),
                    self.AUDIT_PER_PAGE, ctx,
                )
            else:
                ctx["audit_denied"] = True

        if tab == "assistant":
            from apps.ai.models import AdvisorConversation

            # Only the reader's own, and only the threads answers may draw on:
            # the number here has to be the number the assistant would use, or
            # clearing it will look like it did nothing.
            ctx["remembered_count"] = (
                AdvisorConversation.objects.recallable_for(user).count()
            )
            ctx["conversation_count"] = (
                AdvisorConversation.objects.filter(user=user).count()
            )

        if tab == "standards":
            from apps.standards.views import library_context

            ctx.update(library_context(user, self.request.GET))

        if tab == "system":
            try:
                ocr_ok, ocr_detail = get_ocr_engine().is_available()
            except Exception as exc:               # engine not installed
                ocr_ok, ocr_detail = False, str(exc)
            ctx["providers"] = available_providers()
            ctx["ocr"] = {"ok": ocr_ok, "detail": ocr_detail,
                          "engine": dj_settings.OCR_ENGINE,
                          "languages": dj_settings.OCR_LANGUAGES}
            ctx["thresholds"] = dj_settings.OCR_THRESHOLDS
        return ctx
