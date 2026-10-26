"""DeepSeek as a LangChain chat model.

DeepSeek exposes an OpenAI-compatible endpoint, so ``ChatOpenAI`` is the client:
tool schemas, tool calls, retries and timeouts come from the framework instead of
hand-written HTTP and JSON parsing. The only local code is the error policy,
which turns provider failures into sentences a patient can act on.
"""

from __future__ import annotations

import openai
from langchain_openai import ChatOpenAI

from app.config import Settings, get_settings

TEMPERATURE = 0.3
MAX_TOKENS = 700
TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 2


class AgentProviderError(RuntimeError):
    """A provider failure with a patient-facing message. Never logs the key."""

    user_message = "Something went wrong on my end. Could you try that again?"


class ProviderNotConfigured(AgentProviderError):
    user_message = (
        "I can't reach the scheduling assistant right now because it isn't configured. "
        "The front desk can still help."
    )


def build_chat_model(settings: Settings | None = None) -> ChatOpenAI:
    """One model instance per turn, bound to DeepSeek's compatible endpoint."""
    resolved = settings or get_settings()
    if not resolved.deepseek_configured:
        raise ProviderNotConfigured("DEEPSEEK_API_KEY is not configured")
    return ChatOpenAI(
        model=resolved.deepseek_model,
        api_key=resolved.deepseek_api_key,
        base_url=resolved.deepseek_base_url,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        timeout=TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
    )


def provider_user_message(exc: BaseException) -> str | None:
    """Map an SDK exception onto a sentence, or ``None`` if it is not one."""
    # Order matters: a timeout is also a connection error.
    if isinstance(exc, openai.APITimeoutError):
        return "That took a little longer than expected. Could you try sending that again?"
    if isinstance(exc, openai.RateLimitError):
        return "I'm getting a lot of requests right now. Could you send that again in a few seconds?"
    if isinstance(exc, openai.APIConnectionError):
        return "I'm having trouble connecting right now. Please try again in a moment."
    if isinstance(exc, openai.APIStatusError):
        return AgentProviderError.user_message
    return None


__all__ = [
    "AgentProviderError",
    "ProviderNotConfigured",
    "build_chat_model",
    "provider_user_message",
]
