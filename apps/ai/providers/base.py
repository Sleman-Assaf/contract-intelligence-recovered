"""The provider contract (§11, §46, §60).

Business services depend on this interface and nothing else. No module outside
`apps.ai.providers` may import a vendor SDK — that is what makes swapping one
model service for another a configuration change rather than a rewrite.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import Any, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def repair_json(text: str) -> str:
    """Mend the punctuation slips models make in otherwise complete JSON.

    Only punctuation changes, never a word: an unescaped quote inside a
    string (a contract's defined terms -- the "Client" -- copied into the
    reasoning) is escaped, a trailing comma is dropped, and text around the
    outermost object is cut. The result still has to pass the strict schema,
    so a repair can rescue an answer but never invent one (§44).
    """
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]

    out: list[str] = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if not in_string:
            if char == '"':
                in_string = True
            out.append(char)
        elif char == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        elif char == '"':
            if _closes_string(text, i + 1):
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
        else:
            out.append(char)
        i += 1
    return _TRAILING_COMMA.sub(r"\1", "".join(out))


def _closes_string(text: str, j: int) -> bool:
    """Whether a quote ending at `j` closes its string: what follows it must
    be JSON structure -- `:` after a key, `}` / `]` / the end, or a comma that
    leads to the next key or element. `"Client", who ...` is prose."""
    n = len(text)
    while j < n and text[j] in " \t\r\n":
        j += 1
    if j >= n or text[j] in ":}]":
        return True
    if text[j] != ",":
        return False
    j += 1
    while j < n and text[j] in " \t\r\n":
        j += 1
    return j >= n or text[j] in "\"}]{[-0123456789tfn"


#: How the services seen so far phrase "wait this long before asking again".
#: Written against Google's wording during an end-to-end run on its free tier;
#: kept provider-agnostic here because guessing a delay the service already
#: told us is how a retry budget gets wasted.
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry\s+in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.I),
    re.compile(r"'retryDelay':\s*'([0-9]+(?:\.[0-9]+)?)s'", re.I),
    re.compile(r'"retryDelay":\s*"([0-9]+(?:\.[0-9]+)?)s"', re.I),
)


def _retry_after_from(message: str) -> float | None:
    """Seconds the service asked us to wait, if it said."""
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(message)
        if match:
            try:
                return float(match.group(1))
            except ValueError:  # pragma: no cover - regex guarantees a number
                return None
    return None


# ── Errors ───────────────────────────────────────────────────────────────

class AIProviderError(Exception):
    """Base class. Callers catch this rather than any vendor exception."""


class AIProviderUnavailable(AIProviderError):
    """Network failure, timeout, or the service is down. Retryable."""


class AIRateLimited(AIProviderError):
    """Retryable after a delay."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AIPermanentError(AIProviderError):
    """The request will never succeed as written - a retired model, a rejected
    key, a malformed request. Retrying wastes quota and hides the real error
    from an operator."""


class AIProviderNotConfigured(AIProviderError):
    """Credentials or endpoint missing. Not retryable — tell an operator."""


class AIResponseInvalid(AIProviderError):
    """The model returned something that failed schema validation (§44)."""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


# ── Value objects ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ImageRegion:
    """A cropped page region sent for vision verification."""

    image_bytes: bytes
    mime_type: str = "image/png"
    page_number: int | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class AICallResult:
    """Everything the audit and cost trail needs about one call (§45)."""

    parsed: BaseModel
    provider: str
    model: str
    request_type: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    attempts: int = 1
    raw_text: str = field(default="", repr=False)
    #: Answered from AIResponseCache: the same prompt was asked before.
    cached: bool = False


# ── Interface ────────────────────────────────────────────────────────────

class AIProvider(abc.ABC):
    """Capabilities the application needs from any model backend."""

    name: str = "base"

    #: Whether `verify_text_region` can actually read the pixels it is given.
    #: Declared rather than assumed: a provider that quietly cannot see is
    #: indistinguishable from one that can, because the model answers either
    #: way -- it simply invents the contents. Anything that depends on vision
    #: must ask this first, not call and hope.
    supports_vision: bool = False

    # -- lifecycle ------------------------------------------------------
    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True when this provider has everything it needs to make a call."""

    @abc.abstractmethod
    def health_check(self) -> tuple[bool, str]:
        """(ok, human-readable detail). Used by the admin status page."""

    # -- text -----------------------------------------------------------
    @abc.abstractmethod
    def classify_clause(
        self, *, clause_text: str, categories: list[str], language: str = "ar"
    ) -> AICallResult:
        """Classify one clause, allowing alternatives when unsure (§16)."""

    def classify_clauses(
        self, *, clauses: list[tuple[int, str, str]], categories: list[str]
    ) -> AICallResult:
        """Classify several (id, number, text) clauses in one call.

        Optional: a provider that does not offer it is asked one clause at a
        time instead, which is always correct, only slower.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def assess_clause(
        self,
        *,
        clause_text: str,
        standard_position: str,
        acceptable_range: str,
        red_flags: str,
        context_clauses: list[str],
        contract_context: dict[str, Any],
        language: str = "ar",
    ) -> AICallResult:
        """Semantic comparison of one clause against one standard (§19).

        `context_clauses` carries referenced clauses and definitions so the
        clause is never judged in isolation (§20).
        """

    def translate_prose(
        self, *, fields: dict[str, str], target_language: str
    ) -> AICallResult:
        """Restate an existing finding in another language.

        Concrete rather than abstract: a provider that cannot do this should
        say so plainly instead of blocking every other provider from being
        written.
        """
        raise AIProviderNotConfigured(
            f"{type(self).__name__} does not implement translate_prose."
        )

    def translate_clause(
        self, *, clause_text: str, target_language: str
    ) -> AICallResult:
        """Render a clause in another language, for reading beside it."""
        raise AIProviderNotConfigured(
            f"{type(self).__name__} does not implement translate_clause."
        )

    def normalize_standard(
        self, *, text: str, categories: list[dict], part: int = 1, parts: int = 1
    ) -> AICallResult:
        """Restate the positions in part of an uploaded standards document in
        the uniform form (prompts.normalize_standard)."""
        raise AIProviderNotConfigured(
            f"{type(self).__name__} does not implement normalize_standard."
        )

    @abc.abstractmethod
    def verify_finding(
        self,
        *,
        clause_text: str,
        standard_position: str,
        system_result: str,
        previous_assessment: dict[str, Any],
        language: str = "ar",
    ) -> AICallResult:
        """Independent verification pass (§21).

        Implementations must NOT simply re-ask the original question. The
        prompt frames a different task: find what the first pass got wrong.
        """

    @abc.abstractmethod
    def answer_question(
        self,
        *,
        question: str,
        document_excerpts: list[str],
        standards_excerpts: list[str],
        language: str = "ar",
        history: list[dict] | None = None,
        recalled: list[dict] | None = None,
        image: "ImageRegion | None" = None,
    ) -> AICallResult:
        """Contract advisor (§38), grounded in stored canonical text.

        `image` is a photo the reader attached to the question -- a page, a
        clause, a letter. A provider that cannot see raises
        AIProviderNotConfigured rather than answering from the words alone.

        `history` is the conversation so far, oldest first, as
        `{"role": "user"|"assistant", "body": str}`. Optional: a provider that
        answers each question on its own still satisfies this interface.
        """

    def answer_question_stream(self, **kwargs):
        """The same answer, as it is written: yields pieces of the model's raw
        output (text), then the validated AICallResult last.

        Optional. A provider that cannot stream yields its whole answer once,
        at the end -- the reader waits as before, and nothing breaks.
        """
        kwargs.pop("cancel", None)       # nothing to abort mid-way: it answers at once
        result = self.answer_question(**kwargs)
        if result.raw_text:
            yield result.raw_text
        yield result

    # -- vision ---------------------------------------------------------
    @abc.abstractmethod
    def verify_text_region(
        self, *, region: ImageRegion, language: str = "ar", strict: bool = False
    ) -> AICallResult:
        """Read a cropped region independently of OCR (§9, §10).

        The OCR reading is never passed in: the model reads the pixels, and
        apps/documents/verification.py compares the two readings. `strict` is
        the second, closer reading of a disputed critical value. Vision is a
        verifier of specific regions -- never the primary OCR engine.
        """

    # -- shared helper --------------------------------------------------
    @staticmethod
    def parse_or_raise(schema: Type[T], raw_text: str) -> T:
        """Validate raw model text against a schema, or fail loudly.

        Tolerates the common wrapping of JSON in a markdown fence; does not
        tolerate missing required fields, which are never guessed (§44).
        """
        import json
        import re

        text = (raw_text or "").strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if fence:
            text = fence.group(1).strip()
        if not text:
            raise AIResponseInvalid("Model returned an empty response.", raw=raw_text)

        try:
            # strict=False: a raw line break inside a string is a formatting
            # slip, not a different answer.
            payload = json.loads(text, strict=False)
        except json.JSONDecodeError as exc:
            try:
                payload = json.loads(repair_json(text), strict=False)
            except json.JSONDecodeError:
                raise AIResponseInvalid(
                    f"Model response was not valid JSON: {exc}", raw=raw_text
                ) from exc

        try:
            return schema.model_validate(repair_keys(schema, payload))
        except ValidationError as exc:
            raise AIResponseInvalid(
                f"Model response did not match {schema.__name__}: {exc}", raw=raw_text
            ) from exc


def repair_keys(schema, payload):
    """Rename an obviously misspelt key to the field it was meant to be.

    On a real run the model wrote "business_imparket" for "business_impact"
    once in eighty answers, the whole clause failed validation three times,
    and the analysis was marked incomplete. A key is renamed only when it is
    unknown, its closest field is missing from the answer, and the two are
    near-identical; any other unexpected key is left for validation to
    reject, as before. Lists of nested answers (a batch) are repaired item
    by item.
    """
    import difflib
    import typing

    from pydantic import BaseModel

    if isinstance(payload, list):
        return [repair_keys(schema, item) for item in payload]
    if not isinstance(payload, dict) or not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        return payload
    fields = schema.model_fields
    out = dict(payload)
    for key in list(out):
        if key in fields:
            continue
        missing = [name for name in fields if name not in out]
        match = difflib.get_close_matches(key, missing, n=1, cutoff=0.75)
        if match:
            out[match[0]] = out.pop(key)
    for name, info in fields.items():
        value = out.get(name)
        annotation = info.annotation
        inner = typing.get_args(annotation)[0] if typing.get_origin(annotation) is list and typing.get_args(annotation) else annotation
        if isinstance(value, (dict, list)) and isinstance(inner, type) and issubclass(inner, BaseModel):
            out[name] = repair_keys(inner, value)
    return out
