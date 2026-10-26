"""The result contract shared by every tool.

A tool returns ``(content, artifact)``, which is what LangChain's
``response_format="content_and_artifact"`` expects: the model reads the JSON
string, the graph reads the artifact. Interactive cards travel in the artifact,
so trimming the model-facing text can never delete a patient's clickable times.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langgraph.prebuilt import InjectedState
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from app.agent.context import TurnDeps
from app.models import Patient

MAX_TOOL_RESULT_CHARS = 6000
CARD_LIMIT = 12
ITEM_LIMIT = 8
# Free text the graph never acts on. A long description is clipped, not dropped,
# so the model still learns the field existed and was cut.
STRING_LIMIT = 400

# Fields the graph and the UI act on. They are kept whole, whatever their size.
UNIVERSAL_KEEP = frozenset({"ok", "error", "message", "note", "next_step", "truncated", "cards"})


class ToolArguments(BaseModel):
    """Argument models reject unknown fields instead of silently ignoring them.

    ``deps`` is not something the model chooses: ``ToolNode`` injects it from
    graph state because the tool function annotates it with ``InjectedState``.
    Declaring it here is what lets pydantic validate the injected call, and
    ``SkipJsonSchema`` keeps it out of the schema the model is shown, so it is
    never something the model can offer to set.
    """

    model_config = ConfigDict(extra="forbid")

    deps: SkipJsonSchema[Annotated[Any, InjectedState("deps")]] = Field(
        default=None, exclude=True, repr=False
    )


class NoArguments(ToolArguments):
    """For tools that take no input."""


def tool_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": code, "message": message}
    payload.update(extra)
    return payload


def tool_result(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The value every tool returns: model-facing JSON plus the graph artifact."""
    return render_content(payload), payload


def render_content(payload: dict[str, Any]) -> str:
    """Bound the model-facing text of a result without losing the artifact.

    The full payload always survives as the artifact, so cards and proposal ids
    are never at risk here. When a result is too large, the decision fields are
    kept whole, free text is clipped, and nested lists are capped before any of
    it reaches the prompt. A last-resort shape is still better than an unbounded
    prompt.
    """
    encoded = json.dumps(payload, default=str)
    if len(encoded) <= MAX_TOOL_RESULT_CHARS:
        return encoded

    slim: dict[str, Any] = {}
    for key, value in payload.items():
        if key in UNIVERSAL_KEEP:
            slim[key] = value
        elif isinstance(value, str):
            slim[key] = value if len(value) <= STRING_LIMIT else f"{value[:STRING_LIMIT]}..."
        elif isinstance(value, (int, float, bool)) or value is None:
            slim[key] = value
    cards = slim.get("cards")
    if isinstance(cards, list):
        # One grid is a choice, not an archive.
        slim["cards"] = cards[:CARD_LIMIT]
    appointments = payload.get("appointments")
    if isinstance(appointments, list):
        slim["appointments"] = appointments[:ITEM_LIMIT]
    slim["truncated"] = True
    rendered = json.dumps(slim, default=str)
    if len(rendered) <= MAX_TOOL_RESULT_CHARS * 2:
        return rendered

    raw_cards = payload.get("cards")
    return json.dumps(
        {
            "ok": payload.get("ok", True),
            "truncated": True,
            "message": payload.get("message"),
            "cards": raw_cards[:6] if isinstance(raw_cards, list) else [],
            "note": "Result trimmed for size. Ask a narrower question if you need more.",
        },
        default=str,
    )


def require_verified(deps: TurnDeps) -> tuple[Patient | None, dict[str, Any] | None]:
    """The verified patient for this conversation, or the refusal to return."""
    patient = deps.verified_patient
    if patient is None:
        return None, tool_error(
            "not_verified",
            (
                "This session isn't verified. Ask for phone and date of birth to verify an "
                "existing patient, or register them first."
            ),
        )
    return patient, None


__all__ = [
    "CARD_LIMIT",
    "ITEM_LIMIT",
    "MAX_TOOL_RESULT_CHARS",
    "STRING_LIMIT",
    "UNIVERSAL_KEEP",
    "NoArguments",
    "ToolArguments",
    "render_content",
    "require_verified",
    "tool_error",
    "tool_result",
]
