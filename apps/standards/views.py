"""Settings → Standards: the library, uploads, review and approval.

Viewing the library is open to every signed-in reader (a finding cites these
positions, so a reader must be able to see them). Uploading and reviewing
need MANAGE_STANDARDS; approving needs APPROVE_STANDARDS -- a separate
capability on purpose: the person who loads a standard is not the person who
signs it off (§33).
"""
from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts import permissions as perms
from apps.accounts.models import Country

from . import normalization
from .models import (
    ClauseCategory,
    Standard,
    StandardProfile,
    StandardStatus,
    StandardUpload,
    StandardVersion,
)

TAB = "accounts:settings"
#: Library rows per page.
LIBRARY_PER_PAGE = 12
#: Uploaded files per page.
UPLOADS_PER_PAGE = 8


def _tab_url(**extra) -> str:
    from urllib.parse import urlencode

    return f"{reverse(TAB)}?{urlencode({'tab': 'standards', **extra})}"


def library_context(user, request_get) -> dict:
    """What the Standards tab shows. Called from the settings view."""
    from urllib.parse import urlencode

    from django.core.paginator import Paginator
    from django.db.models import Q

    country = (request_get.get("country") or "").upper()
    profile = request_get.get("profile") or ""
    query = (request_get.get("q") or "").strip()[:120]
    if profile == StandardProfile.FIDIC:
        country = ""            # FIDIC has no national versions to filter by
    standards = (Standard.objects.filter(is_active=True)
                 .select_related("category")
                 .prefetch_related("versions")
                 .order_by("category__sort_order", "reference_template", "country"))
    if country in dict(Country.choices):
        standards = standards.filter(country__in=["", country])
    if profile in StandardProfile.values:
        standards = standards.filter(reference_template=profile)
    if query:
        # The clause in either language, its code, the standard's title, and
        # what its versions say -- a reader looks for "insurance", "تأمين" or
        # a figure such as "25,000,000" alike.
        standards = standards.filter(
            Q(category__name__icontains=query) | Q(category__name_ar__icontains=query)
            | Q(category__code__icontains=query) | Q(title__icontains=query)
            | Q(versions__preferred_position__icontains=query)
            | Q(versions__preferred_position_ar__icontains=query)
            | Q(versions__acceptable_range__icontains=query)
            | Q(versions__red_flags__icontains=query)
            | Q(versions__source_reference__icontains=query)
        ).distinct()

    rows = []
    for standard in standards:
        versions = list(standard.versions.all())
        current = standard.current_version()
        pending = [v for v in versions if v.status in (StandardStatus.DRAFT, StandardStatus.PENDING_APPROVAL)]
        if current is None and not pending:
            continue                     # only superseded / withdrawn history left
        rows.append({"standard": standard, "current": current, "pending": pending})

    # Paged after the rows are built: a standard with nothing current or
    # pending is not a row, so counting the queryset would page wrongly.
    paginator = Paginator(rows, LIBRARY_PER_PAGE)
    page = paginator.get_page(request_get.get("page"))
    filters = {k: v for k, v in {"tab": "standards", "q": query, "country": country,
                                 "profile": profile}.items() if v}

    # The uploaded files, paged on their own (`upage`), so paging them keeps
    # the library where it was and the other way round.
    uploads = Paginator(StandardUpload.objects.select_related("uploaded_by"), UPLOADS_PER_PAGE)
    upage = uploads.get_page(request_get.get("upage"))
    library_state = dict(filters)
    if page.number > 1:
        library_state["page"] = page.number
    if upage.number > 1:
        filters["upage"] = upage.number

    queue = (StandardVersion.objects
             .filter(status__in=[StandardStatus.DRAFT, StandardStatus.PENDING_APPROVAL],
                     standard__is_active=True)
             .select_related("standard", "standard__category")
             .order_by("standard__category__sort_order", "created_at"))
    return {
        "std_rows": page,
        "std_page": page,
        "std_paginator": paginator,
        "std_page_range": (list(paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1))
                           if page.has_other_pages() else []),
        "std_page_ellipsis": paginator.ELLIPSIS,
        "std_query": query,
        "std_qs": urlencode(filters),
        "std_queue": list(queue),
        "std_uploads": upage,
        "std_upage": upage,
        "std_upaginator": uploads,
        "std_upage_range": (list(uploads.get_elided_page_range(upage.number, on_each_side=2, on_ends=1))
                            if upage.has_other_pages() else []),
        "std_uqs": urlencode(library_state),
        "std_can_manage": perms.has_capability(user, perms.MANAGE_STANDARDS),
        "std_can_approve": perms.has_capability(user, perms.APPROVE_STANDARDS),
        "std_countries": Country.choices,
        "std_profiles": StandardProfile.choices,
        "std_country": country,
        "std_profile": profile,
        "std_categories_total": ClauseCategory.objects.filter(is_active=True).count(),
    }


@login_required
@require_POST
def upload(request):
    if not perms.has_capability(request.user, perms.MANAGE_STANDARDS):
        raise PermissionDenied
    file = request.FILES.get("file")
    if file is None:
        messages.error(request, _("اختر ملفاً أولاً."))
        return redirect(_tab_url())
    try:
        item = normalization.create_upload(
            request.user, file.read(), getattr(file, "name", ""),
            profile=request.POST.get("profile") or StandardProfile.AJI,
            country=(request.POST.get("country") or "").upper(),
            effective_date=parse_date(request.POST.get("effective_date") or ""),
            note=request.POST.get("note", ""),
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect(_tab_url())
    return redirect("standards:review", pk=item.pk)


@login_required
def review(request, pk):
    if not perms.has_capability(request.user, perms.MANAGE_STANDARDS):
        raise PermissionDenied
    item = get_object_or_404(StandardUpload, pk=pk)
    if request.method == "POST":
        if item.status != StandardUpload.Status.REVIEW:
            messages.error(request, _("هذا الملف حُفظ من قبل أو تعذّرت قراءته."))
            return redirect("standards:review", pk=item.pk)
        try:
            count = max(0, min(int(request.POST.get("count", "0")), 200))
        except ValueError:
            count = 0
        items = normalization.items_from_post(request.POST, count)
        # Keep each position's quote check from the reading: the form cannot
        # re-establish it, and a reviewer should see it after saving too.
        for new, old in zip(items, item.items):
            new["quote_found"] = old.get("quote_found")
        created = normalization.save_drafts(item, items, request.user)
        messages.success(request, _("حُفظت %(n)s معايير كمسودات بانتظار الاعتماد.") % {"n": len(created)})
        return redirect(_tab_url())
    return render(request, "standards/review.html", {
        "active_page": "settings", "tab": "standards", "upload": item,
        "items": [dict(it, rule_json=json.dumps(it.get("rule") or {}, ensure_ascii=False))
                  for it in item.items],
        "categories": ClauseCategory.objects.filter(is_active=True),
        "countries": Country.choices, "impacts": [1, 2, 3, 4, 5],
    })


@login_required
@require_POST
def retry(request, pk):
    if not perms.has_capability(request.user, perms.MANAGE_STANDARDS):
        raise PermissionDenied
    item = get_object_or_404(StandardUpload, pk=pk)
    if item.status in (StandardUpload.Status.FAILED, StandardUpload.Status.REVIEW):
        normalization.process(item)
    return redirect("standards:review", pk=item.pk)


@login_required
@require_POST
def discard(request, pk):
    """Delete an uploaded file and its record.

    Standards already saved from it are separate records -- drafts awaiting
    approval, or approved versions an analysis may cite -- and stay exactly as
    they are; only the file and its upload entry go. The stored file is
    removed unless another upload holds the same bytes (files are stored by
    their SHA-256, so identical uploads share one).
    """
    from pathlib import Path

    from django.conf import settings

    if not perms.has_capability(request.user, perms.MANAGE_STANDARDS):
        raise PermissionDenied
    item = get_object_or_404(StandardUpload, pk=pk)
    target = Path(settings.PRIVATE_MEDIA_ROOT) / item.stored_path
    shared = StandardUpload.objects.filter(stored_path=item.stored_path).exclude(pk=item.pk).exists()
    name = item.original_filename
    item.delete()
    try:
        if not shared and target.is_file():
            target.unlink()
    except OSError:
        pass
    messages.success(request, _("حُذف الملف «%(name)s».") % {"name": name})
    return redirect(_safe_next(request) or _tab_url())


def _safe_next(request) -> str:
    """The form's `next`, when it is an address on this site."""
    from django.utils.http import url_has_allowed_host_and_scheme

    back = request.POST.get("next") or ""
    if back and url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()},
                                                require_https=request.is_secure()):
        return back
    return ""


def _version_action(request, pk, action):
    version = get_object_or_404(StandardVersion, pk=pk)
    try:
        action(version)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    except PermissionDenied:
        raise
    # Only a path on this site: an approval must not bounce the approver to
    # an address someone put in the form.
    return redirect(_safe_next(request) or _tab_url())


@login_required
@require_POST
def approve(request, pk):
    if not perms.has_capability(request.user, perms.APPROVE_STANDARDS):
        raise PermissionDenied
    return _version_action(request, pk, lambda v: (
        normalization.approve(v, request.user),
        messages.success(request, _("اعتُمد المعيار وأصبح سارياً في التحليلات الجديدة."))))


@login_required
@require_POST
def set_impact(request, pk):
    if not perms.has_capability(request.user, perms.MANAGE_STANDARDS):
        raise PermissionDenied
    try:
        impact = int(request.POST.get("impact", ""))
    except ValueError:
        impact = 0
    return _version_action(request, pk, lambda v: normalization.set_impact(v, impact, request.user))


@login_required
@require_POST
def withdraw(request, pk):
    if not perms.has_capability(request.user, perms.MANAGE_STANDARDS):
        raise PermissionDenied
    return _version_action(request, pk, lambda v: normalization.withdraw(v, request.user))
