"""The model-facing half of a tool result is bounded; the artifact never is.

A tool returns ``(content, artifact)``. ``render_content`` bounds the JSON the
model reads while the graph keeps the full payload, so cards and proposal ids can
never be trimmed away. These tests pin the four contracts: a small result is
passed through untouched, a large one keeps the decision fields and the cards,
long nested lists are the first thing dropped, and the last resort is still a
usable answer rather than an empty string.
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.tools import ALL_TOOLS, MAX_TOOL_RESULT_CHARS
from app.agent.tools.results import CARD_LIMIT, ITEM_LIMIT, STRING_LIMIT, render_content

MINIMAL_PAYLOAD_KEYS = {"ok", "truncated", "message", "cards", "note"}


def _encode(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, default=str))


def _big(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """A payload just over the limit, padded with a long undeclared field."""
    payload: dict[str, Any] = {
        "ok": True,
        "message": "Here are some times.",
        "cards": [{"slot_id": f"s{index}"} for index in range(4)],
        "proposal_id": "prop_42",
        "kind": "reschedule",
        "detail": "x" * (MAX_TOOL_RESULT_CHARS + 100),
    }
    payload.update(extra or {})
    return payload


def test_result_under_the_limit_is_passed_through_untouched() -> None:
    payload: dict[str, Any] = {"ok": True, "message": "Tomorrow at 9:00 is open.", "detail": ""}
    payload["detail"] = "x" * (MAX_TOOL_RESULT_CHARS - 1 - _encode(payload))
    assert _encode(payload) == MAX_TOOL_RESULT_CHARS - 1

    assert json.loads(render_content(payload)) == payload


def test_result_over_the_limit_keeps_the_decision_fields_and_cards() -> None:
    payload = _big()
    assert _encode(payload) > MAX_TOOL_RESULT_CHARS

    trimmed = json.loads(render_content(payload))

    assert trimmed["truncated"] is True
    assert trimmed["ok"] is True
    assert trimmed["message"] == payload["message"]
    assert trimmed["cards"] == payload["cards"]
    # Scalars survive: the graph acts on ids and kinds.
    assert trimmed["proposal_id"] == "prop_42"
    assert trimmed["kind"] == "reschedule"


def test_result_over_the_limit_clips_the_long_text() -> None:
    payload = _big()

    trimmed = json.loads(render_content(payload))

    assert len(trimmed["detail"]) <= STRING_LIMIT + len("...")
    assert len(render_content(payload)) <= MAX_TOOL_RESULT_CHARS


def test_long_lists_are_capped_rather_than_dropped() -> None:
    payload = {
        "ok": True,
        "message": "Lots of times.",
        "cards": [{"slot_id": f"s{index}", "pad": "y" * 300} for index in range(40)],
        "appointments": [{"id": index, "pad": "z" * 300} for index in range(40)],
    }

    trimmed = json.loads(render_content(payload))

    assert len(trimmed["cards"]) <= CARD_LIMIT
    assert len(trimmed["appointments"]) <= ITEM_LIMIT
    assert trimmed["truncated"] is True


def test_final_stage_payload_is_the_minimal_marker() -> None:
    # Everything is huge, so the last-resort shape is used. It must still answer.
    payload = _big({"cards": [{"slot_id": f"s{index}", "pad": "y" * 2000} for index in range(40)]})

    trimmed = json.loads(render_content(payload))

    assert set(trimmed) == MINIMAL_PAYLOAD_KEYS
    assert trimmed["truncated"] is True
    assert trimmed["ok"] is True
    assert isinstance(trimmed["cards"], list)


def test_universal_fields_survive_a_trim() -> None:
    payload = {
        "ok": True,
        "message": "m",
        "note": "n",
        "next_step": "s",
        "cards": [],
        "detail": "x" * (MAX_TOOL_RESULT_CHARS * 3),
    }

    trimmed = json.loads(render_content(payload))

    assert set(trimmed) == {
        "ok",
        "message",
        "note",
        "next_step",
        "cards",
        "detail",
        "truncated",
    }
    assert len(trimmed["detail"]) <= STRING_LIMIT + len("...")


def test_every_tool_returns_content_and_an_artifact() -> None:
    # Without the artifact, trimming the model-facing text would be the only copy
    # of the cards. The response format is therefore not optional.
    for tool in ALL_TOOLS:
        assert tool.response_format == "content_and_artifact", tool.name
        assert tool.args_schema is not None, tool.name
