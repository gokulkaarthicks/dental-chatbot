"""The turn graph: shape, the tool cycle, and the round limit.

These tests exercise the LangGraph wiring itself, not the domain rules. They pin
the properties the turn must always have: the model receives real LangChain
messages, a tool round feeds its ``ToolMessage`` results back to the model, the
cycle is bounded, a tool the model was never offered cannot run, and the compiled
graph is built once per process.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent import runner
from app.agent.graph import MAX_TOOL_ROUNDS, TURN_GRAPH, build_graph
from app.ai import AgentProviderError
from app.services import conversations
from tests.support import ScriptedChatModel

NODES = {"ingest", "agent", "tools", "observe", "finalize"}


def _tool_call(call_id: str, name: str = "get_session_context", **arguments):
    return {"name": name, "args": arguments, "id": call_id, "type": "tool_call"}


def _turn(content: str, *calls) -> AIMessage:  # noqa: ANN002
    return AIMessage(content=content, tool_calls=list(calls))


def test_the_graph_is_wired_once() -> None:
    graph = TURN_GRAPH.get_graph()

    assert set(graph.nodes) >= NODES
    assert build_graph() is not TURN_GRAPH, "the compiled graph is a process singleton"


def test_a_tool_round_feeds_its_result_back_to_the_model(scenario) -> None:
    session, requester = scenario
    llm = ScriptedChatModel(
        [_turn("", _tool_call("call_1")), _turn("I can help with that.")]
    )

    result = runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text="hello",
        llm=llm,
    )

    assert result.degraded is False
    assert result.reply == "I can help with that."
    assert len(llm.calls) == 2
    first_round, second_round = llm.calls
    assert isinstance(first_round[0], SystemMessage)
    assert any(isinstance(message, HumanMessage) for message in first_round)
    # The second round carries the assistant's tool call and the tool's answer,
    # which is exactly what LangGraph's message reducer gives the model.
    assert any(
        isinstance(message, AIMessage) and message.tool_calls for message in second_round
    )
    assert any(isinstance(message, ToolMessage) for message in second_round)


def test_the_tool_cycle_stops_at_the_round_limit(scenario) -> None:
    session, requester = scenario
    scripted = [_turn("", _tool_call(f"call_{index}")) for index in range(MAX_TOOL_ROUNDS)]
    llm = ScriptedChatModel(scripted)

    result = runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text="hello",
        llm=llm,
    )

    assert len(llm.calls) == MAX_TOOL_ROUNDS, "the graph must not exceed the round budget"
    assert llm.turns == []
    assert result.reply, "the limit still yields something to say"


def test_no_tool_call_ends_the_turn_after_one_model_round(scenario) -> None:
    session, requester = scenario
    llm = ScriptedChatModel([_turn("We open at nine.")])

    result = runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text="what time do you open",
        llm=llm,
    )

    assert len(llm.calls) == 1
    assert result.reply == "We open at nine."
    assert result.cards == []


def test_a_tool_the_round_was_never_offered_cannot_run(scenario) -> None:
    """The node holds every tool; only the bound ones may execute."""
    session, requester = scenario
    llm = ScriptedChatModel(
        [_turn("", _tool_call("call_1", name="delete_everything")), _turn("Sorry about that.")]
    )

    result = runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text="hello",
        llm=llm,
    )

    assert result.degraded is False
    assert result.reply == "Sorry about that."
    tool_messages = [
        message for message in llm.calls[1] if isinstance(message, ToolMessage)
    ]
    assert tool_messages, "the refused call is still answered"
    assert "tool_not_available" in tool_messages[0].content


def test_a_real_search_lands_on_the_final_turn_as_cards(scenario) -> None:
    """The whole path: tool returns cards, finalize attaches and formats them."""
    session, requester = scenario
    llm = ScriptedChatModel(
        [
            _turn(
                "",
                _tool_call(
                    "call_1",
                    name="search_availability",
                    appointment_type="cleaning",
                    date_phrases=["2030-05-07"],
                ),
            ),
            _turn("Here are the times. 9:00 AM works. Which works for you?"),
        ]
    )

    result = runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text="I need a cleaning on 2030-05-07",
        llm=llm,
    )

    assert result.degraded is False
    assert result.cards, "the search must produce clickable options"
    assert all(card["type"] == "slot" for card in result.cards)
    last = conversations.history(session, requester.conversation)[-1]
    assert last.role == "assistant" and not last.tool_calls
    assert last.cards == result.cards, "the turn's own reply carries the grid"
    assert result.reply == last.content, "one stored message per turn, no rewrite duplicate"


def test_provider_failure_degrades_without_a_dangling_turn(scenario) -> None:
    session, requester = scenario
    error = AgentProviderError("upstream failed")

    result = runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text="hello",
        llm=ScriptedChatModel(error=error),
    )

    assert result.degraded is True
    assert result.reply == AgentProviderError.user_message
    last = conversations.history(session, requester.conversation)[-1]
    assert last.role == "assistant"
    assert last.tool_calls is None


def test_unexpected_failure_degrades_to_the_fallback_reply(scenario) -> None:
    session, requester = scenario

    result = runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text="hello",
        llm=ScriptedChatModel(error=RuntimeError("boom")),
    )

    assert result.degraded is True
    assert result.reply == runner.FALLBACK_REPLY
    last = conversations.history(session, requester.conversation)[-1]
    assert last.role == "assistant"
    assert last.tool_calls is None
