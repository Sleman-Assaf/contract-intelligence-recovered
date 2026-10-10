"""The single entry point business code uses for AI (§43, §44, §45, §46).

    from apps.ai.services import ai_service
    result = ai_service.assess_clause(...)

Adds retry, schema-repair, telemetry and failure classification on top of
whichever provider is configured. No caller ever touches a vendor SDK.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from django.conf import settings

from . import prompts
from .models import AIRequestLog
from .providers.base import (
    AICallResult,
    AIPermanentError,
    AIProviderError,
    AIProviderNotConfigured,
    AIProviderUnavailable,
    AIRateLimited,
    AIResponseInvalid,
    ImageRegion,
)
from .registry import get_ai_provider

logger = logging.getLogger(__name__)

_STATUS_FOR_ERROR = {
    AIResponseInvalid: AIRequestLog.Status.INVALID_RESPONSE,
    AIRateLimited: AIRequestLog.Status.RATE_LIMITED,
    AIProviderNotConfigured: AIRequestLog.Status.NOT_CONFIGURED,
    AIPermanentError: AIRequestLog.Status.ERROR,
    AIProviderUnavailable: AIRequestLog.Status.UNAVAILABLE,
}


def _status_for(exc: Exception) -> str:
    for kind, status in _STATUS_FOR_ERROR.items():
        if isinstance(exc, kind):
            return status
    return AIRequestLog.Status.ERROR


class _RateLimiter:
    """Spaces outgoing calls so a bulk analysis does not exhaust the quota.

    Deliberately simple: one process, one lock, a minimum interval between
    calls. It is not a distributed limiter and does not pretend to be - with
    several worker processes the effective rate is multiplied by their number.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> float:
        """Block until the next call is due. Returns the seconds waited."""
        # Read the setting per call so it can be changed without a restart and
        # overridden in tests.
        rpm = getattr(settings, "AI_MAX_REQUESTS_PER_MINUTE", 0)
        if not rpm or rpm <= 0:
            return 0.0
        interval = 60.0 / rpm
        with self._lock:
            now = time.monotonic()
            due = self._last_call + interval
            waited = 0.0
            if self._last_call and due > now:
                waited = due - now
                time.sleep(waited)
                now = time.monotonic()
            self._last_call = now
        return waited


class AIService:
    """Provider-agnostic facade."""

    def __init__(self) -> None:
        self._limiter = _RateLimiter()

    # ── plumbing ─────────────────────────────────────────────────────────
    def _run(
        self,
        call: Callable[[], AICallResult],
        *,
        request_type: str,
        contract_id: str | int = "",
        analysis_run=None,
        retry_budget: float | None = None,
    ) -> AICallResult:
        provider = get_ai_provider()
        max_attempts = max(1, settings.AI_MAX_RETRIES)
        last_exc: Exception | None = None
        attempts = 0
        #: Seconds already spent waiting between attempts, against `retry_budget`.
        waited = 0.0

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            self._limiter.wait()
            try:
                result = call()
            except AIProviderNotConfigured as exc:
                # Not retryable — an operator must fix configuration.
                self._log(
                    provider.name, "", request_type, _status_for(exc),
                    attempts, None, exc, contract_id, analysis_run,
                )
                raise
            except (AIRateLimited, AIProviderUnavailable, AIResponseInvalid) as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    break
                delay = getattr(exc, "retry_after", None) or min(2 ** attempt, 30)
                if retry_budget is not None and waited + delay > retry_budget:
                    # A daily quota does not clear in thirty seconds. Waiting
                    # it out here only means the reader stares at a spinner
                    # for a minute and a half before seeing the same error.
                    logger.warning(
                        "AI %s giving up after %s attempt(s): %s would need %ss, "
                        "past the %ss interactive budget",
                        request_type, attempt, type(exc).__name__, delay, retry_budget,
                    )
                    break
                waited += delay
                logger.warning(
                    "AI %s attempt %s/%s failed (%s); retrying in %ss",
                    request_type, attempt, max_attempts, type(exc).__name__, delay,
                )
                time.sleep(delay)
            else:
                result.attempts = attempt
                self._log(
                    result.provider, result.model, request_type,
                    AIRequestLog.Status.SUCCESS, attempt, result, None,
                    contract_id, analysis_run,
                )
                return result

        assert last_exc is not None
        self._log(
            provider.name, "", request_type, _status_for(last_exc),
            attempts, None, last_exc, contract_id, analysis_run,
        )
        raise last_exc

    def _log(
        self, provider_name, model, request_type, status, attempts,
        result: AICallResult | None, exc: Exception | None,
        contract_id, analysis_run,
    ) -> None:
        """Telemetry only. Never stores prompts, responses or secrets (§45)."""
        try:
            AIRequestLog.objects.create(
                provider=provider_name,
                model=model or (result.model if result else ""),
                request_type=request_type,
                analysis_run=analysis_run,
                contract_id=str(contract_id) if contract_id else "",
                prompt_template_version=prompts.PROMPT_VERSION,
                status=status,
                attempts=attempts,
                latency_ms=result.latency_ms if result else None,
                prompt_tokens=result.prompt_tokens if result else None,
                completion_tokens=result.completion_tokens if result else None,
                error_message=str(exc)[:2000] if exc else "",
                cached=bool(result and result.cached),
            )
        except Exception:  # pragma: no cover - telemetry must never break work
            logger.exception("Failed to write AIRequestLog for %s", request_type)

    # ── capabilities ─────────────────────────────────────────────────────
    def classify_clause(
        self, *, clause_text: str, categories: list[str], language: str = "ar",
        contract_id="", analysis_run=None,
    ) -> AICallResult:
        provider = get_ai_provider()
        return self._run(
            lambda: provider.classify_clause(
                clause_text=clause_text, categories=categories, language=language
            ),
            request_type="classify_clause",
            contract_id=contract_id,
            analysis_run=analysis_run,
        )

    def classify_clauses(
        self, *, clauses: list[tuple[int, str, str]], categories: list[str],
        contract_id="", analysis_run=None,
    ) -> dict[int, Any]:
        """Classify several (id, number, text) clauses in one call.

        Returns {id: classification} for the answers that can be trusted: an
        id that was asked about, answered once, with a category from the list.
        Anything else is left out, and the caller asks about that clause on
        its own -- a batch can save time, never cost a clause its reading.
        """
        from apps.ai.providers.base import AIProvider

        provider = get_ai_provider()
        if type(provider).classify_clauses is AIProvider.classify_clauses:
            return {}
        try:
            call = self._run(
                lambda: provider.classify_clauses(clauses=clauses, categories=categories),
                request_type="classify_clauses",
                contract_id=contract_id,
                analysis_run=analysis_run,
            )
        except AIProviderError:
            return {}
        asked = {cid for cid, _number, _text in clauses}
        allowed = set(categories) | {prompts.NO_CATEGORY}
        seen: dict[int, int] = {}
        for item in call.parsed.results:
            seen[item.id] = seen.get(item.id, 0) + 1
        return {
            item.id: item for item in call.parsed.results
            if item.id in asked and seen[item.id] == 1 and item.primary in allowed
        }

    def assess_clause(
        self, *, clause_text: str, standard_position: str, acceptable_range: str = "",
        red_flags: str = "", context_clauses: list[str] | None = None,
        contract_context: dict[str, Any] | None = None, language: str = "ar",
        contract_id="", analysis_run=None,
    ) -> AICallResult:
        provider = get_ai_provider()
        return self._run(
            lambda: provider.assess_clause(
                clause_text=clause_text,
                standard_position=standard_position,
                acceptable_range=acceptable_range,
                red_flags=red_flags,
                context_clauses=context_clauses or [],
                contract_context=contract_context or {},
                language=language,
            ),
            request_type="assess_clause",
            contract_id=contract_id,
            analysis_run=analysis_run,
        )

    def translate_finding(self, *, finding, contract_id="", analysis_run=None):
        """Fill in the languages a finding is missing, and save it.

        Only interpretation is translated -- see prompts.TRANSLATABLE_PROSE.
        `original_wording` and `fallback_wording` are never sent or touched:
        they are the contract's own text and the wording Legal approved.

        Returns the language codes that were added.
        """
        from apps.ai.prompts import OUTPUT_LANGUAGES, TRANSLATABLE_PROSE
        from apps.documents.language import mostly

        action = finding.recommended_action or ""
        # Approved wording is Legal's text: never translated, whatever else is.
        approved = bool(action.strip()) and " ".join(action.split()) == " ".join(
            (finding.fallback_wording or "").split())
        source = {
            field: (finding.prose_in(field) or getattr(finding, field, ""))
            for field in TRANSLATABLE_PROSE
            if not (field == "recommended_action" and approved)
        }
        if not any(source.values()):
            return []

        provider = get_ai_provider()
        added = []
        prose = dict(finding.prose or {})

        def translate(fields, code):
            return self._run(
                lambda: provider.translate_prose(fields=fields, target_language=code),
                request_type="translate_prose",
                contract_id=contract_id or str(finding.contract_id or ""),
                analysis_run=analysis_run,
            ).parsed

        for code in OUTPUT_LANGUAGES:
            block = prose.get(code) or {}
            if block.get("title"):
                # Translated before recommended actions were: only the action
                # may still be in the other language.
                current = block.get("recommended_action") or ""
                if not approved and current.strip() and mostly(current) not in ("", code):
                    translated = translate({"recommended_action": current}, code).recommended_action
                    if translated.strip():
                        prose[code] = {**block, "recommended_action": translated}
                        added.append(f"action:{code}")
                continue
            parsed = translate(source, code)
            prose[code] = {
                "title": (parsed.title or "")[:255],
                "deviation": parsed.deviation,
                "explanation": parsed.explanation,
                "business_impact": parsed.business_impact,
                # Approved wording stays exactly as Legal wrote it.
                "recommended_action": action if approved else (parsed.recommended_action or action),
            }
            added.append(code)

        # The clause itself, as a reading aid beside the original. Only for a
        # language the clause is not already written in -- translating English
        # into English just prints the paragraph twice.
        translations = dict(finding.wording_translations or {})
        clause = (finding.original_wording or "").strip()
        if clause:
            from apps.documents.language import detect_language

            own = detect_language(clause)
            for code in OUTPUT_LANGUAGES:
                if translations.get(code):
                    continue
                if code == own:
                    # Already written in it: record the clause as its own
                    # reading, which `wording_translation()` hides, and spend
                    # no call on it.
                    translations[code] = clause
                    added.append(f"clause:{code}")
                    continue
                call = self._run(
                    lambda code=code: provider.translate_clause(
                        clause_text=clause[:8000], target_language=code
                    ),
                    request_type="translate_clause",
                    contract_id=contract_id or str(finding.contract_id or ""),
                    analysis_run=analysis_run,
                )
                text = (call.parsed.text or "").strip()
                # Store it even when it comes back identical: that is a fact
                # about the clause, and `wording_translation()` hides it.
                translations[code] = text
                added.append(f"clause:{code}")

        if added:
            finding.prose = prose
            finding.wording_translations = translations
            finding.save(update_fields=["prose", "wording_translations"])
        return added

    def verify_finding(
        self, *, clause_text: str, standard_position: str, system_result: str,
        previous_assessment: dict[str, Any], language: str = "ar",
        contract_id="", analysis_run=None,
    ) -> AICallResult:
        provider = get_ai_provider()
        return self._run(
            lambda: provider.verify_finding(
                clause_text=clause_text,
                standard_position=standard_position,
                system_result=system_result,
                previous_assessment=previous_assessment,
                language=language,
            ),
            request_type="verify_finding",
            contract_id=contract_id,
            analysis_run=analysis_run,
        )

    def answer_question(
        self, *, question: str, document_excerpts: list[str],
        standards_excerpts: list[str] | None = None, language: str = "ar",
        contract_id="", history: list[dict] | None = None,
        recalled: list[dict] | None = None, image=None,
    ) -> AICallResult:
        provider = get_ai_provider()
        return self._run(
            lambda: provider.answer_question(
                question=question,
                document_excerpts=document_excerpts,
                standards_excerpts=standards_excerpts or [],
                language=language,
                history=history,
                recalled=recalled,
                image=image,
            ),
            request_type="answer_question",
            contract_id=contract_id,
            # Somebody is watching a spinner for this one.
            retry_budget=settings.AI_INTERACTIVE_RETRY_BUDGET,
        )

    def answer_question_stream(
        self, *, question: str, document_excerpts: list[str],
        standards_excerpts: list[str] | None = None, language: str = "ar",
        contract_id="", history: list[dict] | None = None,
        recalled: list[dict] | None = None, image=None, cancel=None,
    ):
        """answer_question, as it is written: yields the model's raw output in
        pieces, then the AICallResult. `cancel` stops it from another thread.

        Not retried once words have been shown -- starting again would repeat
        them. A call that fails before its first piece falls back to the
        ordinary call, which does retry; the reader then waits as before.
        """
        provider = get_ai_provider()
        kwargs = dict(
            question=question, document_excerpts=document_excerpts,
            standards_excerpts=standards_excerpts or [], language=language,
            history=history, recalled=recalled, image=image,
        )
        self._limiter.wait()
        shown = finished = False
        stream = (provider.answer_question_stream(**kwargs, cancel=cancel) if cancel is not None
                  else provider.answer_question_stream(**kwargs))
        try:
            for item in stream:
                if isinstance(item, AICallResult):
                    self._log(provider.name, item.model, "answer_question",
                              AIRequestLog.Status.SUCCESS, 1, item, None, contract_id, None)
                    finished = True
                    yield item
                    return
                shown = True
                yield item
        except GeneratorExit:
            # Closed by the caller. Closing the provider's stream closes its
            # HTTP request, so the model stops writing. Only an unfinished
            # answer was stopped by the reader; a finished one is just done.
            stream.close()
            if not finished:
                self._log(provider.name, "", "answer_question", AIRequestLog.Status.ERROR, 1,
                          None, RuntimeError("stopped by the reader"), contract_id, None)
            raise
        except AIProviderError as exc:
            self._log(provider.name, "", "answer_question", _status_for(exc), 1,
                      None, exc, contract_id, None)
            if shown or (cancel is not None and cancel.is_set) or isinstance(
                    exc, (AIProviderNotConfigured, AIPermanentError)):
                raise
            logger.info("Advisor stream failed before its first word (%s); asking again", exc)
        result = self.answer_question(contract_id=contract_id, **kwargs)
        if result.raw_text:
            yield result.raw_text
        yield result

    def normalize_standard(self, *, text: str, categories: list[dict],
                           part: int = 1, parts: int = 1) -> AICallResult:
        """One part of an uploaded standards document, restated uniformly."""
        provider = get_ai_provider()
        return self._run(
            lambda: provider.normalize_standard(
                text=text, categories=categories, part=part, parts=parts),
            request_type="normalize_standard",
        )

    def verify_text_region(
        self, *, region: ImageRegion, language: str = "ar", strict: bool = False,
        contract_id="", analysis_run=None,
    ) -> AICallResult:
        """An independent reading of one cropped region. The OCR text is not
        an argument: the model must not see it (see prompts.verify_text_region)."""
        provider = get_ai_provider()
        return self._run(
            lambda: provider.verify_text_region(
                region=region, language=language, strict=strict
            ),
            request_type="verify_text_region",
            contract_id=contract_id,
            analysis_run=analysis_run,
        )

    # ── diagnostics ──────────────────────────────────────────────────────
    def health(self) -> tuple[bool, str]:
        return get_ai_provider().health_check()


ai_service = AIService()

__all__ = ["ai_service", "AIService", "AIProviderError", "ImageRegion"]
