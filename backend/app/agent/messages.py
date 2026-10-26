"""The transcript as LangChain messages.

SQLite keeps the canonical thread; this module is the one bridge between those
rows and the message objects the graph runs on. Two directions:

* ``thread_messages`` rebuilds ``HumanMessage`` / ``AIMessage`` / ``ToolMessage``
  for a stored turn, pairing every tool call with its result so the provider
  accepts the replay;
* ``stored_tool_calls`` converts an ``AIMessage`` back into the rows we persist.

The system prompt is not stored. It is rebuilt every round because verification
can change session state in the middle of a turn. It always carries the same id,
so ``add_messages`` replaces it in place instead of appending a second copy.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from app.models import Message

SYSTEM_MESSAGE_ID = "system"


def system_message(text: str) -> SystemMessage:
    return SystemMessage(content=text, id=SYSTEM_MESSAGE_ID)


def thread_messages(rows: list[Message]) -> list[AnyMessage]:
    """Stored rows to provider messages, with tool calls paired to results.

    ``system`` and ``staff`` rows are skipped: the prompt is rebuilt per turn.
    A tool call whose result row is missing still gets a placeholder result,
    because an unanswered call is rejected by the provider and would make the
    whole thread unreplayable.
    """
    results: dict[str, list[Message]] = {}
    for row in rows:
        if row.role == "tool" and row.tool_call_id:
            results.setdefault(row.tool_call_id, []).append(row)

    messages: list[AnyMessage] = []
    for row in rows:
        if row.role == "patient":
            messages.append(HumanMessage(content=row.content))
        elif row.role == "assistant":
            calls = [_tool_call(call) for call in (row.tool_calls or [])]
            messages.append(AIMessage(content=row.content or "", tool_calls=calls))
            for call in calls:
                followers = results.pop(call["id"], [])
                messages.append(
                    ToolMessage(
                        content=(
                            followers[0].content
                            if followers
                            else json.dumps({"ok": False, "error": "missing_result"})
                        ),
                        tool_call_id=call["id"],
                        name=followers[0].tool_name if followers else call["name"],
                    )
                )
    return messages


def stored_tool_calls(message: AIMessage) -> list[dict[str, Any]]:
    """The wire shape persisted for one assistant turn's tool calls."""
    return [
        {
            "id": call["id"],
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call["args"], default=str),
            },
        }
        for call in message.tool_calls
    ]


def _tool_call(entry: dict[str, Any]) -> dict[str, Any]:
    """One persisted tool call in the shape ``AIMessage`` expects."""
    function = entry.get("function") or {}
    raw = function.get("arguments")
    if isinstance(raw, str):
        try:
            arguments = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            arguments = {}
    elif isinstance(raw, dict):
        arguments = raw
    else:
        arguments = {}
    return {
        "name": function.get("name") or entry.get("name") or "",
        "args": arguments,
        "id": entry.get("id") or "",
        "type": "tool_call",
    }


__all__ = ["SYSTEM_MESSAGE_ID", "stored_tool_calls", "system_message", "thread_messages"]
