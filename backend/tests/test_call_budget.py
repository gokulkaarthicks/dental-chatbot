"""More actions than one step can run are refused out loud, never dropped.

The round budget exists because a model that is allowed to fire unlimited calls
in one step can hold, propose and confirm the same card three times over. The
cap itself is old; what changed is what happens to the calls past it. Dropping
them silently left an assistant message with tool calls that had no results, so
the next provider request was malformed, and the model had no idea its plan had
been cut short. Now every call is answered, and the ones over budget are told
why nothing happened.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, ToolMessage

from app.agent import runner
from app.agent.graph import MAX_TOOL_CALLS_PER_ROUND
from app.services import conversations
from tests.support import ScriptedChatModel

# Read-only calls, so a round can ask for more of them than it is allowed to run.
READERS = (
    "get_session_context",
    "get_practice_summary",
    "get_practice_info",
    "get_booking_context",
    "get_patient_appointments",
)


def _calls() -> list[dict]:
    return [
        {"name": name, "args": {}, "id": f"call_{index}", "type": "tool_call"}
        for index, name in enumerate(READERS, start=1)
    ]


def _run(scenario, model: ScriptedChatModel):  # noqa: ANN001, ANN202
    session, requester = scenario
    return runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text="what's the situation with my booking?",
        llm=model,
    )


def test_a_round_over_budget_runs_what_it_can_and_refuses_the_rest(scenario, caplog) -> None:
    caplog.set_level(logging.INFO)
    assert len(READERS) > MAX_TOOL_CALLS_PER_ROUND
    model = ScriptedChatModel(
        [
            AIMessage(content="", tool_calls=_calls()),
            AIMessage(content="Here's what I have."),
        ]
    )

    # ``tool.executed`` is an INFO event, so the capture has to be told to keep
    # INFO records from this logger; the default level would drop every one.
    with caplog.at_level(logging.INFO, logger="app.agent.graph"):
        result = _run(scenario, model)

    assert result.degraded is False
    assert result.reply == "Here's what I have."

    executed = [
        record.fields
        for record in caplog.records
        if getattr(record, "event", None) == "tool.executed"
    ]
    assert len(executed) == len(READERS), "every call is answered exactly once"
    refusals = [fields for fields in executed if fields.get("error") == "too_many_calls"]
    assert len(refusals) == len(READERS) - MAX_TOOL_CALLS_PER_ROUND
    # The ones inside the budget ran; only the tail was refused.
    assert executed[:MAX_TOOL_CALLS_PER_ROUND] and all(
        fields.get("ok") for fields in executed[:MAX_TOOL_CALLS_PER_ROUND]
    )


def test_the_model_is_told_why_a_call_did_not_run(scenario) -> None:
    model = ScriptedChatModel(
        [
            AIMessage(content="", tool_calls=_calls()),
            AIMessage(content="Let me know if you want anything else."),
        ]
    )

    _run(scenario, model)

    # The next round reads this round's results, so the refusal is in front of it.
    answers = [message for message in model.calls[1] if isinstance(message, ToolMessage)]
    assert len(answers) == len(READERS)
    refused = [message for message in answers if "too_many_calls" in str(message.content)]
    assert len(refused) == len(READERS) - MAX_TOOL_CALLS_PER_ROUND
    assert all(message.status == "error" for message in refused)
    assert "did not" in refused[0].content


def test_the_transcript_stays_paired_with_every_call(scenario) -> None:
    """Five calls and five results: the thread still replays to the provider."""
    model = ScriptedChatModel(
        [
            AIMessage(content="", tool_calls=_calls()),
            AIMessage(content="Done."),
        ]
    )

    session, requester = scenario
    _run(scenario, model)

    rows = conversations.history(session, requester.conversation)
    calling = [row for row in rows if row.role == "assistant" and row.tool_calls]
    assert len(calling) == 1
    assert len(calling[0].tool_calls) == len(READERS)

    answers = [row for row in rows if row.role == "tool"]
    assert {row.tool_call_id for row in answers} == {call["id"] for call in calling[0].tool_calls}
    assert all(row.content for row in answers), "no call was left without a result"
