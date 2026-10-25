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

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self._post(payload)
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

    def _post(self, payload: dict[str, Any]) -> AssistantTurn:
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
            # 400/401/403/422 are not retryable. Log the status only: bodies can
            # echo prompt content and must not enter logs.
            logger.error(
                "DeepSeek rejected request: status=%s latency_ms=%s",
                response.status_code,
                elapsed_ms,
            )
            raise DeepSeekBadResponse(f"status {response.status_code}")

        try:
            body = response.json()
            choice = (body.get("choices") or [])[0]
            message = choice.get("message") or {}
        except (ValueError, IndexError, AttributeError) as exc:
            raise DeepSeekBadResponse("unparseable response") from exc

        tool_calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            parsed = _tool_call_from_payload(raw_call)
            if parsed is not None:
                tool_calls.append(parsed)

        deepseek_status(
            logger,
            latency_ms=elapsed_ms,
            status=response.status_code,
            tool_calls=len(tool_calls),
            finish_reason=choice.get("finish_reason"),
        )

        return AssistantTurn(
            content=(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
        )
