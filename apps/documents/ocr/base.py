"""OCR engine abstraction (§7).

The project must not be built around one OCR library. Everything downstream
depends on these dataclasses, never on Tesseract's output shape, so swapping to
PaddleOCR or Azure Document Intelligence is a new adapter and a settings change.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


class OCRError(Exception):
    """Base for OCR failures."""


class OCREngineNotAvailable(OCRError):
    """The engine binary or service is missing. Not retryable."""


@dataclass(frozen=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class OCRWord:
    text: str
    bbox: BBox
    confidence: float          # 0–100
    line_index: int = 0
    block_index: int = 0


@dataclass
class OCRLine:
    text: str
    bbox: BBox
    confidence: float
    words: list[OCRWord] = field(default_factory=list)


@dataclass
class OCRBlock:
    text: str
    bbox: BBox
    confidence: float
    lines: list[OCRLine] = field(default_factory=list)
    reading_order: int = 0

    @property
    def min_word_confidence(self) -> float | None:
        """Worst word in the block.

        This is the number that matters for contract-critical values: a page
        can average 96% while the token '10%' sits at 72%.
        """
        confidences = [w.confidence for line in self.lines for w in line.words]
        return min(confidences) if confidences else None


@dataclass
class OCRPageResult:
    page_number: int
    blocks: list[OCRBlock]
    mean_confidence: float
    language: str = ""
    engine: str = ""
    width: float | None = None
    height: float | None = None

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)

    @property
    def word_count(self) -> int:
        return sum(len(line.words) for b in self.blocks for line in b.lines)


class OCREngine(abc.ABC):
    """What the pipeline needs from any OCR backend."""

    name: str = "base"

    @abc.abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """(available, detail). Checked before a job starts, not mid-pipeline."""

    @abc.abstractmethod
    def recognise_page(
        self, image_bytes: bytes, *, page_number: int, languages: str
    ) -> OCRPageResult:
        """Recognise one rasterised page.

        Must populate word-level confidences and bounding boxes wherever the
        engine provides them — the quality gate and the source viewer both
        depend on them.
        """
