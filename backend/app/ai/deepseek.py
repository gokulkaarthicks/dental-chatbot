"""DeepSeek chat-completions client (OpenAI-compatible wire format).

Only the parts we need are modelled. The client is deliberately small:

* strict connect / read / total timeouts,
* retries with exponential backoff for transient failures (429, 5xx, network),
* typed exceptions so the caller can produce a recoverable, user-facing message,
* **no logging of the API key** and no logging of prompt contents.

The key is read from settings at call time and sent only in the Authorization
header of the outbound request.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.observability import deepseek_status

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 45.0
TOTAL_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2
RETRY_BASE_DELAY_SECONDS = 0.6


class DeepSeekError(Exception):
    """Base class for provider failures that the API layer can translate."""

    user_message = "I'm having trouble reaching the scheduling system right now."


class DeepSeekNotConfigured(DeepSeekError):
    user_message = (
        "The online assistant isn't configured yet. I can still take your details "
        "and have the front desk call you back."
    )


class DeepSeekTimeout(DeepSeekError):
    user_message = "That took a little longer than expected. Could you try sending that again?"


class DeepSeekUnavailable(DeepSeekError):
    user_message = "I'm having trouble connecting right now. Please try again in a moment."


class DeepSeekRateLimited(DeepSeekError):
    user_message = "I'm getting a lot of requests right now. Could you send that again in a few seconds?"


class DeepSeekBadResponse(DeepSeekError):
    user_message = "Something went wrong on my end. Could you try that again?"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class AssistantTurn:
    """A normalized assistant message, ready to persist or replay."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def _tool_call_from_payload(payload: dict[str, Any]) -> ToolCall | None:
    function = payload.get("function") or {}
    call_id = payload.get("id")
    name = function.get("name")
    if not call_id or not name:
        return None
    arguments = function.get("arguments")
    if arguments is None:
        arguments = "{}"
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return ToolCall(id=str(call_id), name=str(name), arguments=arguments)


# Some DeepSeek-compatible deployments return the model's native DSML tool
# syntax in ``message.content`` instead of populating ``message.tool_calls``.
# Keep this adapter deliberately narrow: only an invoke block containing
# parameter elements is accepted, and every parameter is decoded before it
# reaches the normal Pydantic tool boundary.
_DSML_INVOKE_START = re.compile(
    r"<[^>]*DSML[^>]*\binvoke\s+name\s*=\s*['\"]([^'\"]+)['\"][^>]*>",
    re.IGNORECASE,
)
_DSML_INVOKE_END = re.compile(
    r"\\?\s*</?[^>]*DSML[^>]*\binvoke\s*>",
    re.IGNORECASE,
)
_DSML_PARAMETER_START = re.compile(
    r"<[^>]*DSML[^>]*\bparameter\s+name\s*=\s*['\"]([^'\"]+)['\"]"
    r"(?:\s+string\s*=\s*['\"]([^'\"]+)['\"])?[^>]*>",
    re.IGNORECASE,
)
_DSML_PARAMETER_END = re.compile(
    r"\\?\s*</[^>]*DSML[^>]*\bparameter\s*>",
    re.IGNORECASE,
)
_DSML_CALLS_WRAPPER = re.compile(
    r"\\?\s*</?[^>]*DSML[^>]*\bcalls\s*>",
    re.IGNORECASE,
)


def _decode_dsml_value(raw: str, *, is_string: str | None) -> Any:
    value = raw.strip().rstrip("\\").strip()
    if (is_string or "").lower() == "true":
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        # A malformed typed value must still reach Pydantic as text, where it
        # gets the same invalid-argument response as an ordinary tool call.
        return value


def _tool_calls_from_dsml(content: str) -> tuple[str, list[ToolCall]]:
    """Extract DeepSeek DSML invokes and return content with them removed."""
    if "DSML" not in content or "invoke" not in content:
        return content, []

    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    for index, match in enumerate(_DSML_INVOKE_START.finditer(content), start=1):
        end = _DSML_INVOKE_END.search(content, match.end())
        if end is None:
            continue
        arguments: dict[str, Any] = {}
        for parameter in _DSML_PARAMETER_START.finditer(content, match.end(), end.start()):
            parameter_end = _DSML_PARAMETER_END.search(content, parameter.end(), end.start())
            if parameter_end is None:
                continue
            arguments[parameter.group(1)] = _decode_dsml_value(
                content[parameter.end() : parameter_end.start()],
                is_string=parameter.group(2),
            )
        calls.append(
            ToolCall(
                id=f"dsml_{index}",
                name=match.group(1),
                arguments=json.dumps(arguments),
            )
        )
        spans.append((match.start(), end.end()))

    if not spans:
        return content, []
    cleaned = content
    for start, end in reversed(spans):
        cleaned = f"{cleaned[:start]}{cleaned[end:]}"
    return _DSML_CALLS_WRAPPER.sub("", cleaned).strip(), calls


class DeepSeekClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return self._settings.deepseek_configured

    def _endpoint(self) -> str:
        base = self._settings.deepseek_base_url.rstrip("/")
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.deepseek_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            TOTAL_TIMEOUT_SECONDS,
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
            write=READ_TIMEOUT_SECONDS,
        )

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        temperature: float = 0.3,
        max_tokens: int = 700,
    ) -> AssistantTurn:
        if not self.configured:
            raise DeepSeekNotConfigured("DEEPSEEK_API_KEY is not set")

        payload: dict[str, Any] = {
            "model": self._settings.deepseek_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        tools_enabled = bool(tools)
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self._post(payload, tools_enabled=tools_enabled)
            except (DeepSeekTimeout, DeepSeekUnavailable, DeepSeekRateLimited) as exc:
                last_error = exc
                if attempt >= MAX_RETRIES:
                    break
                delay = RETRY_BASE_DELAY_SECONDS * (2**attempt) + random.uniform(0, 0.25)
                logger.warning(
                    "DeepSeek call failed (attempt %s/%s): %s; retrying in %.2fs",
                    attempt + 1,
                    MAX_RETRIES + 1,
                    type(exc).__name__,
                    delay,
                )
                time.sleep(delay)
            except DeepSeekError:
                raise

        raise last_error or DeepSeekUnavailable("DeepSeek request failed")

    def _post(self, payload: dict[str, Any], *, tools_enabled: bool = True) -> AssistantTurn:
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout()) as client:
                response = client.post(self._endpoint(), headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise DeepSeekTimeout("timed out") from exc
        except httpx.HTTPError as exc:
            raise DeepSeekUnavailable("network error") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)

        if response.status_code == 429 or response.status_code >= 500:
            logger.warning(
                "DeepSeek transient failure: status=%s latency_ms=%s",
                response.status_code,
                elapsed_ms,
            )
            if response.status_code >= 500:
                raise DeepSeekUnavailable(f"status {response.status_code}")
            raise DeepSeekRateLimited(f"status {response.status_code}")

        if response.status_code >= 400:
            # 400/401/403/422 are not retryable. Log status and a coarse error
            # type only: bodies can echo prompt content and must not enter logs.
            error_type = None
            try:
                error = (response.json() or {}).get("error") or {}
                if isinstance(error, dict):
                    error_type = error.get("type") or error.get("code")
            except (ValueError, AttributeError):
                error_type = None
            logger.error(
                "DeepSeek rejected request: status=%s latency_ms=%s error_type=%s",
                response.status_code,
                elapsed_ms,
                error_type,
            )
            raise DeepSeekBadResponse(f"status {response.status_code}")

        try:
            body = response.json()
            choice = (body.get("choices") or [])[0]
            message = choice.get("message") or {}
        except (ValueError, IndexError, AttributeError) as exc:
            raise DeepSeekBadResponse("unparseable response") from exc

        content = (message.get("content") or "").strip()
        tool_calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            parsed = _tool_call_from_payload(raw_call)
            if parsed is not None:
                tool_calls.append(parsed)
        content, dsml_calls = _tool_calls_from_dsml(content)
        # DSML is only a fallback when tools were actually offered. Promoting
        # markup into synthetic tool_call ids after tools were withdrawn corrupts
        # the next provider round (DeepSeek answers 400).
        if not tool_calls and tools_enabled:
            tool_calls = dsml_calls
        elif dsml_calls and not tools_enabled:
            logger.info(
                "Ignoring %s DSML tool invoke(s); tools were not offered this round",
                len(dsml_calls),
            )

        deepseek_status(
            logger,
            latency_ms=elapsed_ms,
            status=response.status_code,
            tool_calls=len(tool_calls),
            finish_reason=choice.get("finish_reason"),
        )

        return AssistantTurn(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
        )
