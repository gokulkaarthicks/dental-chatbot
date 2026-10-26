"""The tool catalogue: every tool the graph may offer a model round.

A duplicate name fails at import time rather than on the first patient turn,
because two tools with one name would make a tool call ambiguous.
"""

from __future__ import annotations

from collections import Counter

from langchain_core.tools import BaseTool

from app.agent.tools.domains import DOMAIN_TOOLS

ALL_TOOLS: tuple[BaseTool, ...] = DOMAIN_TOOLS

TOOL_BY_NAME: dict[str, BaseTool] = {tool.name: tool for tool in ALL_TOOLS}

_duplicates = sorted(name for name, count in Counter(t.name for t in ALL_TOOLS).items() if count > 1)
if _duplicates:
    raise ValueError(f"Duplicate tool name(s): {_duplicates}")


__all__ = ["ALL_TOOLS", "TOOL_BY_NAME"]
