"""Tool package: the LangChain tools and the per-turn visibility policy.

Every tool is a ``@tool`` function in ``domains/``; ``registry`` joins them into
one tuple, and ``policy`` decides which of them a model round may see.
"""

from __future__ import annotations

from app.agent.tools.policy import (
    CONFIRM_TOOL_NAMES,
    PUBLIC_TOOL_NAMES,
    REASK_TOOL_NAMES,
    REGISTRATION_GATED_TOOL_NAMES,
    SEARCH_TOOLS,
    SOLO_SEARCH_TOOL,
    TurnMode,
    is_alternative_day_request,
    is_explicit_affirmation,
    is_household,
    is_time_reask,
    turn_mode,
    visible_tools,
)
from app.agent.tools.registry import ALL_TOOLS, TOOL_BY_NAME
from app.agent.tools.results import (
    MAX_TOOL_RESULT_CHARS,
    UNIVERSAL_KEEP,
    NoArguments,
    ToolArguments,
    require_verified,
    tool_error,
    tool_result,
)

__all__ = [
    "ALL_TOOLS",
    "CONFIRM_TOOL_NAMES",
    "MAX_TOOL_RESULT_CHARS",
    "PUBLIC_TOOL_NAMES",
    "REGISTRATION_GATED_TOOL_NAMES",
    "REASK_TOOL_NAMES",
    "SEARCH_TOOLS",
    "SOLO_SEARCH_TOOL",
    "TOOL_BY_NAME",
    "UNIVERSAL_KEEP",
    "NoArguments",
    "ToolArguments",
    "TurnMode",
    "is_alternative_day_request",
    "is_explicit_affirmation",
    "is_household",
    "is_time_reask",
    "require_verified",
    "tool_error",
    "tool_result",
    "turn_mode",
    "visible_tools",
]
