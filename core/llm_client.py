"""The only module that talks to a model (Architecture §4.2).

Every call: budget preflight -> provider call -> usage logged to budget.py -> JSON validated.
Providers are swappable; each must return (text, Usage).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from pydantic import TypeAdapter, ValidationError

from core.budget import BudgetTracker, Usage, compute_cost_inr, estimate_tokens
from core.config import Settings

log = logging.getLogger("llm_client")

RETRYABLE_CODES = {429, 500, 502, 503, 504}
PREPAY_EXHAUSTED_MESSAGE = "Gemini prepay credits exhausted — top up in AI Studio"

REPAIR_SUFFIX = (
    "\n\nYour previous reply was not valid JSON for the required schema. Error: {error}\n"
    "Reply again with ONLY valid JSON matching the schema. Do not repeat the post text."
)

_STRIP_SCHEMA_KEYS = {"additionalProperties", "additional_properties"}
_FIELD_AT_RE = re.compile(r"""at ['"]?([A-Za-z0-9_.\[\]]+)['"]?""")


def _strip_additional_props(obj: Any) -> Any:
    """Recursively remove additionalProperties / additional_properties from any nested dict/list."""
    if isinstance(obj, dict):
        return {
            k: _strip_additional_props(v)
            for k, v in obj.items()
            if k not in _STRIP_SCHEMA_KEYS
        }
    if isinstance(obj, list):
        return [_strip_additional_props(item) for item in obj]
    return obj


def clean_schema_for_gemini(schema: Any) -> Dict[str, Any]:
    """Convert a Pydantic model / type annotation / dict into a JSON schema dict
    with all `additionalProperties` and `additional_properties` keys stripped recursively.
    """
    if isinstance(schema, dict):
        raw = schema
    else:
        raw = TypeAdapter(schema).json_schema()
    return _strip_additional_props(raw)


def extract_error_info(exc: Exception) -> Tuple[Optional[int], Optional[str], str]:
    """Extract (http_code, error_field, error_message) from a provider exception."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None

    message = getattr(exc, "message", None)
    details = getattr(exc, "details", None)
    field: Optional[str] = None

    if isinstance(details, dict):
        err_obj = details.get("error", details)
        if isinstance(err_obj, dict):
            if not message and isinstance(err_obj.get("message"), str):
                message = err_obj["message"]
            for detail in err_obj.get("details") or []:
                if isinstance(detail, dict):
                    for violation in detail.get("fieldViolations") or []:
                        if isinstance(violation, dict) and violation.get("field"):
                            field = str(violation["field"])
                            if not message and violation.get("description"):
                                message = str(violation["description"])
                            break
                if field:
                    break

    msg_str = str(message).strip() if message else str(exc).strip()
    if not field:
        m = _FIELD_AT_RE.search(msg_str)
        if m:
            field = m.group(1)
    return code, field, msg_str


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        retryable: bool = False,
        error_field: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.error_field = error_field
        self.error_message = message


class LLMUnavailable(Exception):
    """Non-retryable provider error, or retries exhausted. Caller should checkpoint and stop."""

    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        error_field: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.error_field = error_field
        self.error_message = error_message or message


class LLMProvider(Protocol):
    name: str

    def generate(self, *, model: str, prompt: str, system: Optional[str], max_output_tokens: int,
                 temperature: float, thinking: Dict[str, Any], response_schema: Any) -> Tuple[Optional[str], Usage]:
        ...

    def list_models(self) -> List[str]:
        ...


def usage_from_metadata(md: Any) -> Usage:
    """Map Gemini usage_metadata -> Usage. prompt_token_count includes cached tokens, so subtract them."""

    def _i(name: str) -> int:
        return int(getattr(md, name, 0) or 0) if md is not None else 0

    cached = _i("cached_content_token_count")
    return Usage(
        input_tokens=max(_i("prompt_token_count") - cached, 0),
        output_tokens=_i("candidates_token_count"),
        thinking_tokens=_i("thoughts_token_count"),
        cached_tokens=cached,
    )


def finish_reason_of(resp: Any) -> Optional[str]:
    """'MAX_TOKENS', 'STOP', ... from the first candidate, or None if unavailable."""
    try:
        reason = resp.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None
    if reason is None:
        return None
    return str(getattr(reason, "name", reason)).split(".")[-1].upper()


def thinking_kwargs(settings: Settings, call_type: str = "tagging") -> Dict[str, Any]:
    """Exactly one thinking control, never both (sending both returns HTTP 400)."""
    llm = settings.llm
    if llm.thinking_param == "thinking_level":
        level = llm.thinking_level_clustering if call_type == "clustering" else llm.thinking_level_lowest
        return {"thinking_level": level}
    return {"thinking_budget": settings.budget.thinking_budget_tagging}


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str):
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=api_key)

    @staticmethod
    def _wrap(e: Exception) -> ProviderError:
        code, field, msg = extract_error_info(e)
        if code == 402:
            return ProviderError(PREPAY_EXHAUSTED_MESSAGE, code=402, retryable=False, error_field=field)
        return ProviderError(msg, code=code, retryable=code in RETRYABLE_CODES, error_field=field)

    def build_generate_config(
        self,
        *,
        system: Optional[str],
        max_output_tokens: int,
        temperature: float,
        thinking: Dict[str, Any],
        response_schema: Any,
    ) -> Any:
        t = self._types
        cleaned_schema = clean_schema_for_gemini(response_schema)
        return t.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_schema=cleaned_schema,
            thinking_config=t.ThinkingConfig(**thinking),
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        )

    def generate(self, *, model, prompt, system, max_output_tokens, temperature, thinking, response_schema):
        config = self.build_generate_config(
            system=system,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            thinking=thinking,
            response_schema=response_schema,
        )
        self.last_finish_reason = None
        try:
            resp = self._client.models.generate_content(model=model, contents=prompt, config=config)
        except Exception as e:  # SDK raises google.genai.errors.APIError subclasses
            raise self._wrap(e) from e
        self.last_finish_reason = finish_reason_of(resp)
        try:
            text = resp.text
        except Exception:  # some SDK versions raise when the candidate was cut off
            text = None
        return text, usage_from_metadata(getattr(resp, "usage_metadata", None))

    def list_models(self) -> List[str]:
        try:
            return [m.name for m in self._client.models.list()]
        except Exception as e:
            raise self._wrap(e) from e


def build_provider(name: str, api_key: Optional[str]) -> LLMProvider:
    if name == "gemini":
        if not api_key:
            raise LLMUnavailable("GEMINI_API_KEY missing. Copy .env.example to .env and add your key.")
        return GeminiProvider(api_key)
    raise ValueError(f"Unknown LLM provider: {name}")


@dataclass
class LLMResult:
    status: str                      # "ok" | "enrichment_failed" | "max_tokens"
    data: Any
    raw_text: Optional[str]
    usage: Usage
    attempts: int
    error: Optional[str] = None
    finish_reason: Optional[str] = None


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LLMClient:
    def __init__(self, settings: Settings, tracker: BudgetTracker, provider: Optional[LLMProvider] = None,
                 api_key: Optional[str] = None, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.tracker = tracker
        self.provider = provider or build_provider(settings.llm.provider, api_key)
        self.sleep = sleep

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _parse(text: Optional[str], schema: Any) -> Tuple[Any, Optional[str]]:
        if not text:
            return None, "empty response"
        cleaned = _FENCE.sub("", text.strip())
        try:
            return TypeAdapter(schema).validate_python(json.loads(cleaned)), None
        except (json.JSONDecodeError, ValidationError) as e:
            return None, str(e)

    def _check_thinking(self, usage: Usage, batch_id: str) -> None:
        log.info("call %s usage: in=%d out=%d thinking=%d cached=%d", batch_id, usage.input_tokens,
                 usage.output_tokens, usage.thinking_tokens, usage.cached_tokens)
        ratio = self.settings.budget.thinking_warn_ratio
        if usage.thinking_tokens and usage.thinking_tokens > ratio * usage.output_tokens:
            log.warning("call %s: thinking tokens (%d) exceed %.0f%% of output tokens (%d). Check thinking config.",
                        batch_id, usage.thinking_tokens, ratio * 100, usage.output_tokens)

    def worst_case_cost(self, prompt: str, system: Optional[str], max_output_tokens: int) -> float:
        # max_output_tokens caps visible + thinking output, both billed at the output price.
        in_tok = estimate_tokens(prompt) + estimate_tokens(system or "")
        return compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=max_output_tokens), self.settings.budget)

    # ------------------------------------------------------------------ main entry
    def generate_json(self, *, prompt: str, schema: Any, batch_id: str, max_output_tokens: int,
                      system: Optional[str] = None, call_type: str = "tagging",
                      expected_cost_inr: Optional[float] = None, parse_schema: Any = None) -> LLMResult:
        """`schema` is what Gemini must follow. `parse_schema` (default: same) is what the reply is
        validated against; Enrich passes a lenient wrapper so one bad item can't fail a whole batch.
        A reply cut off at max_output_tokens returns status "max_tokens" with no repair retry: the
        caller splits the batch instead (a repair would hit the same limit)."""
        llm = self.settings.llm
        parse_schema = parse_schema if parse_schema is not None else schema
        worst = self.worst_case_cost(prompt, system, max_output_tokens)
        cleaned_schema = clean_schema_for_gemini(schema)
        retry_count, attempts, repair_used = 0, 0, False
        total = Usage()
        current_prompt = prompt
        last_text, last_err = None, None

        while True:
            self.tracker.preflight(worst)  # raises BudgetError -> caller checkpoints and exits
            attempts += 1
            try:
                text, usage = self.provider.generate(
                    model=llm.model, prompt=current_prompt, system=system,
                    max_output_tokens=max_output_tokens, temperature=llm.temperature,
                    thinking=thinking_kwargs(self.settings, call_type), response_schema=cleaned_schema,
                )
            except ProviderError as e:
                self.tracker.record(Usage(), batch_id, retry_count, call_status=f"error_{e.code or 'unknown'}")
                if e.code == 402:
                    log.error("call %s: %s (HTTP 402)", batch_id, PREPAY_EXHAUSTED_MESSAGE)
                    raise LLMUnavailable(
                        f"{PREPAY_EXHAUSTED_MESSAGE} (call {batch_id}, HTTP 402)",
                        code=402,
                        error_field=e.error_field,
                        error_message=PREPAY_EXHAUSTED_MESSAGE,
                    ) from e
                if e.retryable and retry_count < llm.max_retries:
                    wait = llm.backoff_base_sec * (2 ** retry_count)
                    log.warning("call %s: provider error %s; retry %d/%d in %.0fs", batch_id, e.code,
                                retry_count + 1, llm.max_retries, wait)
                    self.sleep(wait)
                    retry_count += 1
                    continue
                field_note = f" [field: {e.error_field}]" if e.error_field else ""
                raise LLMUnavailable(
                    f"call {batch_id} failed (HTTP {e.code or 'unknown'}{field_note}): {e.error_message}",
                    code=e.code,
                    error_field=e.error_field,
                    error_message=e.error_message,
                ) from e

            total = total + usage
            self._check_thinking(usage, batch_id)
            finish = getattr(self.provider, "last_finish_reason", None)
            if finish == "MAX_TOKENS":
                self.tracker.record(usage, batch_id, retry_count, call_status="max_tokens",
                                    expected_cost_inr=expected_cost_inr)
                log.warning("call %s hit max_output_tokens (%d); caller should split the batch", batch_id,
                            max_output_tokens)
                return LLMResult("max_tokens", None, text, total, attempts, "MAX_TOKENS", finish)
            data, err = self._parse(text, parse_schema)
            self.tracker.record(usage, batch_id, retry_count, call_status="ok" if data is not None else "invalid_json",
                                expected_cost_inr=expected_cost_inr)
            last_text, last_err = text, err
            if data is not None:
                return LLMResult("ok", data, text, total, attempts, finish_reason=finish)
            if repair_used:
                return LLMResult("enrichment_failed", None, last_text, total, attempts, last_err, finish)
            repair_used = True
            retry_count += 1
            current_prompt = prompt + REPAIR_SUFFIX.format(error=(err or "")[:500])

    def list_models(self) -> List[str]:
        names = self.provider.list_models()
        self.tracker.record(Usage(), "models_list", 0, call_status="ok")
        return names
