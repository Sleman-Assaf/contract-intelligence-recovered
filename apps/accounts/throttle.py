"""Sign-in lockout: too many wrong passwords and the door closes for a while.

Without it, any number of guesses could be made against an account -- tried
before launch: thirty wrong passwords, then the right one, and it signed in.

Counted per account and address together (LOGIN_MAX_FAILURES, default 5),
and per address alone across accounts (LOGIN_MAX_FAILURES_PER_IP, default
30), for LOGIN_LOCKOUT_SECONDS (default 15 minutes). A correct password
clears the account's count. Counted in the cache, so a site run by several
processes needs a shared cache (prod uses the database cache).

Used by the authentication backend below, so it covers the site's sign-in
and the admin's alike.
"""
from __future__ import annotations

import hashlib

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.core.cache import cache
from django.core.exceptions import PermissionDenied


def client_ip(request) -> str:
    """The caller's address. X-Forwarded-For is believed only behind a proxy
    we were told about (TRUST_X_FORWARDED_FOR): otherwise anyone could set it
    and get a fresh count with every guess."""
    if request is None:
        return ""
    if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def _keys(request, username: str) -> tuple[str, str]:
    ip = client_ip(request)
    account = hashlib.sha256(f"{(username or '').strip().lower()}|{ip}".encode()).hexdigest()
    return f"login-fail:acct:{account}", f"login-fail:ip:{hashlib.sha256(ip.encode()).hexdigest()}"


def _limit(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def is_locked(request, username: str) -> bool:
    account, ip = _keys(request, username)
    return (cache.get(account, 0) >= _limit("LOGIN_MAX_FAILURES", 5)
            or cache.get(ip, 0) >= _limit("LOGIN_MAX_FAILURES_PER_IP", 30))


def record_failure(request, username: str) -> None:
    seconds = _limit("LOGIN_LOCKOUT_SECONDS", 15 * 60)
    for key in _keys(request, username):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, seconds)


def clear(request, username: str) -> None:
    cache.delete(_keys(request, username)[0])


class ThrottledModelBackend(ModelBackend):
    """ModelBackend that refuses to check passwords for a locked account."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        name = username if username is not None else kwargs.get("email", "")
        if is_locked(request, name):
            # PermissionDenied stops authenticate() from trying any other way.
            raise PermissionDenied
        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user is None:
            record_failure(request, name)
        else:
            clear(request, name)
        return user
