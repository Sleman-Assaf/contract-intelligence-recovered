"""HTML to PDF rendering (§37).

WeasyPrint needs the GTK runtime, which is not present on this Windows host, so
the default engine shells out to headless Chromium (Edge or Chrome). That was
verified on this machine to render Arabic RTL correctly with embedded fonts.

The engine sits behind `render_pdf()` so swapping to WeasyPrint on a Linux
server is a settings change, not a code change.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from django.conf import settings


class PdfUnavailable(RuntimeError):
    """No usable PDF engine on this host. Reported, never silently skipped."""


# Chromium builds that ship on Windows, in preference order.
_CHROMIUM_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_chromium() -> str | None:
    configured = getattr(settings, "PDF_CHROMIUM_PATH", "")
    if configured and Path(configured).exists():
        return configured
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _CHROMIUM_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def engine_status() -> tuple[bool, str]:
    """For the system-status page, so an operator can see this before a user does."""
    engine = getattr(settings, "PDF_ENGINE", "chromium")
    if engine == "weasyprint":
        try:
            import weasyprint  # noqa: F401
        except Exception as exc:
            return False, f"WeasyPrint selected but unavailable: {exc}"
        return True, "WeasyPrint available."

    binary = find_chromium()
    if binary:
        return True, f"Headless Chromium available at {binary}."
    return False, (
        "No Chromium binary found. Install Edge or Chrome, or set "
        "PDF_CHROMIUM_PATH, or switch PDF_ENGINE to weasyprint."
    )


def _await_pdf(target: Path, deadline: float) -> bytes:
    """Wait for Chromium to finish writing, and return the bytes.

    The launcher hands the print job to a background process and exits: on this
    host it returns after ~160ms while the file lands at ~750ms. Checking for
    the file the moment `subprocess.run` returns therefore finds nothing, and
    the temporary directory is then deleted out from under the process still
    writing into it. That is the whole reason this export reported "no PDF
    engine" on a host where the engine works.

    A size that has stopped changing between two polls means the write is
    finished; the magic number confirms we are not handing back a half-written
    file as though it were a report.
    """
    previous = -1
    while time.monotonic() < deadline:
        if target.exists():
            size = target.stat().st_size
            if size and size == previous:
                data = target.read_bytes()
                if data.startswith(b"%PDF"):
                    return data
            previous = size
        time.sleep(0.1)
    return b""


def _render_with_chromium(html: str) -> bytes:
    binary = find_chromium()
    if not binary:
        raise PdfUnavailable(engine_status()[1])

    timeout = getattr(settings, "PDF_TIMEOUT_SECONDS", 120)
    # `ignore_cleanup_errors`, because Chromium's process is still letting go
    # of its profile directory when this block exits. Without it the export
    # raises after the PDF has been rendered successfully, and the reader is
    # told the report failed because a scratch file could not be deleted.
    with tempfile.TemporaryDirectory(
        prefix="aji-pdf-", ignore_cleanup_errors=True
    ) as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "report.html"
        target = tmp_path / "report.pdf"
        source.write_text(html, encoding="utf-8")

        started = time.monotonic()
        result = subprocess.run(
            [
                binary,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--no-first-run",
                "--disable-extensions",
                "--disable-background-networking",
                # A profile of its own. Without it the launcher can hand the
                # job to the browser the person already has open -- which does
                # not print, and touches their session.
                f"--user-data-dir={tmp_path / 'profile'}",
                "--no-pdf-header-footer",
                # Give webfonts and layout time to settle before printing.
                "--virtual-time-budget=10000",
                f"--print-to-pdf={target}",
                source.as_uri(),
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )

        data = _await_pdf(target, deadline=started + timeout)
        if not data:
            detail = (result.stderr or b"").decode("utf-8", "replace")[-600:]
            raise PdfUnavailable(
                f"Chromium exited {result.returncode} without writing a PDF "
                f"within {timeout}s. {detail}".strip()
            )
        return data


def _render_with_weasyprint(html: str) -> bytes:
    try:
        from weasyprint import HTML
    except Exception as exc:                        # pragma: no cover
        raise PdfUnavailable(f"WeasyPrint is not usable: {exc}") from exc
    return HTML(string=html, base_url=str(settings.BASE_DIR)).write_pdf()


def render_pdf(html: str) -> bytes:
    """Render HTML to PDF bytes with the configured engine."""
    engine = getattr(settings, "PDF_ENGINE", "chromium")
    if engine == "weasyprint":
        return _render_with_weasyprint(html)
    if engine == "chromium":
        return _render_with_chromium(html)
    raise PdfUnavailable(f"Unknown PDF_ENGINE '{engine}'.")
