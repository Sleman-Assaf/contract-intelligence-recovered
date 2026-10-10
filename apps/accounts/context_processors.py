"""UI preferences exposed to every template.

The v2 design carries language, theme and sidebar state in component state;
server-rendered pages read them from the signed-in user (falling back to the
session for anonymous pages).
"""
from __future__ import annotations

from django.conf import settings

from . import permissions as perms


def ui_preferences(request):
    user = getattr(request, "user", None)
    authed = getattr(user, "is_authenticated", False)

    lang = (
        user.preferred_language if authed
        else request.session.get("ui_lang", settings.LANGUAGE_CODE)
    )
    theme = (
        user.preferred_theme if authed
        else request.session.get("ui_theme", "light")
    )
    collapsed = (
        user.sidebar_collapsed if authed
        else request.session.get("ui_sidebar_collapsed", False)
    )

    # What this reader may do, so a template can leave out a control rather
    # than show one that answers with a 403. Every role could see "تحليل
    # جديد" in the sidebar and the top bar, and "تصدير PDF" on the report
    # page, and three of the six roles were forbidden the moment they clicked.
    caps = perms.capabilities_for(user) if authed else set()

    return {
        "can_upload": perms.UPLOAD_CONTRACT in caps,
        "can_export": perms.EXPORT_REPORT in caps,
        "can_download": perms.DOWNLOAD_SOURCE_FILE in caps,
        "can_run_analysis": perms.RUN_ANALYSIS in caps,
        "can_manage_users": perms.MANAGE_USERS in caps,
        "ui_lang": lang,
        "ui_dir": "rtl" if lang == "ar" else "ltr",
        "ui_theme": theme,
        "ui_sidebar_collapsed": collapsed,
        "APP_VERSION": "2.0.0",
    }
