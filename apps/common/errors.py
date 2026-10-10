"""The server-error page, in the visitor's language.

Django's own `server_error` renders 500.html with an empty context -- no
request, no context processors -- because whatever failed might be exactly
what they depend on. That kept the page safe but always Arabic in its layout:
the English site got English words (the language is still active from
UserLanguageMiddleware) laid out right to left, in the light theme.

This hands the page the three values it needs and nothing else: the language
already active for the request, and the reader's theme. Each is read
defensively -- a database that is down must not turn the error page into a
second error -- and anything that cannot be read falls back to what the
page would have shown anyway: Arabic, right to left, light.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpResponseServerError
from django.template import loader
from django.utils import translation

logger = logging.getLogger(__name__)

_FALLBACK = "<!doctype html><title>Server error</title><h1>Server error</h1>"


def _preferences(request) -> tuple[str, str]:
    supported = {code for code, _ in settings.LANGUAGES}
    lang = (translation.get_language() or "").split("-")[0]
    if lang not in supported:
        lang = "ar"
    theme = "light"
    try:
        user = getattr(request, "user", None)
        if getattr(user, "is_authenticated", False):
            theme = getattr(user, "preferred_theme", "") or theme
        else:
            theme = request.session.get("ui_theme", theme)
    except Exception:          # the session store or the database may be what broke
        pass
    return lang, (theme if theme in {"light", "dark"} else "light")


def server_error(request, template_name: str = "500.html"):
    try:
        lang, theme = _preferences(request)
        with translation.override(lang):
            body = loader.get_template(template_name).render({
                "ui_lang": lang,
                "ui_dir": "rtl" if lang == "ar" else "ltr",
                "ui_theme": theme,
            })
    except Exception:
        logger.exception("The 500 page itself could not be rendered")
        body = _FALLBACK
    return HttpResponseServerError(body)
