"""AJi Brain adapter — the production provider (§60).

Speaks to AJi Brain, the in-house model service, at its `/api/general-chat/`
endpoint. The generic OpenAI shape is kept as a second wire format so the same
class still works against vLLM, SGLang or TGI if the deployment ever moves.

── The context guard ────────────────────────────────────────────────────────

AJi Brain forwards to Ollama, which defaults to a 4096-token window and, when a
prompt overflows it, **silently discards the head and keeps the tail**. No
error, no warning, no flag on the response. Measured against the live service:
a 42,000-token prompt came back reporting 2050 prompt tokens, and a marker
placed at the start or the middle of the text was gone while the same marker at
the end survived.

That is the worst failure this system can have. Every prompt in `prompts.py`
puts the instructions, the AJi standard and the required JSON shape *first* and
the clause text after, so an overflowing prompt loses precisely the part that
makes the answer trustworthy -- and what comes back still looks like a normal,
confident finding.

So this provider never lets the window be the thing that decides:

* it sizes `num_ctx` from the prompt before sending (`_context_for`), and
  refuses to send at all when the prompt cannot fit the configured ceiling;
* it checks the token count the service reports afterwards and raises rather
  than return an answer that was produced from a truncated prompt
  (`_check_context`).

`num_ctx` must be sent as a **top-level** field. The documented Ollama-style
`options: {"num_ctx": ...}` nesting is accepted by the endpoint and silently
ignored, which is the more dangerous of the two spellings because it looks
configured and is not.

── What this endpoint cannot do ─────────────────────────────────────────────

It cannot see images. `_build_messages` on the service rebuilds every message
as `{role, content}`, so an `images` field is dropped before the model is
reached; the model then answers from the text alone and invents a description
of a picture it never received. `verify_text_region` therefore refuses instead
of guessing -- see the method for the reasoning.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx
from django.conf import settings
from django.utils.translation import gettext as _

from .. import prompts
from ..schemas import (
    AdvisorAnswer,
    ClauseClassification,
    ClauseClassificationBatch,
    ClauseTranslation,
    StandardExtraction,
    ProseTranslation,
    SemanticAssessment,
    TextRegionVerification,
    VerificationResult,
)
from .base import (
    AICallResult,
    AIPermanentError,
    AIProvider,
    AIProviderNotConfigured,
    AIProviderUnavailable,
    AIRateLimited,
    AIResponseInvalid,
    ImageRegion,
)

#: Windows the provider is willing to ask for, smallest first. Ollama allocates
#: a KV cache per window and re-allocates when the size changes, so a handful of
#: buckets keeps consecutive calls on one allocation instead of making every
#: slightly different prompt pay for a resize.
_CTX_BUCKETS = (4096, 8192, 16384, 32768, 65536, 131072)

#: Characters per token, by script. Arabic packs into far fewer characters per
#: token than Latin text does. Both are deliberate under-estimates of the
#: divisor -- i.e. they over-estimate the token count -- because reserving a
#: window slightly too large costs memory, while one too small costs silence.
_CHARS_PER_TOKEN_AR = 2.0
_CHARS_PER_TOKEN_OTHER = 3.4

#: The fewest tokens a prompt of a given length could plausibly have come to,
#: by script. These divisors are far above any real characters-per-token ratio
#: -- ordinary Arabic runs near 2.0 and Latin near 4.0 -- so a count below the
#: floor they give is not a tokeniser being efficient, it is text that never
#: reached the model. Used only to detect truncation, never to size a window.
_FLOOR_CHARS_PER_TOKEN_AR = 4.0
_FLOOR_CHARS_PER_TOKEN_OTHER = 8.0


def _arabic_share(text: str) -> float:
    """Fraction of `text` in the Arabic block, used only to size the window."""
    if not text:
        return 0.0
    arabic = sum(1 for ch in text if "؀" <= ch <= "ۿ")
    return arabic / len(text)


class AjiBrainProvider(AIProvider):
    name = "ajibrain"

    @property
    def supports_vision(self) -> bool:
        """True once a vision model is configured. AJi Brain's
        /api/general-chat/ forwards `images` to the model since 2026-09-27;
        before that it dropped them, and this was False on that wire format
        whatever was configured."""
        return bool(self._vision_model)

    def __init__(self):
        self._base_url = (settings.AJI_BRAIN_API_BASE_URL or "").rstrip("/")
        self._api_key = settings.AJI_BRAIN_API_KEY
        self._text_model = settings.AJI_BRAIN_TEXT_MODEL
        # The vision setting is the switch: empty means "this deployment
        # cannot read images", and nothing here guesses that the text model
        # can. Set it to the model that /api/general-chat/info/ reports as
        # `vision: true` (today the same qwen3.8:27b).
        self._vision_model = settings.AJI_BRAIN_VISION_MODEL or ""
        self._timeout = settings.AJI_BRAIN_TIMEOUT_SECONDS
        self.wire_format = settings.AJI_BRAIN_WIRE_FORMAT
        self._max_ctx = settings.AJI_BRAIN_MAX_NUM_CTX
        self._ctx_reserve = settings.AJI_BRAIN_CTX_RESERVE
        self._chat_path = settings.AJI_BRAIN_CHAT_PATH or (
            "/api/general-chat/"
            if self.wire_format == "aji_brain"
            else "/v1/chat/completions"
        )

    # ── lifecycle ────────────────────────────────────────────────────────
    def is_configured(self) -> bool:
        return bool(self._base_url and self._text_model)

    def health_check(self) -> tuple[bool, str]:
        missing = [
            name
            for name, value in (
                ("AJI_BRAIN_API_BASE_URL", self._base_url),
                ("AJI_BRAIN_TEXT_MODEL", self._text_model),
            )
            if not value
        ]
        if missing:
            return False, _(
                "AJi Brain is not configured. Missing: %(missing)s."
            ) % {"missing": ", ".join(missing)}

        probe = "/api/health/" if self.wire_format == "aji_brain" else "/"
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(self._base_url + probe, headers=self._headers())
        except httpx.HTTPError as exc:
            return False, _("AJi Brain endpoint unreachable: %(error)s") % {"error": exc}

        if response.status_code == 401:
            return False, _("AJi Brain rejected the API key (401).")
        if response.status_code >= 400:
            return False, _("AJi Brain health probe returned %(code)s.") % {
                "code": response.status_code
            }

        served = ""
        try:
            served = (response.json() or {}).get("model", "") or ""
        except ValueError:
            pass
        # Worth surfacing rather than asserting: the service answers with
        # whichever model is active, which need not be the one configured here.
        if served and served != self._text_model:
            return True, _(
                "AJi Brain reachable, but it is serving %(served)s while "
                "AJI_BRAIN_TEXT_MODEL is %(configured)s."
            ) % {"served": served, "configured": self._text_model}
        return True, _("AJi Brain reachable.")

    # ── the context guard ────────────────────────────────────────────────
    def _estimate_tokens(self, text: str) -> int:
        """A deliberately generous token count for `text`."""
        share = _arabic_share(text)
        per_token = (
            _CHARS_PER_TOKEN_AR * share + _CHARS_PER_TOKEN_OTHER * (1 - share)
        )
        return math.ceil(len(text) / per_token)

    @staticmethod
    def _image_tokens(image) -> int:
        """Context an attached picture takes up.

        Qwen-VL reads a picture in 28-pixel patches, one token each. Measured
        on the live service: a 724x1024 page cost 772 prompt tokens, a
        910x1287 one 1,156. The window used to be sized from the text alone,
        so a question with a scanned page asked for a window too small to
        hold it.
        """
        if image is None:
            return 0
        import io
        import math

        regions = image if isinstance(image, (list, tuple)) else [image]
        total = 0
        for region in regions:
            try:
                from PIL import Image

                with Image.open(io.BytesIO(region.image_bytes)) as img:
                    width, height = img.size
            except Exception:
                width, height = 1024, 1024          # unknown: assume a full page
            total += math.ceil(width / 28) * math.ceil(height / 28) + 16
        return total

    def _context_for(self, prompt: str, extra_tokens: int = 0) -> int:
        """Choose the window this prompt needs, or refuse to send it.

        Refusing is the point. The alternative -- sending anyway and letting
        the service keep whichever end fits -- produces an answer that reads
        exactly like a good one.
        """
        needed = self._estimate_tokens(prompt) + extra_tokens + self._ctx_reserve
        for bucket in _CTX_BUCKETS:
            if bucket >= needed and bucket <= self._max_ctx:
                return bucket
        if needed > self._max_ctx:
            raise AIPermanentError(
                f"Prompt needs about {needed} tokens of context but "
                f"AJI_BRAIN_MAX_NUM_CTX is {self._max_ctx}. Sending it would make the "
                f"service silently drop the start of the prompt -- which is where "
                f"the instructions and the AJi standard are -- so it was not sent. "
                f"Either shorten what is passed in, or raise AJI_BRAIN_MAX_NUM_CTX if "
                f"the server has the memory for it."
            )
        return min(self._max_ctx, _CTX_BUCKETS[-1])

    def _token_floor(self, text: str) -> int:
        """The fewest tokens `text` could honestly have tokenised to."""
        share = _arabic_share(text)
        per_token = (
            _FLOOR_CHARS_PER_TOKEN_AR * share
            + _FLOOR_CHARS_PER_TOKEN_OTHER * (1 - share)
        )
        return int(len(text) / per_token)

    def _check_context(self, prompt: str, num_ctx: int, reported: int | None) -> None:
        """Fail if the answer came from a prompt the service had cut down.

        Sizing the window should make truncation impossible, so this catches
        the case sizing cannot: the service, or the Ollama behind it, using a
        smaller window than the one asked for. The measured tell is a count far
        below what the text could possibly tokenise to -- 2050 reported for a
        prompt of some 107,000 characters.

        The count is read as the whole prompt's, which is what the live service
        returns: across prompts sharing a long identical prefix it still scaled
        with total length rather than dropping to the newly-evaluated tail. If
        a future version starts reporting only uncached tokens, this guard
        would begin refusing good answers -- noisy, but the failure falls on
        the safe side, and the message below says where to look.
        """
        if not reported:
            return

        # Filled the window: even if nothing was dropped this time, there was
        # no room for the answer and the next token would have gone over.
        if reported >= num_ctx:
            raise AIProviderUnavailable(
                f"AJi Brain evaluated {reported} prompt tokens against a {num_ctx}-token "
                f"window, so the prompt filled the context entirely and content was "
                f"lost. Raise AJI_BRAIN_MAX_NUM_CTX or send less text."
            )

        floor = self._token_floor(prompt)
        if reported < floor:
            raise AIProviderUnavailable(
                f"AJi Brain reported only {reported} prompt tokens for a prompt of "
                f"{len(prompt)} characters, which could not have tokenised below "
                f"about {floor}. The service truncated the prompt -- it drops the "
                f"start, where the instructions and the AJi standard are -- so the "
                f"answer was discarded rather than returned. The window asked for "
                f"was {num_ctx}; check that the server honours a top-level num_ctx."
            )

    # ── wire format ──────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            # AJi Brain checks X-API-Key first and falls back to a bearer
            # token; sending both costs nothing and covers either deployment.
            headers["X-API-Key"] = self._api_key
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_payload(
        self, prompt: str, *, model_name: str, image: ImageRegion | None, num_ctx: int
    ) -> dict[str, Any]:
        if self.wire_format == "aji_brain":
            payload = {
                "model": model_name,
                "prompt": prompt,
                # Determinism matters more than variety: the same clause must
                # produce the same finding, or a review cannot be reproduced.
                "temperature": 0,
                # Top level on purpose. Nested under "options" it is accepted
                # and ignored -- see the module docstring.
                "num_ctx": num_ctx,
                # Qwen3 emits a <think> block otherwise. The service strips it
                # when thinking is explicitly off, and the answer arrives sooner.
                "think": False,
                "stream": False,
                "timeout": self._timeout,
            }
            if image is not None:
                import base64

                # Ollama's shape, which the service passes through to the
                # model: base64 of the file bytes, no data: prefix. One
                # photo, or the pages of a scanned PDF.
                regions = image if isinstance(image, (list, tuple)) else [image]
                payload["images"] = [
                    base64.b64encode(r.image_bytes).decode("ascii") for r in regions]
            return payload

        if self.wire_format == "openai_compatible":
            if image is None:
                content: Any = prompt
            else:
                import base64

                regions = image if isinstance(image, (list, tuple)) else [image]
                content = [{"type": "text", "text": prompt}] + [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{r.mime_type};base64,"
                                   + base64.b64encode(r.image_bytes).decode("ascii")
                        },
                    }
                    for r in regions
                ]
            return {
                "model": model_name,
                "messages": [{"role": "user", "content": content}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }

        raise AIProviderNotConfigured(
            f"Unsupported AJI_BRAIN_WIRE_FORMAT '{self.wire_format}'. "
            f"Expected 'aji_brain' or 'openai_compatible'."
        )

    def _extract_text(self, data: dict[str, Any]) -> tuple[str, dict[str, int | None]]:
        if self.wire_format == "aji_brain":
            if "answer" not in data:
                raise AIProviderUnavailable(
                    f"Unexpected AJi Brain response shape: {list(data)[:8]}"
                )
            usage = data.get("usage") or {}
            return data["answer"], {
                "prompt": usage.get("prompt_tokens"),
                "completion": usage.get("completion_tokens"),
            }

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderUnavailable(
                f"Unexpected AJi Brain response shape: {list(data)[:8]}"
            ) from exc
        usage = data.get("usage") or {}
        return text, {
            "prompt": usage.get("prompt_tokens"),
            "completion": usage.get("completion_tokens"),
        }

    # ── transport ────────────────────────────────────────────────────────
    @staticmethod
    def _raise_for_status(response) -> None:
        """The error an HTTP status means, for the plain and the streamed call."""
        if response.status_code == 401:
            raise AIProviderNotConfigured(
                "AJi Brain rejected the API key. Set AJI_BRAIN_API_KEY to the shared key "
                "the service expects in X-API-Key."
            )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise AIRateLimited(
                "AJi Brain rate limited.",
                retry_after=float(retry_after) if retry_after else None,
            )
        if response.status_code >= 500:
            raise AIProviderUnavailable(
                f"AJi Brain server error {response.status_code}: {response.text[:300]}"
            )
        if response.status_code >= 400:
            raise AIProviderUnavailable(
                f"AJi Brain rejected the request ({response.status_code}): {response.text[:300]}"
            )

    def _generate_stream(self, prompt: str, *, model_name: str, image=None, cancel=None):
        """The Brain's own streaming mode (`"stream": true`): yields
        ("chunk", text) as the model writes, then ("end", (latency_ms,
        tokens, done_reason)). Same window sizing and checks as _generate.

        `cancel` (apps.ai.streaming.Cancel) aborts the request from another
        thread -- the reader pressed Stop while the model was still reading
        the prompt, before there was anything to send them."""
        if not self.is_configured():
            raise AIProviderNotConfigured(
                "AI_PROVIDER=ajibrain but AJI_BRAIN_API_BASE_URL / AJI_BRAIN_TEXT_MODEL "
                "are not set."
            )
        num_ctx = self._context_for(prompt, extra_tokens=self._image_tokens(image))
        payload = self._build_payload(prompt, model_name=model_name, image=image, num_ctx=num_ctx)
        payload["stream"] = True

        started = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout) as client:
                if cancel is not None:
                    cancel.on(client.close)
                with client.stream("POST", self._base_url + self._chat_path,
                                   json=payload, headers=self._headers()) as response:
                    if cancel is not None:
                        cancel.on(response.close)
                    if response.status_code >= 400:
                        response.read()
                        self._raise_for_status(response)
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            data = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if data.get("error"):
                            raise AIProviderUnavailable(
                                f"AJi Brain stream error: {str(data['error'])[:300]}")
                        if data.get("chunk"):
                            yield "chunk", data["chunk"]
                        if data.get("done"):
                            usage = data.get("usage") or {}
                            tokens = {"prompt": usage.get("prompt_tokens"),
                                      "completion": usage.get("completion_tokens")}
                            self._check_context(prompt, num_ctx, tokens["prompt"])
                            yield "end", (int((time.monotonic() - started) * 1000),
                                          tokens, data.get("done_reason"))
                            return
        except httpx.TimeoutException as exc:
            raise AIProviderUnavailable(f"AJi Brain timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise AIProviderUnavailable(f"AJi Brain request failed: {exc}") from exc
        raise AIProviderUnavailable("AJi Brain ended the stream before the answer was complete.")

    def _generate(
        self, prompt: str, *, model_name: str, image: ImageRegion | None = None
    ) -> tuple[str, int, dict[str, int | None]]:
        if not self.is_configured():
            raise AIProviderNotConfigured(
                "AI_PROVIDER=ajibrain but AJI_BRAIN_API_BASE_URL / AJI_BRAIN_TEXT_MODEL "
                "are not set."
            )

        num_ctx = self._context_for(prompt, extra_tokens=self._image_tokens(image))
        payload = self._build_payload(
            prompt, model_name=model_name, image=image, num_ctx=num_ctx
        )

        started = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    self._base_url + self._chat_path,
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            raise AIProviderUnavailable(f"AJi Brain timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise AIProviderUnavailable(f"AJi Brain request failed: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)

        self._raise_for_status(response)

        try:
            data = response.json()
        except ValueError as exc:
            raise AIProviderUnavailable(
                f"AJi Brain returned a non-JSON response: {response.text[:200]}"
            ) from exc

        text, tokens = self._extract_text(data)
        self._check_context(prompt, num_ctx, tokens["prompt"])

        # 'length' means the model was cut off mid-answer; the JSON would parse
        # as invalid anyway, but the cause is worth naming.
        if data.get("done_reason") == "length":
            raise AIProviderUnavailable(
                "AJi Brain stopped at the output limit, so the answer is incomplete."
            )
        return text, latency_ms, tokens

    def _call(self, prompt, schema, *, request_type, model_name, image=None) -> AICallResult:
        from apps.ai import cache

        key = cache.key_for(model_name, request_type, prompt) if (
            image is None and cache.enabled(request_type)) else None
        if key:
            stored = cache.lookup(key)
            if stored is not None:
                try:
                    return AICallResult(
                        parsed=self.parse_or_raise(schema, stored), provider=self.name,
                        model=model_name, request_type=request_type, latency_ms=0,
                        raw_text=stored, cached=True,
                    )
                except AIResponseInvalid:
                    cache.forget(key)                  # the schema moved on; ask again
        raw, latency_ms, tokens = self._generate(prompt, model_name=model_name, image=image)
        parsed = self.parse_or_raise(schema, raw)
        if key:
            cache.store(key, model=model_name, request_type=request_type, raw_text=raw)
        return AICallResult(
            parsed=parsed,
            provider=self.name,
            model=model_name,
            request_type=request_type,
            latency_ms=latency_ms,
            prompt_tokens=tokens["prompt"],
            completion_tokens=tokens["completion"],
            raw_text=raw,
        )

    # ── capabilities ─────────────────────────────────────────────────────
    def classify_clause(self, *, clause_text, categories, language="ar") -> AICallResult:
        return self._call(
            prompts.classify_clause(
                clause_text=clause_text, categories=categories, language=language
            ),
            ClauseClassification,
            request_type="classify_clause",
            model_name=self._text_model,
        )

    def classify_clauses(self, *, clauses, categories) -> AICallResult:
        return self._call(
            prompts.classify_clauses(clauses=clauses, categories=categories),
            ClauseClassificationBatch,
            request_type="classify_clauses",
            model_name=self._text_model,
        )

    def assess_clause(
        self, *, clause_text, standard_position, acceptable_range, red_flags,
        context_clauses, contract_context, language="ar",
    ) -> AICallResult:
        return self._call(
            prompts.assess_clause(
                clause_text=clause_text,
                standard_position=standard_position,
                acceptable_range=acceptable_range,
                red_flags=red_flags,
                context_clauses=context_clauses,
                contract_context=contract_context,
                language=language,
            ),
            SemanticAssessment,
            request_type="assess_clause",
            model_name=self._text_model,
        )

    def verify_finding(
        self, *, clause_text, standard_position, system_result,
        previous_assessment, language="ar",
    ) -> AICallResult:
        return self._call(
            prompts.verify_finding(
                clause_text=clause_text,
                standard_position=standard_position,
                system_result=system_result,
                previous_assessment=previous_assessment,
                language=language,
            ),
            VerificationResult,
            request_type="verify_finding",
            model_name=self._text_model,
        )

    def answer_question(
        self, *, question, document_excerpts, standards_excerpts, language="ar",
        history=None, recalled=None, image=None,
    ) -> AICallResult:
        if image is not None and not self.supports_vision:
            raise AIProviderNotConfigured(
                "A photo was attached but AJI_BRAIN_VISION_MODEL is not set; the "
                "model cannot read it."
            )
        return self._call(
            prompts.answer_question(
                question=question,
                document_excerpts=document_excerpts,
                standards_excerpts=standards_excerpts,
                language=language,
                history=history,
                recalled=recalled,
                attached_image=image is not None,
            ),
            AdvisorAnswer,
            request_type="answer_question",
            model_name=self._vision_model if image is not None else self._text_model,
            image=image,
        )

    def answer_question_stream(
        self, *, question, document_excerpts, standards_excerpts, language="ar",
        history=None, recalled=None, image=None, cancel=None,
    ):
        kwargs = dict(question=question, document_excerpts=document_excerpts,
                      standards_excerpts=standards_excerpts, language=language,
                      history=history, recalled=recalled, image=image)
        if self.wire_format != "aji_brain":
            # Only the Brain's own format streams; the other answers at once.
            yield from super().answer_question_stream(**kwargs)
            return
        if image is not None and not self.supports_vision:
            raise AIProviderNotConfigured(
                "A photo was attached but AJI_BRAIN_VISION_MODEL is not set; the "
                "model cannot read it."
            )
        prompt = prompts.answer_question(
            question=question, document_excerpts=document_excerpts,
            standards_excerpts=standards_excerpts, language=language,
            history=history, recalled=recalled, attached_image=image is not None,
        )
        model_name = self._vision_model if image is not None else self._text_model
        parts: list[str] = []
        latency_ms, tokens, done_reason = 0, {"prompt": None, "completion": None}, None
        for kind, value in self._generate_stream(prompt, model_name=model_name, image=image,
                                                 cancel=cancel):
            if kind == "chunk":
                parts.append(value)
                yield value
            else:
                latency_ms, tokens, done_reason = value
        if done_reason == "length":
            raise AIProviderUnavailable(
                "AJi Brain stopped at the output limit, so the answer is incomplete."
            )
        raw = "".join(parts)
        # Validated exactly as the plain call is: what was shown while it was
        # being written is replaced by this, or by the error it raises.
        yield AICallResult(
            parsed=self.parse_or_raise(AdvisorAnswer, raw), provider=self.name,
            model=model_name, request_type="answer_question", latency_ms=latency_ms,
            prompt_tokens=tokens["prompt"], completion_tokens=tokens["completion"],
            raw_text=raw,
        )

    def translate_prose(self, *, fields, target_language) -> AICallResult:
        return self._call(
            prompts.translate_prose(fields=fields, target_language=target_language),
            ProseTranslation,
            request_type="translate_prose",
            model_name=self._text_model,
        )

    def translate_clause(self, *, clause_text, target_language) -> AICallResult:
        return self._call(
            prompts.translate_clause(
                clause_text=clause_text, target_language=target_language
            ),
            ClauseTranslation,
            request_type="translate_clause",
            model_name=self._text_model,
        )

    def normalize_standard(self, *, text, categories, part=1, parts=1) -> AICallResult:
        return self._call(
            prompts.normalize_standard(text=text, categories=categories, part=part, parts=parts),
            StandardExtraction,
            request_type="normalize_standard",
            model_name=self._text_model,
        )

    def verify_text_region(self, *, region, language="ar", strict=False) -> AICallResult:
        """Read a cropped page region, for comparison against OCR.

        Refused when no vision model is configured: a text-only model shown
        OCR text simply agrees with it -- a verification that always passes,
        which is worse than none. `AJI_BRAIN_VISION_MODEL` is the switch, set
        to the model /api/general-chat/info/ reports as `vision: true`.
        """
        if not self.supports_vision:
            raise AIProviderNotConfigured(
                "AJI_BRAIN_VISION_MODEL is not set, so AJi Brain cannot see images; "
                "OCR region verification would confirm the OCR text without reading "
                "the page. Set it to a vision-capable model to enable this."
            )
        return self._call(
            prompts.verify_text_region(language=language, strict=strict),
            TextRegionVerification,
            request_type="verify_text_region",
            model_name=self._vision_model,
            image=region,
        )
