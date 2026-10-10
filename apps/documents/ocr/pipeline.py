"""Preparing a scanned page before OCR, and reading it (§7, §8).

Measured on six real signed contracts (319 scanned pages), plain OCR of the
page as rendered lost exactly the values a review depends on:

* pages scanned sideways -- the fee tables of one agreement, the staffing
  tables of another -- came back as noise, every rate and total gone;
* the blue company stamp printed on every page was read as text, wedged into
  the middle of clauses, and covered the words beneath it;
* English pages read with the Arabic model mixed Arabic letters into English
  words ("AJi" became Arabic glyphs at 94% confidence);
* a slight tilt broke lines apart.

So each page is rendered, cleaned and turned upright before it is read:

    render (OCR_RENDER_DPI) -> drop stamp ink -> upright (Tesseract OSD)
        -> straighten (small skew) -> pick the language model -> OCR

The text is still exactly what the engine read -- nothing is rewritten here.
Every box is mapped back to the page as stored (PDF points, unrotated), so the
source viewer and the vision crop keep pointing at the right place.
"""
from __future__ import annotations

import io
import logging
import math
import re
from dataclasses import dataclass, field, replace

from django.conf import settings

from .base import BBox, OCRBlock, OCRLine, OCRPageResult

logger = logging.getLogger(__name__)

PDF_POINTS_PER_INCH = 72


def render_dpi() -> int:
    return int(getattr(settings, "OCR_RENDER_DPI", 300))


# ── image steps ──────────────────────────────────────────────────────────

def mask_stamp_ink(image):
    """The page with light coloured ink (stamps, seals, initials) made white.

    Stamps are bright, saturated blue, violet or red; contract text is black
    or dark grey, and dark coloured text (navy headings) is darker than any
    stamp ink measured. Only pixels that are both clearly coloured and light
    are removed, so a dark blue heading survives. Returns (image, share of
    pixels removed).
    """
    import numpy as np
    from PIL import Image

    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    hi = rgb.max(axis=2)
    lo = rgb.min(axis=2)
    saturation = (hi - lo) / np.maximum(hi, 1)
    light = hi >= int(255 * settings.OCR_STAMP_MIN_LIGHTNESS)
    coloured = saturation >= settings.OCR_STAMP_MIN_SATURATION
    bluish = (b >= np.maximum(r, g) + 20)           # blue / violet stamps
    reddish = (r >= np.maximum(g, b) + 40)          # red / pink seals
    ink = light & coloured & (bluish | reddish)
    if not ink.any():
        return image.convert("RGB"), 0.0
    # Only ink that forms a stamp-sized patch. Blue *text* -- an email
    # printed with the replies in blue -- runs across the page in lines, and
    # removing it erased the very figures being looked for.
    mask = ink & _stamp_patches(ink)
    share = float(mask.mean())
    if not mask.any():
        return image.convert("RGB"), 0.0
    out = rgb.copy()
    out[mask] = 255
    # The faint halo a stamp leaves behind (its lightest, least saturated
    # edge) made Tesseract treat the area as a picture and skip the line of
    # text beside it. Inside the stamp patches, anything light goes white.
    halo = _stamp_patches(ink, grow=2) & (hi >= 170)
    out[halo] = 255
    return Image.fromarray(out.astype("uint8"), "RGB"), share


def _stamp_patches(ink, *, cell: int = 24, grow: int = 1):
    """Pixels inside compact patches of coloured ink -- the shape of a stamp.

    The page is divided into cells; cells holding coloured ink are grouped
    into connected patches. A patch no wider than OCR_STAMP_MAX_WIDTH and no
    taller than OCR_STAMP_MAX_HEIGHT of the page is a stamp, seal or set of
    initials; a wider one is coloured text and is kept.
    """
    import numpy as np

    h, w = ink.shape
    gh, gw = -(-h // cell), -(-w // cell)
    padded = np.zeros((gh * cell, gw * cell), dtype=bool)
    padded[:h, :w] = ink
    counts = padded.reshape(gh, cell, gw, cell).sum(axis=(1, 3))
    marked = counts >= max(3, cell * cell // 60)
    keep = np.zeros_like(marked)
    seen = np.zeros_like(marked)
    max_w = settings.OCR_STAMP_MAX_WIDTH * gw
    max_h = settings.OCR_STAMP_MAX_HEIGHT * gh
    for sy, sx in zip(*np.nonzero(marked)):
        if seen[sy, sx]:
            continue
        stack, cells = [(sy, sx)], []
        seen[sy, sx] = True
        while stack:
            y, x = stack.pop()
            cells.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < gh and 0 <= nx < gw and marked[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        ys = [c[0] for c in cells]
        xs = [c[1] for c in cells]
        if (max(xs) - min(xs) + 1) <= max_w and (max(ys) - min(ys) + 1) <= max_h:
            y0, y1 = max(0, min(ys) - grow), min(gh, max(ys) + grow + 1)
            x0, x1 = max(0, min(xs) - grow), min(gw, max(xs) + grow + 1)
            keep[y0:y1, x0:x1] = True
    full = np.repeat(np.repeat(keep, cell, axis=0), cell, axis=1)
    return full[:h, :w]


@dataclass(frozen=True)
class Orientation:
    rotate: int = 0              # degrees clockwise that make the page upright
    confidence: float = 0.0
    script: str = ""             # "Latin", "Arabic", ...
    script_confidence: float = 0.0


def detect_orientation(image) -> Orientation:
    """Tesseract's orientation and script detection, on a reduced copy.

    A page with too little text (a stamp, a divider, a drawing) has no
    orientation to find; it is left as it is.
    """
    import pytesseract

    from .tesseract import find_tesseract

    binary = find_tesseract()
    if binary:
        pytesseract.pytesseract.tesseract_cmd = binary
    small = _scaled(image.convert("L"), max_side=2000)
    try:
        osd = pytesseract.image_to_osd(
            small, output_type=pytesseract.Output.DICT,
            config="--psm 0 -c min_characters_to_try=10")
    except Exception as exc:          # too few characters, or no osd data
        logger.debug("OSD skipped: %s", exc)
        return Orientation()
    return Orientation(
        rotate=int(osd.get("rotate", 0)) % 360,
        confidence=float(osd.get("orientation_conf", 0.0) or 0.0),
        script=str(osd.get("script", "") or ""),
        script_confidence=float(osd.get("script_conf", 0.0) or 0.0),
    )


def estimate_skew(image, *, limit: float = 4.0, step: float = 0.25) -> float:
    """The small rotation (degrees, counter-clockwise) that levels the lines.

    The angle at which the rows of ink are most sharply separated -- the row
    profile with the greatest variance -- is the one where the text lines lie
    flat. Measured on a reduced, thresholded copy.
    """
    import numpy as np

    gray = _scaled(image.convert("L"), max_side=1200)
    ink = gray.point(lambda v: 255 if v < 150 else 0)
    if not np.asarray(ink).any():
        return 0.0
    best, best_score = 0.0, -1.0
    angles = [round(-limit + i * step, 2) for i in range(int(2 * limit / step) + 1)]
    for angle in angles:
        rows = np.asarray(ink.rotate(angle, expand=False), dtype=np.float32).sum(axis=1)
        score = float(np.var(rows))
        if score > best_score:
            best, best_score = angle, score
    return best


def _scaled(image, *, max_side: int):
    from PIL import Image

    longest = max(image.size)
    if longest <= max_side:
        return image
    ratio = max_side / longest
    return image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                        Image.BILINEAR)


# ── coordinates ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Transform:
    """How the image the engine read relates to the page as rendered."""

    dpi: int
    rendered_size: tuple[int, int]                  # W0, H0
    rotate: int = 0                                 # clockwise, 0/90/180/270
    upright_size: tuple[int, int] = (0, 0)          # W1, H1
    skew: float = 0.0                               # counter-clockwise degrees
    final_size: tuple[int, int] = (0, 0)            # W2, H2

    def to_points(self, box) -> tuple[float, float, float, float]:
        """A box in the read image's pixels, as PDF points on the stored page."""
        x0, y0, x1, y1 = box
        corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
        mapped = [self._back(x, y) for x, y in corners]
        scale = PDF_POINTS_PER_INCH / self.dpi
        xs = [p[0] * scale for p in mapped]
        ys = [p[1] * scale for p in mapped]
        return (min(xs), min(ys), max(xs), max(ys))

    def _back(self, x: float, y: float) -> tuple[float, float]:
        if self.skew:
            x, y = _unrotate(x, y, self.skew, self.final_size, self.upright_size)
        if self.rotate:
            # The upright image is the render turned clockwise by `rotate`,
            # i.e. PIL's counter-clockwise rotation by -rotate.
            x, y = _unrotate(x, y, -self.rotate, self.upright_size, self.rendered_size)
        return x, y


def _unrotate(x, y, angle, out_size, in_size):
    """Undo PIL's `rotate(angle, expand=True)` (counter-clockwise, y down)."""
    theta = math.radians(angle)
    dx, dy = x - out_size[0] / 2, y - out_size[1] / 2
    ox = dx * math.cos(theta) - dy * math.sin(theta)
    oy = dx * math.sin(theta) + dy * math.cos(theta)
    return ox + in_size[0] / 2, oy + in_size[1] / 2


# ── the page reading ─────────────────────────────────────────────────────

@dataclass
class PageReading:
    """One page as OCR read it, with boxes already in PDF points.

    `result` is exactly what the engine read. `corrected` holds, per block
    (by reading order), the text after the recorded repairs -- second
    readings of figures and the rules in ocr/corrections.py -- for blocks that
    needed any.
    """

    result: OCRPageResult
    transform: Transform
    languages: str
    orientation: Orientation = field(default_factory=Orientation)
    stamp_share: float = 0.0
    corrected: dict = field(default_factory=dict)
    recovered_blocks: int = 0

    @property
    def text(self) -> str:
        return self.result.text

    @property
    def corrected_text(self) -> str:
        return "\n".join(
            self.corrected[b.reading_order].text if b.reading_order in self.corrected else b.text
            for b in self.result.blocks)

    @property
    def signals(self) -> dict:
        """What was done to the page, kept with the page as evidence."""
        return {
            "dpi": self.transform.dpi,
            "rotated": self.transform.rotate,
            "skew": round(self.transform.skew, 2),
            "script": self.orientation.script,
            "languages": self.languages,
            "stamp_ink_removed": round(self.stamp_share, 4),
            "recovered_lines": self.recovered_blocks,
            "corrections": sum(len(c.corrections) for c in self.corrected.values()),
        }


def choose_languages(orientation: Orientation, default: str) -> str:
    """English-only for a page that is confidently Latin script.

    Read with ara+eng, English words on a stamped page picked up Arabic
    letters; Arabic pages, mixed pages and pages OSD could not judge keep the
    configured languages.
    """
    # OSD names a clean English page "Cyrillic" or "Greek" now and then (seen
    # on a typeset JV agreement); none of those scripts occurs in AJi's
    # contracts, so all three mean Latin here.
    if (orientation.script in ("Latin", "Cyrillic", "Greek")
            and orientation.script_confidence >= settings.OCR_LATIN_SCRIPT_MIN_CONFIDENCE):
        return getattr(settings, "OCR_LATIN_LANGUAGES", "eng")
    return default


def prepare_image(png: bytes, *, dpi: int):
    """Clean and turn the rendered page; returns (image, transform, orientation, stamp share)."""
    from PIL import Image

    image = Image.open(io.BytesIO(png))
    image.load()
    rendered = image.size
    stamp_share = 0.0
    if settings.OCR_REMOVE_STAMP_INK:
        image, stamp_share = mask_stamp_ink(image)
    else:
        image = image.convert("RGB")

    orientation = detect_orientation(image) if settings.OCR_AUTO_ROTATE else Orientation()
    rotate = orientation.rotate if orientation.confidence >= settings.OCR_ROTATE_MIN_CONFIDENCE else 0
    if rotate:
        image = image.rotate(-rotate, expand=True, fillcolor="white")
    upright = image.size

    skew = estimate_skew(image) if settings.OCR_DESKEW else 0.0
    if abs(skew) < settings.OCR_DESKEW_MIN_ANGLE:
        skew = 0.0
    if skew:
        image = image.rotate(skew, expand=True, fillcolor="white", resample=Image.BICUBIC)

    transform = Transform(dpi=dpi, rendered_size=rendered, rotate=rotate,
                          upright_size=upright, skew=skew, final_size=image.size)
    return image, transform, orientation, stamp_share


def read_image(png: bytes, *, dpi: int, page_number: int, engine=None,
               languages: str | None = None, known_terms: tuple[str, ...] = ()) -> PageReading:
    """Prepare and OCR one rendered page; boxes come back in PDF points."""
    from . import get_ocr_engine

    engine = engine or get_ocr_engine()
    image, transform, orientation, stamp_share = prepare_image(png, dpi=dpi)
    langs = languages or choose_languages(orientation, settings.OCR_LANGUAGES)

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    raw = engine.recognise_page(buffer.getvalue(), page_number=page_number, languages=langs)

    # Both passes below are local Tesseract work, never a call to the vision
    # model.
    is_tesseract = getattr(engine, "name", "") == "tesseract"
    recovered = 0
    if settings.OCR_RECOVER_MISSED_LINES and is_tesseract:
        raw, recovered = recover_missed_lines(image, raw, engine, languages=langs,
                                              page_number=page_number)

    corrected = {}
    if (settings.OCR_SECOND_READING and "ara" in langs.split("+")
            and is_tesseract):
        corrected = second_reading(image, raw, engine, page_number=page_number)
    corrected = _apply_rules(raw, corrected, known_terms=known_terms)

    result = _in_points(raw, transform)
    return PageReading(result=result, transform=transform, languages=langs,
                       orientation=orientation, stamp_share=stamp_share, corrected=corrected,
                       recovered_blocks=recovered)


# ── lines the layout analysis skipped ────────────────────────────────────

def recover_missed_lines(image, raw: OCRPageResult, engine, *, languages: str,
                         page_number: int) -> tuple[OCRPageResult, int]:
    """Read again the bands of ink no recognised word covers.

    Tesseract's page layout sometimes skips a whole line -- seen on a
    particulars sheet, where "قيمة غرامة التأخير: 170 دينار" (the delay
    damages, of all lines) was not in the output at all, though the line was
    clean. Ink that lies outside every recognised word is grouped into
    horizontal bands; each band is cropped and read on its own, and what it
    yields is added as a block of its own, flagged as recovered.
    """
    import numpy as np

    gray = np.asarray(image.convert("L"))
    h, w = gray.shape
    ink = gray < 140
    covered = np.zeros_like(ink)
    for block in raw.blocks:
        for line in block.lines:
            for word in line.words:
                b = word.bbox
                covered[max(0, int(b.y0) - 6):min(h, int(b.y1) + 6),
                        max(0, int(b.x0) - 6):min(w, int(b.x1) + 6)] = True
    loose = ink & ~covered
    threshold = max(8, int(w * 0.002))
    if not (loose.sum(axis=1) >= threshold).any():
        return raw, 0
    # Long straight rules (frames, table borders) are not text. Found on a
    # quarter-size copy -- a rule is long at any size -- since walking every
    # row of a 300 dpi page took seconds.
    loose &= ~_rules_mask(ink, h, w)
    rows = loose.sum(axis=1)

    bands, start = [], None
    for y, count in enumerate(rows):
        if count >= threshold and start is None:
            start = y
        elif count < threshold and start is not None:
            bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, h))

    min_h = max(10, int(render_dpi() * 0.05))       # ~ 1.3 mm
    max_h = int(render_dpi() * 0.6)                 # ~ 15 mm: taller is a picture
    extra = []
    # The bands with the most missed ink first, a few per page: signatures,
    # handwriting and stamp remains also leave loose ink, and reading every
    # such band cost seconds a page for nothing.
    bands = [b for b in bands if min_h <= b[1] - b[0] <= max_h]
    bands.sort(key=lambda b: int(loose[b[0]:b[1]].sum()), reverse=True)
    for y0, y1 in sorted(bands[:settings.OCR_RECOVER_MAX_BANDS]):
        cols = np.nonzero(loose[y0:y1].any(axis=0))[0]
        if cols.size == 0:
            continue
        top, bottom = max(0, y0 - 12), min(h, y1 + 12)
        # A line set as "label ........ value" has a wide gap in it, and
        # read as one line the engine returned nothing at all: each piece
        # between wide gaps is read on its own.
        pieces = []
        for c0, c1 in _segments(cols, gap=int(render_dpi() * 0.35)):
            x0, x1 = max(0, c0 - 30), min(w, c1 + 30)
            found = _read_crop(image, (x0, top, x1, bottom), engine, languages=languages,
                               page_number=page_number, text_height=y1 - y0)
            words = [w_ for w_ in (found or []) if w_.confidence >= 50 and w_.text.strip()]
            if sum(len(w_.text) for w_ in words) < 2:
                continue
            pieces.append(words)
        if not pieces:
            continue
        # Pieces in reading order: right to left on an Arabic line.
        right_to_left = any(ARABIC_LETTER.search(x.text) for piece in pieces for x in piece)
        pieces.sort(key=lambda piece: piece[0].bbox.x0, reverse=right_to_left)
        shifted = [x for piece in pieces for x in piece]
        if sum(len(x.text) for x in shifted) < 3:
            continue
        box = BBox(min(x.bbox.x0 for x in shifted), min(x.bbox.y0 for x in shifted),
                   max(x.bbox.x1 for x in shifted), max(x.bbox.y1 for x in shifted))
        text = " ".join(x.text for x in shifted)
        confidence = sum(x.confidence for x in shifted) / len(shifted)
        line = OCRLine(text=text, bbox=box, confidence=confidence, words=shifted)
        extra.append(OCRBlock(text=text, bbox=box, confidence=confidence, lines=[line]))
    if not extra:
        return raw, 0
    blocks = list(raw.blocks)
    for item in extra:
        blocks.append(replace(item, reading_order=len(blocks)))
    return replace(raw, blocks=blocks), len(extra)


#: Text height (pixels) line crops are scaled to before a single-line read.
#: Tesseract's line mode returned nothing at all for a clean 55 px tall line
#: of a 300 dpi render, and read it correctly at half the size.
LINE_TEXT_HEIGHT = 30


def _read_crop(image, box, engine, *, languages: str, page_number: int, text_height: float):
    """Read one cropped line at a normalised size; word boxes come back in
    the coordinates of `image`. None when the engine fails."""
    from PIL import Image

    x0, y0, x1, y1 = box
    crop = image.crop((x0, y0, x1, y1))
    scale = min(1.5, max(0.3, LINE_TEXT_HEIGHT / max(1.0, text_height)))
    if abs(scale - 1.0) > 0.05:
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))),
                           Image.LANCZOS)
    buffer = io.BytesIO()
    crop.save(buffer, "PNG")
    try:
        found = engine.recognise_page(buffer.getvalue(), page_number=page_number,
                                      languages=languages, single_line=True)
    except Exception as exc:          # a second look must never fail the page
        logger.debug("line read skipped: %s", exc)
        return None
    return [replace(w, bbox=BBox(w.bbox.x0 / scale + x0, w.bbox.y0 / scale + y0,
                                 w.bbox.x1 / scale + x0, w.bbox.y1 / scale + y0))
            for b in found.blocks for ln in b.lines for w in ln.words]


def _segments(cols, *, gap: int):
    """Runs of column indices split wherever they jump by more than `gap`."""
    out, start, prev = [], int(cols[0]), int(cols[0])
    for c in cols[1:]:
        c = int(c)
        if c - prev > gap:
            out.append((start, prev))
            start = c
        prev = c
    out.append((start, prev))
    return out


def _rules_mask(ink, h: int, w: int, *, factor: int = 4):
    """Pixels on long horizontal or vertical rules, found at 1/`factor` size."""
    import numpy as np

    gh, gw = h // factor, w // factor
    if gh < 4 or gw < 4:
        return np.zeros_like(ink)
    small = ink[:gh * factor, :gw * factor].reshape(gh, factor, gw, factor).any(axis=(1, 3))
    rules = (_long_runs(small, axis=0, length=int(gh * 0.06))
             | _long_runs(small, axis=1, length=int(gw * 0.06)))
    full = np.zeros_like(ink)
    full[:gh * factor, :gw * factor] = np.repeat(np.repeat(rules, factor, axis=0), factor, axis=1)
    return full


def _long_runs(ink, *, axis: int, length: int):
    """Pixels on straight runs of ink at least `length` long along `axis`."""
    import numpy as np

    grid = ink if axis == 0 else ink.T
    rows, cols = grid.shape
    forward = np.zeros(grid.shape, dtype=np.int32)
    run = np.zeros(cols, dtype=np.int32)
    for r in range(rows):
        run = (run + 1) * grid[r]
        forward[r] = run
    backward = np.zeros(grid.shape, dtype=np.int32)
    run = np.zeros(cols, dtype=np.int32)
    for r in range(rows - 1, -1, -1):
        run = (run + 1) * grid[r]
        backward[r] = run
    hit = (forward + backward - 1) >= max(2, length)
    return hit if axis == 0 else hit.T


# ── second reading of figures on Arabic lines ────────────────────────────

#: A line worth a second look: it states a duration, an amount, a rate.
_KEY_LINE = re.compile(
    r"مدة|يوم|أيام|غرامة|قيمة|مبلغ|نسبة|دينار|دولار|يورو|سقف|كفالة|ضمان|سلفة|محتجز|شهر|أشهر|سنة|%|٪")
_LATIN_DIGIT = re.compile(r"[0-9]")


def second_reading(image, raw: OCRPageResult, engine, *, page_number: int) -> dict:
    """Re-read the figures of key Arabic lines with the English model.

    The Arabic model drops or mangles Latin digits set in another font: a
    contract's "مدة العقد: 180 يوما" came back as "0 يوما" at 95% confidence,
    while the English model read "180" from the same line. Each key line
    holding Latin digits is cropped from the prepared image and read again
    with the English model only; a number the two readings see differently,
    in the same place on the line, is replaced by the English reading and
    recorded. Arabic-Indic digits, which the English model cannot read, are
    left alone.
    """
    from .corrections import Corrected, Correction

    out: dict = {}
    for block in raw.blocks:
        changed, notes, lines = False, [], []
        for line in block.lines:
            words = [w.text for w in line.words]
            if (ARABIC_LETTER.search(line.text) and _LATIN_DIGIT.search(line.text)
                    and _KEY_LINE.search(line.text)):
                for index, replacement in _reread_line(image, line, engine, page_number):
                    notes.append(Correction("second_reading", words[index], replacement,
                                            "figure read again with the English OCR model"))
                    words[index] = replacement
                    changed = True
            lines.append(" ".join(words))
        if changed:
            out[block.reading_order] = Corrected("\n".join(lines), notes)
    return out


ARABIC_LETTER = re.compile(r"[؀-ۿ]")


#: "(5):", "(16)", ")4(", "12-", "3." -- a clause or item number, not a figure.
_NUMBERING = re.compile(r"(?:[()\[\]]\s*\d{1,2}\s*[()\[\]]?|\d{1,2}\s*[()\[\]])\s*[:.\-–]?|\d{1,2}\s*[:\-–]")
#: A bracket between digits: never a figure.
_BRACKET_INSIDE = re.compile(r"\d\s*[()\[\]]\s*\d")


def _reread_line(image, line: OCRLine, engine, page_number: int):
    """(word index, new text) for each figure the English reading saw differently.

    Each figure is read in a small window around it -- a few character
    heights either side -- rather than with the whole line: across a long
    Arabic line the English model returned no digits at all.
    """
    lang = getattr(settings, "OCR_LATIN_LANGUAGES", "eng")
    height = max(8.0, line.bbox.height)
    changes = []
    for index, word in enumerate(line.words):
        if not _LATIN_DIGIT.search(word.text) or ARABIC_LETTER.search(word.text):
            continue
        if _NUMBERING.fullmatch(word.text.strip()):
            # An article or item number -- "(5):", "(16)", "12-" -- not a
            # figure: re-read in English it came back as "5(5" and "3(16",
            # and the article it opens was lost.
            continue
        reach = 3.5 * height
        box = (max(0, int(word.bbox.x0 - reach)), max(0, int(line.bbox.y0 - 10)),
               min(image.width, int(word.bbox.x1 + reach)), min(image.height, int(line.bbox.y1 + 10)))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            continue
        english = _read_crop(image, box, engine, languages=lang, page_number=page_number,
                             text_height=height)
        if not english:
            continue
        figures = [(w.bbox.x0, w.bbox.x1, _figure(w.text)) for w in english
                   if _LATIN_DIGIT.search(w.text) and _figure(w.text)]
        overlapping = [f for f in figures if _overlap(word.bbox.x0, word.bbox.x1, f[0], f[1]) >= 0.5]
        if len(overlapping) != 1:
            continue
        mine, theirs = _figure(word.text), overlapping[0][2]
        if theirs and _BRACKET_INSIDE.search(theirs):
            continue                          # "5(5": a bracket read into the number
        if theirs and _digits(theirs) != _digits(mine) and len(_digits(theirs)) >= len(_digits(mine)):
            changes.append((index, theirs))
    return changes


def _figure(text: str) -> str:
    """The number-ish part of a token: digits and the marks around them."""
    return re.sub(r"[^0-9%.,/()\-]", "", text).strip("()-,.")


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _overlap(a0, a1, b0, b1) -> float:
    """Horizontal overlap as a share of the narrower box."""
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    narrow = max(1.0, min(a1 - a0, b1 - b0))
    return inter / narrow


def _apply_rules(raw: OCRPageResult, corrected: dict, *, known_terms=()) -> dict:
    """Run the rules of ocr/corrections.py over every block's text."""
    from .corrections import Corrected, correct

    out = dict(corrected)
    for block in raw.blocks:
        base = out.get(block.reading_order)
        text = base.text if base else block.text
        fixed = correct(text, known_terms)
        if fixed.changed:
            out[block.reading_order] = Corrected(
                fixed.text, (base.corrections if base else []) + fixed.corrections)
    return out


def read_page(path, page_number: int, *, engine=None, languages: str | None = None,
              dpi: int | None = None, known_terms: tuple[str, ...] = ()) -> PageReading:
    """Render, prepare and OCR one page of a PDF."""
    from .. import extractors

    dpi = dpi or render_dpi()
    png = extractors.rasterise_page(path, page_number, dpi=dpi)
    return read_image(png, dpi=dpi, page_number=page_number, engine=engine, languages=languages,
                      known_terms=known_terms)


def _in_points(result: OCRPageResult, transform: Transform) -> OCRPageResult:
    def box(b: BBox) -> BBox:
        return BBox(*transform.to_points(b.as_tuple()))

    blocks = []
    for block in result.blocks:
        lines = [OCRLine(text=line.text, bbox=box(line.bbox), confidence=line.confidence,
                         words=[replace(w, bbox=box(w.bbox)) for w in line.words])
                 for line in block.lines]
        blocks.append(OCRBlock(text=block.text, bbox=box(block.bbox), confidence=block.confidence,
                               lines=lines, reading_order=block.reading_order))
    scale = PDF_POINTS_PER_INCH / transform.dpi
    w0, h0 = transform.rendered_size
    return replace(result, blocks=blocks, width=w0 * scale, height=h0 * scale)
