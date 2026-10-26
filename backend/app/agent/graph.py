"""The LangGraph turn graph for the patient front-desk agent.

One invocation is one patient message:

    START -> ingest -> agent -> (tools -> observe -> agent)* -> finalize -> END

Everything that used to be hand-rolled here is now framework machinery:

* ``ingest`` persists the patient message and rebuilds the thread as LangChain
  messages, so the model receives ``HumanMessage`` / ``AIMessage(tool_calls)`` /
  ``ToolMessage`` objects instead of provider dictionaries.
* ``agent`` binds the tools this round may see with ``llm.bind_tools`` and lets
  the model answer with native tool calls.
* ``tools`` runs LangGraph's ``ToolNode`` over exactly the tools that were
  bound, so a call the model could not see cannot execute.
* ``observe`` folds the round into the turn: cards, search/exhaustion flags,
  the refreshed system prompt after a mid-turn verification, and the round cap.
* ``finalize`` is the single exit. It applies the card and prose rules, refreshes
  a re-ask from SQLite, and guarantees an emergency notice.

Why there is no checkpointer
----------------------------
SQLite is the single source of truth for conversation state: the thread table
plus the revisioned scheduling request. The graph is invoked per turn with that
state already loaded, so persisting LangGraph checkpoints would create a second
copy of the same facts with no resumption use case behind it. If a future flow
needs to pause for days and resume, add a checkpointer then, and keep it fed from
the same rows.

The state carries request-scoped objects (the SQLAlchemy session, the chat model,
the patient session). That is safe precisely because nothing serializes it, and
it is what lets a test inject a scripted chat model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolInvocationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.agent import prompts, reply_format
from app.agent.context import TurnDeps
from app.agent.messages import stored_tool_calls, system_message, thread_messages
from app.agent.tools import (
    ALL_TOOLS,
    SEARCH_TOOLS,
    TurnMode,
    is_alternative_day_request,
    is_explicit_affirmation,
    is_household,
    is_time_reask,
    turn_mode,
    visible_tools,
)
from app.agent.tools.results import tool_error
from app.models import Conversation, utcnow
from app.observability import log_event
from app.services import (
    availability_read,
    conversations,
    patient_sessions,
    patients,
    scheduling,
)
from app.tenancy import ClinicContext

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS_PER_ROUND = 4

EXHAUSTED_REPLY = (
    "I checked what's open and don't have a complete set for everyone "
    "on those days. Would you like a different day, or is a different "
    "time of day okay?"
)
ROUND_LIMIT_REPLY = (
    "I've gathered what I need for now. The front desk will follow up to confirm the next step."
)


class AgentState(TypedDict, total=False):
    """Everything one turn accumulates.

    ``messages`` is a LangChain message list merged by ``add_messages``: a node
    returns only what it produced, and the reducer appends it (or replaces a
    message whose id already exists, which is how the system prompt is refreshed
    in place). Every other key is plain overwrite state.
    """

    deps: TurnDeps
    messages: Annotated[list[AnyMessage], add_messages]
    text: str
    reply: str
    cards: list[dict[str, Any]]
    bound: tuple[str, ...]
    # The tool calls this round's budget allows to execute, in the order the
    # model asked for them. Anything past the budget is refused, not dropped.
    call_budget: tuple[str, ...]
    # Whether the patient's own message this turn agreed to a pending step.
    may_confirm: bool
    last_tool_message: str
    rounds: int
    searched: bool
    exhausted: bool
    mode: TurnMode
    household: bool
    reask: bool
    turn_floor: int
    first_message: bool
    request_before: dict[str, Any] | None


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


def ingest(state: AgentState) -> dict[str, Any]:
    """Persist the patient message and load everything the turn starts from."""
    deps = state["deps"]
    session = deps.session
    conversation = deps.conversation

    history = conversations.history(session, conversation)
    first_message = conversations.visible_messages(history) == []
    patient_sessions.touch(deps.patient_session)

    conversations.append_patient_message(session, conversation, state["text"])
    conversations.backfill_title(session, conversation)
    session.commit()

    # Everything already in the thread is earlier context. This turn's cards
    # must attach to this turn's own reply, never to a message the patient
    # wrote before the search ran: that would put an offer above the answer it
    # belongs to, and repeat it further down.
    turn_floor = conversations.last_message_id(session, conversation)
    request_before = scheduling.scheduling_request(session, conversation.id)
    # Two related but distinct facts: a re-ask refreshes the stored search at the
    # end of the turn, while the click arm only narrows which tools may run.
    reask = is_time_reask(state["text"])
    mode = turn_mode(reask=reask, family_option_identity=deps.family_option_identity)
    # Whose authority this turn carries. A confirmation may only be spent on the
    # patient's own agreement, so it is decided here from their words and never
    # from what the model later says about them.
    may_confirm = is_explicit_affirmation(state["text"])

    messages: list[AnyMessage] = [
        system_message(_system_prompt(deps)),
        *thread_messages(conversations.history(session, conversation)),
    ]

    if may_confirm:
        log_event(logger, "turn.confirm_granted", conversation=conversation.public_id)

    return {
        "messages": messages,
        "mode": mode,
        "reask": reask,
        "may_confirm": may_confirm,
        "household": is_household(request_before),
        "request_before": request_before,
        "turn_floor": turn_floor,
        "first_message": first_message,
        "cards": [],
        "last_tool_message": "",
        "rounds": 0,
        "searched": False,
        "exhausted": False,
    }


def agent(state: AgentState) -> dict[str, Any]:
    """One model round: either native tool calls, or this turn's prose."""
    deps = state["deps"]
    # Read every round, not once per turn: a verification can land in the middle
    # of a turn, and the round after it should see the tools it can now use.
    patient = deps.verified_patient
    tools = visible_tools(
        mode=state.get("mode", TurnMode.DEFAULT),
        searched=state.get("searched", False),
        exhausted=state.get("exhausted", False),
        household=state.get("household", False),
        verified=patient is not None,
        registration_complete=bool(
            patient is not None and all(patients.registration_completeness(patient).values())
        ),
        may_confirm=state.get("may_confirm", False),
    )
    response = _respond(deps, state["messages"], tools)
    prose = _text(response)
    fallback = (
        state.get("last_tool_message")
        or (EXHAUSTED_REPLY if state.get("exhausted") else "")
    )

    if not response.tool_calls:
        reply = prose or fallback
        _close_turn(deps, reply, state.get("cards", []))
        return {"messages": [response], "reply": reply, "bound": ()}

    if not tools:
        # The policy withdrew every tool (an empty search, or a re-ask that
        # already searched). A call here cannot be executed, and replaying a
        # call the provider was never offered would corrupt the next round.
        reply = prose or fallback
        _close_turn(deps, reply, state.get("cards", []))
        return {"messages": [AIMessage(content=reply)], "reply": reply, "bound": ()}

    conversations.append_assistant_message(
        deps.session,
        deps.conversation,
        content=_text(response),
        tool_calls=stored_tool_calls(response),
    )
    deps.session.commit()

    return {
        "messages": [response],
        # The ToolNode holds every tool; this is the set the model was actually
        # offered, and the wrapper refuses anything outside it.
        "bound": tuple(tool.name for tool in tools),
        # Every call is kept and answered, so the transcript stays paired with
        # the thread. The budget decides which ones execute: a call past it is
        # refused with a reason rather than silently dropped, so the model learns
        # it asked for more than one round can do.
        "call_budget": tuple(
            call["id"] for call in response.tool_calls[:MAX_TOOL_CALLS_PER_ROUND]
        ),
        "rounds": state.get("rounds", 0) + 1,
    }


def observe(state: AgentState) -> dict[str, Any]:
    """Fold the round the ``ToolNode`` just ran into the turn.

    Execution belongs to ``ToolNode``; interpretation belongs here. Reading the
    messages after the last assistant turn is exactly this round's results,
    whatever order the node wrote them in.
    """
    deps = state["deps"]
    tool_messages = _round_tool_messages(state["messages"])
    searched = state.get("searched", False)
    exhausted = state.get("exhausted", False)
    last_tool_message = state.get("last_tool_message", "")
    reask = state.get("mode") is TurnMode.REASK
    round_cards: list[dict[str, Any]] = []

    for message in tool_messages:
        payload = _payload(message)
        if payload.get("message"):
            last_tool_message = str(payload["message"])
        if reask and message.name in SEARCH_TOOLS:
            searched = True
        if payload.get("availability_checked") is True and not payload.get("cards"):
            # A complete empty search is terminal for this model turn. Do not
            # let the provider turn its own no-result response into a repeated
            # search or callback loop.
            exhausted = True
        round_cards.extend(_cards_from(payload))

    cards = (
        _merge_round_cards(state.get("cards", []), round_cards)
        if round_cards
        else state.get("cards", [])
    )
    _finish_tool_turn(deps.session, deps.conversation)

    # Verification can happen inside a tool call, so the patient binding and the
    # system prompt are both refreshed before the next model round.
    _sync_patient(deps)
    update: dict[str, Any] = {
        "messages": [system_message(_system_prompt(deps))],
        "cards": cards,
        "searched": searched,
        "exhausted": exhausted,
        "bound": (),
        "call_budget": (),
        "last_tool_message": last_tool_message,
    }

    if state.get("rounds", 0) >= MAX_TOOL_ROUNDS:
        logger.info(
            "Tool round limit reached for conversation %s", deps.conversation.public_id
        )
        reply = last_tool_message or ROUND_LIMIT_REPLY
        _close_turn(deps, reply, cards)
        update["reply"] = reply

    return update


def finalize(state: AgentState) -> dict[str, Any]:
    """The single exit: apply the card and prose rules, then hand back the reply."""
    deps = state["deps"]
    session = deps.session
    conversation = deps.conversation
    patient_session = deps.patient_session

    reply = state.get("reply", "")
    cards = state.get("cards", [])
    reask = state.get("reask", False)
    turn_floor = state.get("turn_floor")

    if reask:
        reply, cards = _refresh_reask(
            deps, reply=reply, request_before=state.get("request_before"), text=state["text"]
        )

    reply, noticed = _ensure_emergency_notice(reply, cards)
    if noticed:
        _replace_last_assistant_text(session, conversation, reply)
        session.commit()

    if any(
        isinstance(card, dict) and card.get("type") == "slot" and card.get("settled")
        for card in cards
    ) and reply:
        # The server is telling the UI these times are context, not offers. Any
        # "which one works for you?" the model wrote would contradict that.
        # The real cards are passed through so a held grid and a booked one get
        # the wording that matches what actually happened.
        settled_note = reply_format.settled_intro(cards)
        if reply.strip() != settled_note:
            reply = settled_note
            _replace_last_assistant_text(session, conversation, reply)

    # Anything still pending from an earlier turn stays actionable, so a refresh
    # (or a search that superseded nothing) cannot lose an active proposal. Only
    # a turn that already shows cards is merged: a plain answer must not sprout
    # a proposal card in the middle of an unrelated conversation.
    #
    # A card may be carried by the tool result (a declined proposal renders as
    # "Cancelled"). Those cards already sit on the turn's own reply, so they are
    # not attached a second time: doing so is what showed the same cancelled box
    # twice, with an empty message between them.
    carried = {_card_identity(card) for card in _rendered_cards(session, conversation)}
    to_attach = [card for card in cards if _card_identity(card) not in carried] if cards else []
    if to_attach:
        cards = scheduling.with_live_proposals(session, conversation.id, to_attach)
        # A grid this request has already held or booked is context, not an
        # offer, however it was produced.
        cards = scheduling.settle_superseded_grids(session, deps.clinic, conversation.id, cards)
        # One identity, one card: a retried tool call or a re-derived proposal
        # must not render the same option twice.
        cards = _dedupe_cards(cards)
        # The turn's cards belong to its text. Attaching them here, before the
        # intro is rewritten, is what makes a refresh replay the same grid: a
        # restored thread used to lose every slot card and keep only prose that
        # told the patient to pick a time no longer on screen.
        _attach_cards(session, conversation, cards, turn_floor=turn_floor)

    # "Do you have any other slots on Monday?" where the model answered in prose
    # without searching again: the grid is still in the thread and still the
    # answer, so it is shown again under this turn rather than leaving the reply
    # pointing at cards "above". Only a verified patient with times already
    # rendered gets this; a cardless thread is handled below.
    if reask and not cards:
        earlier = _slot_cards_in_thread(session, conversation, turn_floor=turn_floor)
        if earlier:
            replayed = scheduling.with_live_proposals(session, conversation.id, earlier)
            cards = scheduling.settle_superseded_grids(
                session, deps.clinic, conversation.id, replayed
            )
            _attach_cards(session, conversation, cards, turn_floor=turn_floor)
            log_event(
                logger,
                "reply.grid_replayed",
                conversation=conversation.public_id,
                cards=len(cards),
            )

    if not reply and not cards:
        # Nothing to show and nothing to say. For a verified patient asking
        # about times, "ask me again" is not an answer: it sends them round the
        # same loop. Say what is true and offer the two real next steps.
        if reask and patient_sessions.verified_patient(session, patient_session) is not None:
            reply = (
                "I don't have any open times to show for that right now. I can check "
                "another day, or ask the front desk to call you about times they keep "
                "off the online schedule."
            )
        else:
            reply = "Could you say a little more about what you need?"
        persist_assistant_text(session, conversation, reply)
        session.commit()
    elif cards:
        # Enforce the "cards, not prose" rule regardless of what the model wrote:
        # echoes of times are stripped and an empty intro becomes a card-derived
        # one. The cards were attached to the turn's text above, so a refresh
        # replays them in place.
        formatted, reformatted = reply_format.format_assistant_text(reply, cards, refreshed=reask)
        if reformatted:
            reply = formatted
            # The stored copy carries the cleaned intro, so a refresh cannot
            # bring back the raw prose (or a dash) the patient never saw.
            _replace_last_assistant_text(session, conversation, reply)
            log_event(
                logger,
                "reply.formatted",
                conversation=conversation.public_id,
                cards=len(cards),
            )
        session.commit()
    else:
        # A plain answer with no cards skips the card formatting above, so the
        # style rule is applied here. The text was already stored by the model
        # round; rewrite that same row rather than appending a second message.
        clean = reply_format.strip_dashes(reply)
        if clean != reply:
            reply = clean
            _replace_last_assistant_text(session, conversation, clean)
            session.commit()

    if state.get("first_message"):
        logger.info("First patient message stored for conversation %s", conversation.public_id)

    return {"reply": reply, "cards": cards}


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def _route_after_agent(state: AgentState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else "finalize"


def _route_after_observe(state: AgentState) -> str:
    # ``observe`` only sets ``reply`` when it had to end the turn itself, which
    # is exactly when another model round must not run.
    return "finalize" if state.get("reply") else "agent"


def build_graph():
    """Wire and compile the turn graph.

    Compiled once at import. The graph holds no per-request state, so a single
    instance serves every turn.
    """
    graph = StateGraph(AgentState)
    graph.add_node("ingest", ingest)
    graph.add_node("agent", agent)
    graph.add_node(
        "tools",
        ToolNode(
            ALL_TOOLS,
            messages_key="messages",
            handle_tool_errors=_handle_tool_error,
            wrap_tool_call=_wrap_tool_call,
        ),
    )
    graph.add_node("observe", observe)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "agent")
    graph.add_conditional_edges(
        "agent", _route_after_agent, {"tools": "tools", "finalize": "finalize"}
    )
    graph.add_edge("tools", "observe")
    graph.add_conditional_edges(
        "observe", _route_after_observe, {"agent": "agent", "finalize": "finalize"}
    )
    graph.add_edge("finalize", END)
    return graph.compile()


# --------------------------------------------------------------------------- #
# Model and tool plumbing
# --------------------------------------------------------------------------- #


def _respond(deps: TurnDeps, messages: list[AnyMessage], tools: tuple[Any, ...]) -> AIMessage:
    """One model call with exactly this round's tools bound."""
    model = deps.llm.bind_tools(tools) if tools else deps.llm
    response = model.invoke(messages)
    if not isinstance(response, AIMessage):
        raise TypeError(f"the chat model returned {type(response).__name__}, not an assistant message")
    return response


def _round_tool_messages(messages: list[AnyMessage]) -> list[ToolMessage]:
    """The tool results produced by the round that just ran.

    They are the messages between here and the assistant message that requested
    them, read backwards, so the answer is exact without a second state key.
    """
    collected: list[ToolMessage] = []
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            break
        if isinstance(message, ToolMessage):
            collected.append(message)
    return list(reversed(collected))


def _handle_tool_error(exc: Exception) -> str:
    """Turn a tool failure into a result the model can recover from."""
    if isinstance(exc, ToolInvocationError):
        field_errors = {
            str(error["loc"][0]): error.get("msg", "Invalid value")
            for error in (exc.filtered_errors or [])
            if error.get("loc")
        }
        return json.dumps(
            tool_error(
                "invalid_arguments",
                "Those details didn't validate. Ask the patient again for the relevant field.",
                field_errors=field_errors,
            )
        )
    if isinstance(exc, ValidationError):
        return json.dumps(
            tool_error(
                "invalid_arguments",
                "Those details didn't validate. Ask the patient again for the relevant field.",
                field_errors={
                    str(error["loc"][0]): error.get("msg", "Invalid value")
                    for error in exc.errors()
                    if error.get("loc")
                },
            )
        )
    logger.exception("Tool raised an unexpected error")
    return json.dumps(tool_error("tool_failed", "Something went wrong on my end with that step."))


def _wrap_tool_call(
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Any],
) -> ToolMessage | Any:
    """The one hook around every execution: override, guard, persist, log.

    A browser click is authoritative for which family option was chosen. The
    model supplies the family members and index but cannot select a different
    historical option than the card the patient pressed.
    """
    deps: TurnDeps = request.state["deps"]
    call = _patient_clicked_call(request.tool_call, deps)
    if call is not request.tool_call:
        request = request.override(tool_call=call)

    bound = request.state.get("bound") or ()
    if call["name"] not in bound:
        # The node holds every tool, but only the tools this round was offered
        # may run. A hallucinated name is answered, never executed.
        return _refusal(
            deps,
            call,
            "tool_not_available",
            (
                "That action isn't available in this step. Use one of the tools you were "
                "offered, or answer in words."
            ),
        )

    budget = request.state.get("call_budget")
    if budget is not None and call["id"] not in budget:
        # Past this round's budget. The call is still answered, so the thread
        # stays paired and the model is told why nothing happened rather than
        # silently losing an action it asked for.
        return _refusal(
            deps,
            call,
            "too_many_calls",
            (
                f"Only {MAX_TOOL_CALLS_PER_ROUND} actions can run in one step, and this one "
                "did not. Finish what you were given, then ask for the rest in the next "
                "step, or answer in words."
            ),
        )

    outcome = handler(request)
    if isinstance(outcome, ToolMessage):
        _record_outcome(deps, outcome)
    return outcome


def _refusal(deps: TurnDeps, call: Any, code: str, message: str) -> ToolMessage:
    """One call that was not executed, answered as a tool result."""
    refusal = ToolMessage(
        content=json.dumps(tool_error(code, message)),
        name=call["name"],
        tool_call_id=call["id"],
        status="error",
    )
    _record_outcome(deps, refusal)
    return refusal


def _patient_clicked_call(call: Any, deps: TurnDeps) -> Any:
    if call["name"] != "hold_family_option" or not deps.family_option_identity:
        return call
    arguments = dict(call.get("args") or {})
    arguments["option_identity"] = deps.family_option_identity
    arguments["request_id"] = deps.family_option_request_id
    return {**call, "args": arguments}


def _record_outcome(deps: TurnDeps, message: ToolMessage) -> None:
    """Persist one tool result and log its outcome (never its arguments)."""
    payload = _payload(message)
    deps.session.flush()
    conversations.append_tool_message(
        deps.session,
        deps.conversation,
        tool_call_id=message.tool_call_id,
        tool_name=message.name or "",
        payload=payload,
    )
    deps.session.commit()
    log_event(
        logger,
        "tool.executed",
        tool=message.name,
        ok=bool(payload.get("ok")),
        error=payload.get("error"),
        cards=len(payload.get("cards") or []),
    )


def _payload(message: ToolMessage) -> dict[str, Any]:
    """The structured result of one tool message.

    Tools return ``content_and_artifact``, so the artifact is the exact payload.
    Framework-level errors (an unknown tool, a schema failure) have no artifact;
    their JSON content is parsed instead, and anything unparseable becomes a
    plain failure so the graph never inspects free text.
    """
    if isinstance(message.artifact, dict):
        return message.artifact
    content = message.content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        return {"ok": False, "error": message.status, "message": content}
    return {"ok": False, "error": message.status, "message": "Unreadable tool result."}


def _text(message: AIMessage) -> str:
    return message.text.strip() if isinstance(message.text, str) else ""


# --------------------------------------------------------------------------- #
# Turn helpers
# --------------------------------------------------------------------------- #


def _system_prompt(deps: TurnDeps) -> str:
    patient = deps.verified_patient
    context = availability_read.scheduling_context_summary(deps.session, deps.clinic)
    prompt = prompts.build_system_prompt(
        clinic=deps.clinic,
        patient=patient,
        clinic_today=context["clinic_today"],
        next_open_slot=context["next_open_slot"],
    )
    return _with_family_option_action(
        prompt, deps.family_option_identity, deps.family_option_request_id
    )


def _with_family_option_action(
    prompt: str,
    option_identity: str | None,
    request_id: str | None = None,
) -> str:
    """Give the model a card identity without exposing it in the transcript."""
    if not option_identity:
        return prompt
    return (
        f"{prompt}\n\n"
        "Internal UI action: the patient clicked a family option card. When "
        "calling hold_family_option, pass this exact opaque option_identity: "
        f"{option_identity}. Also pass request_id {request_id or '(the current request)'}. "
        "Do not repeat either identifier in your reply."
    )


def _refresh_reask(
    deps: TurnDeps,
    *,
    reply: str,
    request_before: dict[str, Any] | None,
    text: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Re-run the stored search so a re-ask answers from live SQLite state."""
    session = deps.session
    conversation = deps.conversation
    verified = deps.verified_patient
    if verified is None:
        return reply, []

    refreshed = scheduling.refresh_scheduling_request(
        session,
        requester=scheduling.Requester(
            patient=verified,
            patient_session=deps.patient_session,
            conversation=conversation,
            clinic=deps.clinic,
        ),
        alternative_day=is_alternative_day_request(text),
        request=scheduling.scheduling_request(session, conversation.id),
        alternative_day_anchor=request_before,
    )
    if refreshed is None:
        return reply, []

    refreshed_cards = _cards_from(refreshed)
    reply = refreshed.get("message") or reply
    for message in reversed(conversations.history(session, conversation)):
        if message.role == "assistant" and not message.tool_calls:
            message.cards = refreshed_cards or None
            message.content = reply
            session.flush()
            break
    return reply, refreshed_cards


def _close_turn(deps: TurnDeps, reply: str, cards: list[dict]) -> None:
    """Persist the assistant text that ends this turn, with its cards."""
    conversations.append_assistant_message(
        deps.session, deps.conversation, content=reply, cards=cards or None
    )
    deps.session.commit()


def _finish_tool_turn(session: Session, conversation: Conversation) -> None:
    """Mark this round's tool-calling assistant message as complete."""
    for message in reversed(conversations.history(session, conversation)):
        if message.role == "assistant" and message.tool_calls:
            if message.finished_at is None:
                message.finished_at = utcnow()
                session.flush()
            return


def _cards_from(result: dict) -> list[dict]:
    raw = result.get("cards") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return []
    return [card for card in raw if isinstance(card, dict) and card.get("type")]


def _ensure_emergency_notice(reply: str, cards: list[dict]) -> tuple[str, bool]:
    """Make the staff notification explicit whenever an emergency alert exists.

    The requirement is a promise the practice makes to the patient, so it is
    enforced in code rather than left to the model: if this turn carries an
    emergency-style handoff card and the prose does not say the front desk has
    been told, that sentence is added.
    """
    has_emergency_alert = any(
        card.get("type") == "handoff"
        and any(
            entry.get("type") == "alert"
            and str(entry.get("kind", "")).lower() in {"emergency", "urgent", "urgent_callback"}
            for entry in card.get("entries") or []
            if isinstance(entry, dict)
        )
        for card in cards
    )
    if not has_emergency_alert:
        return reply, False
    lowered = (reply or "").lower()
    acknowledged = "front desk" in lowered and any(
        word in lowered for word in ("let", "notified", "passed", "logged", "alerted")
    )
    if acknowledged:
        return reply, False
    notice = "I've let the front desk know about the symptoms you described."
    return (f"{notice} {reply}".strip(), True)


def _dedupe_cards(cards: list[dict]) -> list[dict]:
    output: list[dict] = []
    seen: set[str] = set()
    for card in cards:
        identity = _card_identity(card)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(card)
    return output


def _merge_round_cards(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Combine one round's cards with what the turn already showed.

    A fresh availability search supersedes earlier grids (otherwise a thread
    shows two competing sets of times). Informational cards from a later round
    (appointments, confirmations, handoffs) must not erase the grid the patient
    is being asked to choose from: a reschedule search followed by an
    appointment read-back used to hide every replacement time. Merge those,
    and de-duplicate by card identity so the same family option cannot render
    twice.
    """
    offer_types = {"slot", "family_option"}
    if any(card.get("type") in offer_types for card in fresh):
        # A new grid supersedes older grids, but an informational card from an
        # earlier round in the same turn (a staff handoff, a confirmation) still
        # describes something that happened and stays with the turn.
        informational = [card for card in existing if card.get("type") not in offer_types]
        merged = [*fresh, *informational]
    else:
        merged = [*existing, *fresh]
    return _dedupe_cards(merged)


def _card_identity(card: dict) -> str:
    """What makes two cards the same thing, so one is never shown twice."""
    if card.get("proposal_id"):
        return f"proposal:{card['proposal_id']}:{card.get('status')}"
    if card.get("slot_id"):
        return f"slot:{card['slot_id']}"
    if card.get("type") == "family_option":
        return f"family:{card.get('option_id')}"
    return f"{card.get('type')}"


def _rendered_cards(session: Session, conversation: Conversation) -> list[dict]:
    """Cards the turn's own reply already carries.

    Only the reply that *is* the turn can have carried them, and that reply is
    the newest assistant text with no tool calls. A read of the transcript is
    what makes this exact regardless of how the model phrased the turn.
    """
    for message in reversed(conversations.history(session, conversation)):
        if message.role == "assistant" and not message.tool_calls:
            return [card for card in (message.cards or []) if isinstance(card, dict)]
    return []


def _slot_cards_in_thread(
    session: Session, conversation: Conversation, *, turn_floor: int | None = None
) -> list[dict]:
    """The newest slot grid already rendered in this thread, before this turn.

    Used only when the patient asks to see the times again and the model did
    not search afresh: replaying the grid the server built from SQLite is the
    honest answer, and the cards are re-settled by the caller so a grid that is
    already held cannot be offered twice. ``turn_floor`` bounds the search to
    messages the patient had already seen when this turn began; newer rows are
    this turn's own and must not be quoted back at itself.
    """
    for message in reversed(conversations.history(session, conversation)):
        if turn_floor is not None and message.id > turn_floor:
            continue
        if message.role != "assistant" or message.tool_calls:
            continue
        slots = [
            card
            for card in (message.cards or [])
            if isinstance(card, dict) and card.get("type") == "slot" and not card.get("settled")
        ]
        if slots:
            return slots
    return []


def _attach_cards(
    session: Session,
    conversation: Conversation,
    cards: list[dict],
    *,
    turn_floor: int | None = None,
) -> None:
    """Attach cards to this turn's final assistant text (or add a card-only turn).

    ``turn_floor`` is the newest message id when the turn began. Only messages
    after it belong to this turn, so a card can never be attached to an older
    reply the patient saw before the tool ran. Without the floor (older callers)
    the most recent text is used.
    """
    for message in reversed(conversations.history(session, conversation)):
        if turn_floor is not None and message.id <= turn_floor:
            break
        if message.role == "assistant" and not message.tool_calls and not message.cards:
            message.content = message.content or ""
            message.cards = cards
            return
    conversations.append_assistant_message(session, conversation, content="", cards=cards)


def persist_assistant_text(session: Session, conversation: Conversation, text: str) -> None:
    """Store a reply the graph produced rather than the model.

    Public because ``runner`` writes its closed failure paths through it: every
    fallback goes through here, so dash normalisation happens once, in one place,
    for the error and greeting paths alike.
    """
    conversations.append_assistant_message(
        session, conversation, content=reply_format.strip_dashes(text)
    )


def _replace_last_assistant_text(
    session: Session, conversation: Conversation, text: str
) -> None:
    """Rewrite the turn's own assistant message in place.

    The model round stored its text before this turn's style pass runs. Editing
    that row keeps one message per turn, so a refresh shows the cleaned text
    instead of displaying the raw one and then a second copy.
    """
    for message in reversed(conversations.history(session, conversation)):
        if message.role == "assistant" and not message.tool_calls:
            message.content = text
            session.flush()
            return
    conversations.append_assistant_message(session, conversation, content=text)


def _sync_patient(deps: TurnDeps) -> None:
    patient_session = deps.patient_session
    if patient_session.verified_patient_id:
        conversations.sync_verified_patient(
            deps.conversation, patient_session.verified_patient_id
        )
        deps.session.commit()


def visible_thread_messages(
    session: Session, clinic: ClinicContext, conversation: Conversation
) -> list[dict]:
    """The thread as the browser should see it right now.

    Proposal cards are reconciled against live rows and grids this request has
    already settled, so a refreshed thread cannot show a time that is held,
    booked or expired. Shared with ``runner``'s fallback responses for the same
    reason: a failure must still return a card state that matches the database.
    """
    return [
        {
            **entry,
            "cards": scheduling.reconcile_proposal_cards(
                session,
                scheduling.settle_superseded_grids(
                    session, clinic, conversation.id, entry.get("cards")
                ),
            ),
        }
        for entry in conversations.visible_messages(conversations.history(session, conversation))
    ]


# Compiled after every helper exists: the tool node is wired with the error and
# interception callbacks defined above.
TURN_GRAPH = build_graph()


__all__ = [
    "MAX_TOOL_ROUNDS",
    "TURN_GRAPH",
    "AgentState",
    "_attach_cards",
    "visible_thread_messages",
    "_with_family_option_action",
    "build_graph",
]
