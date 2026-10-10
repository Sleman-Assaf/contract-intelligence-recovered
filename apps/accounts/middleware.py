"""Activate the language the reader actually chose.

`LocaleMiddleware` picks a language from the URL prefix, the session, the
language cookie, then the browser's `Accept-Language` header. None of those is
the preference this application stores on the user, so a reviewer whose browser
announces `en-US` was served an Arabic-designed page with English model labels:
statuses, roles, risk levels and contract types all fell back to their msgids.

This runs after `LocaleMiddleware` and overrides its choice with the stored
preference, then restores the previous language once the response is built so
nothing leaks into the next request on the same worker thread.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import translation


class UserLanguageMiddleware:
    """Honour `user.preferred_language`, falling back to the session."""

    def __init__(self, get_response):
        self.get_response = get_response

    def _language_for(self, request) -> str:
        user = getattr(request, "user", None)
        if getattr(user, "is_authenticated", False):
            chosen = getattr(user, "preferred_language", "") or ""
        else:
            chosen = request.session.get("ui_lang", "") or ""

        supported = {code for code, _ in settings.LANGUAGES}
        return chosen if chosen in supported else settings.LANGUAGE_CODE

    def __call__(self, request):
        language = self._language_for(request)
        previous = translation.get_language()
        translation.activate(language)
        request.LANGUAGE_CODE = language
        try:
            response = self.get_response(request)
        finally:
            # Workers are reused; leaving a language active would bleed into
            # whichever request is handled next on this thread.
            if previous:
                translation.activate(previous)
            else:
                translation.deactivate()

        # Tell caches the response varies by language, not only by the header
        # LocaleMiddleware already accounts for.
        response.setdefault("Content-Language", language)
        return response
