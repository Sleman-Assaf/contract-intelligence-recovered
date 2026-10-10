"""Tesseract adapter.

One implementation of `OCREngine`, not the shape the rest of the system is
built around. Arabic support requires the `ara` language data to be installed
alongside the binary.

Finding the binary is deliberately forgiving. Since vision OCR was withdrawn
this is the only way a scanned page becomes readable, so an installation that
is present but merely not on `PATH` must not look like one that is absent.
`TESSERACT_CMD` still wins when it is set; otherwise the usual per-machine
locations are searched, which is what makes a fresh checkout work without
anyone editing `.env` first.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.utils.translation import gettext as _

from .base import BBox, OCRBlock, OCREngine, OCREngineNotAvailable, OCRLine, OCRPageResult, OCRWord


def _candidate_paths() -> list[Path]:
    """Where a Tesseract install plausibly lives, in order of preference."""
    if sys.platform == "win32":
        names = ("Tesseract-OCR", "tesseract")
        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            # Per-user installs need no administrator rights, so on a locked
            # down machine this is often the only place it can go.
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        return [
            Path(root) / name / "tesseract.exe"
            for root in roots if root
            for name in names
        ]
    return [
        Path(p) for p in (
            "/usr/bin/tesseract", "/usr/local/bin/tesseract",
            "/opt/homebrew/bin/tesseract", "/snap/bin/tesseract",
        )
    ]


def find_tesseract() -> str | None:
    """The binary to use, or None when nothing is installed.

    Order: the configured path, then `PATH`, then the usual install locations.
    """
    configured = (getattr(settings, "TESSERACT_CMD", "") or "").strip()
    if configured:
        # Returned even when it does not exist: a path somebody set explicitly
        # and got wrong should produce a clear error naming that path, not a
        # silent fallback to some other copy on the machine.
        return configured

    on_path = shutil.which("tesseract")
    if on_path:
        return on_path

    for candidate in _candidate_paths():
        if candidate.is_file():
            return str(candidate)
    return None


class TesseractEngine(OCREngine):
    name = "tesseract"

    def __init__(self, binary_path: str | None = None):
        self._binary = binary_path or find_tesseract()
        self._ready: tuple[bool, str] | None = None

    # ── availability ─────────────────────────────────────────────────────
    def is_available(self) -> tuple[bool, str]:
        # Remembered once it has succeeded: asked before every page, the
        # check started two extra Tesseract processes per page.
        if self._ready is not None:
            return self._ready
        ready = self._check()
        if ready[0]:
            self._ready = ready
        return ready

    def _check(self) -> tuple[bool, str]:
        try:
            import pytesseract
        except ImportError:
            return False, _("pytesseract is not installed.")

        if self._binary:
            pytesseract.pytesseract.tesseract_cmd = self._binary

        try:
            version = pytesseract.get_tesseract_version()
        except Exception as exc:
            return False, _(
                "Tesseract was not found. Install it, then either put it on PATH "
                "or set TESSERACT_CMD to the tesseract executable. Looked at: "
                "%(looked)s. (%(error)s)"
            ) % {
                "looked": self._binary or _("PATH and the usual install locations"),
                "error": exc,
            }

        try:
            langs = set(pytesseract.get_languages(config=""))
        except Exception:
            langs = set()

        required = {p for p in settings.OCR_LANGUAGES.split("+") if p}
        missing = required - langs if langs else set()
        if missing:
            return False, _(
                "Tesseract %(version)s is installed but language data is "
                "missing: %(missing)s."
            ) % {"version": version, "missing": ", ".join(sorted(missing))}
        return True, _("Tesseract %(version)s available (languages: %(langs)s).") % {
            "version": version, "langs": settings.OCR_LANGUAGES,
        }

    # ── recognition ──────────────────────────────────────────────────────
    def recognise_page(
        self, image_bytes: bytes, *, page_number: int, languages: str,
        single_line: bool = False,
    ) -> OCRPageResult:
        """Recognise a page -- or, with `single_line`, one cropped line.

        Automatic page segmentation finds nothing at all in a crop of a
        single line; PSM 7 reads it as the one line it is.
        """
        available, detail = self.is_available()
        if not available:
            raise OCREngineNotAvailable(detail)

        import pytesseract
        from PIL import Image

        if self._binary:
            pytesseract.pytesseract.tesseract_cmd = self._binary

        image = Image.open(io.BytesIO(image_bytes))
        data = pytesseract.image_to_data(
            image,
            lang=languages,
            output_type=pytesseract.Output.DICT,
            # PSM 3 = automatic page segmentation; preserves multi-column layout,
            # which contracts frequently use for bilingual text.
            config="--psm 7" if single_line else "--psm 3",
        )

        # Tesseract returns a flat table; regroup into block → line → word.
        # A line is identified by block, paragraph *and* line number: line
        # numbers restart in every paragraph, and keyed without the paragraph
        # the first lines of two paragraphs -- often two columns side by side
        # -- were joined into one line ("...registered office at Whereas the
        # Consultant has entered...").
        grouped: dict[tuple[int, int, int], list[OCRWord]] = defaultdict(list)
        for i in range(len(data["text"])):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                confidence = float(data["conf"][i])
            except (TypeError, ValueError):
                confidence = -1.0
            if confidence < 0:            # -1 marks a non-text element
                continue
            grouped[(data["block_num"][i], data["par_num"][i], data["line_num"][i])].append(
                OCRWord(
                    text=text,
                    bbox=BBox(
                        float(data["left"][i]),
                        float(data["top"][i]),
                        float(data["left"][i] + data["width"][i]),
                        float(data["top"][i] + data["height"][i]),
                    ),
                    confidence=confidence,
                    line_index=int(data["line_num"][i]),
                    block_index=int(data["block_num"][i]),
                )
            )

        lines_by_block: dict[int, list[OCRLine]] = defaultdict(list)
        for (block_num, _par_num, _line_num), words in sorted(grouped.items()):
            if not words:
                continue
            lines_by_block[block_num].append(
                OCRLine(
                    text=" ".join(w.text for w in words),
                    bbox=_union([w.bbox for w in words]),
                    confidence=sum(w.confidence for w in words) / len(words),
                    words=words,
                )
            )

        blocks: list[OCRBlock] = []
        for order, (block_num, lines) in enumerate(sorted(lines_by_block.items())):
            blocks.append(
                OCRBlock(
                    text="\n".join(line.text for line in lines),
                    bbox=_union([line.bbox for line in lines]),
                    confidence=sum(line.confidence for line in lines) / len(lines),
                    lines=lines,
                    reading_order=order,
                )
            )

        all_words = [w for b in blocks for line in b.lines for w in line.words]
        mean = sum(w.confidence for w in all_words) / len(all_words) if all_words else 0.0

        return OCRPageResult(
            page_number=page_number,
            blocks=blocks,
            mean_confidence=mean,
            language=languages,
            engine=self.name,
            width=float(image.width),
            height=float(image.height),
        )


def _union(boxes: list[BBox]) -> BBox:
    return BBox(
        min(b.x0 for b in boxes),
        min(b.y0 for b in boxes),
        max(b.x1 for b in boxes),
        max(b.y1 for b in boxes),
    )
