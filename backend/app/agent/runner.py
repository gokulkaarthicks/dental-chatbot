"""The conversation runner: one patient message in, one assistant reply out.

Flow (matches the phase spec exactly):

1. authenticate the session (caller has already done this),
2. persist the incoming message,
3. load the full thread in order,
4. build the system prompt + provider messages,
5. call DeepSeek with tools,
6. execute any requested tools through validated services,
7. persist the assistant tool-call turn and each tool result,
8. call DeepSeek again with the results,
9. persist the final assistant text,
10. return it.

Bounds
------
* ``MAX_TOOL_ROUNDS`` limits model round-trips per patient message.
* ``MAX_TOOL_CALLS_PER_ROUND`` limits fan-out from a single model turn.
* Each round is one HTTP call; the client applies its own timeout and retries.
* Any failure before the final text is persisted as a short, friendly assistant
  message so history never contains a dangling tool call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from app.agent import prompts, reply_format
from app.agent.tools import ToolContext, execute_tool, openai_tool_schemas
from app.ai.deepseek import AssistantTurn, DeepSeekClient, DeepSeekError, ToolCall
from app.models import Conversation, PatientSession, utcnow
from app.observability import log_event
from app.services import (
    availability,
    conversations,
    patient_sessions,
    patients,
    practice,
    scheduling,
)

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS_PER_ROUND = 4
MAX_PATIENT_MESSAGE_CHARS = 2000

FALLBACK_REPLY = "Sorry about that. I hit a snag on my side. Could you send that again?"

# A patient asking to see the times they were already shown. The reply alone
# cannot answer this: the grid lives in an earlier turn, and the turn that
# answers "any other slots?" must carry its own copy or the thread points at
# cards the patient has to scroll back to. The search itself is unchanged.
# Spellings drift ("abvaialbility" is a real one from a demo), so each word is
# matched loosely rather than the whole phrase.
_FUZZY_AVAILABILITY = r"[a-z]*?i[a-z]*?i[a-z]*?y\b"
REASK_TIMES = re.compile(
    r"\b(?:any|other|more|another|different|else)\b[^?]{0,20}\b(?:slots?|times?|openings?)\b"
    rf"|\b(?:current|other|more|any) {_FUZZY_AVAILABILITY}\b"
    r"|\bwhat(?:'s| is) available\b|\banything (?:open|available|else)\b"
    r"|\b(?:show|see|check) (?:me )?(?:the )?(?:times|slots|availability)(?: again)?\b"
    r"|\b(?:refresh|refreshed)\b[^?]{0,20}\b(?:availability|times|slots)\b"
    r"|\b(?:another|different) (?:day|date)\b"
    r"|\bwhat (?:times|slots) do you have\b|\bshow me again\b",
    re.IGNORECASE,
)


def is_time_reask(text: str) -> bool:
    """Whether the patient asked to be shown times again, rather than a new day."""
    value = text or ""
    if REASK_TIMES.search(value):
        return True
    return any(
        len(word) >= 7 and SequenceMatcher(None, word.lower(), "availability").ratio() >= 0.72
        for word in re.findall(r"[a-z]+", value.lower())
    )


def is_alternative_day_request(text: str) -> bool:
    return bool(re.search(r"\b(?:another|different) (?:day|date)\b", text or "", re.IGNORECASE))


_AVAILABILITY_REASK_TOOLS = {
    "get_booking_context",
    "get_session_context",
    "register_child_for_verified_parent",
    "search_availability",
    "search_family_availability",
}


def _availability_reask_tools(tools: list[dict], request: dict | None) -> list[dict]:
    """Keep a fresh-availability turn from selecting a historical card.

    A natural-language re-ask is not a card selection. The model may extract
    new people or preferences, but it must not dispatch a hold or confirm tool
    based on an older option mentioned in the transcript. Child registration
    remains available because a family re-ask may name a child who is not linked
    yet.
    """
    if not request:
        return tools
    allowed = set(_AVAILABILITY_REASK_TOOLS)
    participants = request.get("participants") or []
    if isinstance(participants, list) and len(participants) > 1:
        # A family request must not be collapsed into a solo search just
        # because the model omitted one of the people on a follow-up turn.
        allowed.discard("search_availability")
    return [
        tool
        for tool in tools
        if (tool.get("function") or {}).get("name") in allowed
    ]


@dataclass
class TurnResult:
    reply: str
    messages: list[dict] = field(default_factory=list)
    degraded: bool = False
    cards: list[dict] = field(default_factory=list)


def _system_prompt(session: Session, patient_session: PatientSession) -> str:
    patient = patient_sessions.verified_patient(session, patient_session)
    context = availability.scheduling_context_summary(session)
    return prompts.build_system_prompt(
        patient=patient,
        clinic_today=context["clinic_today"],
        next_open_slot=context["next_open_slot"],
    )


def _provider_tool_calls(turn: AssistantTurn) -> list[dict]:
    return [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }
        for call in turn.tool_calls
    ]


def _add_family_option_action(
    provider_messages: list[dict], option_identity: str | None, request_id: str | None = None
) -> None:
    """Give the model a card identity without exposing it in the transcript."""
    if not option_identity or not provider_messages:
        return
    system = provider_messages[0]
    if system.get("role") != "system":
        return
    system["content"] = (
        f"{system.get('content', '')}\n\n"
        "Internal UI action: the patient clicked a family option card. When "
        "calling hold_family_option, pass this exact opaque option_identity: "
        f"{option_identity}. Also pass request_id {request_id or '(the current request)'}. "
        "Do not repeat either identifier in your reply."
    )


def run_turn(
    session: Session,
    *,
    conversation: Conversation,
    patient_session: PatientSession,
    patient_text: str,
    family_option_identity: str | None = None,
    family_option_request_id: str | None = None,
    client: DeepSeekClient | None = None,
) -> TurnResult:
    text = (patient_text or "").strip()
    if not text:
        return TurnResult(reply="I didn't catch that. Could you type it again?", degraded=True)
    if len(text) > MAX_PATIENT_MESSAGE_CHARS:
        text = text[:MAX_PATIENT_MESSAGE_CHARS]

    if conversation.patient_session_id != patient_session.id:
        # Defence in depth: routes already enforce this.
        raise PermissionError("conversation does not belong to this session")

    history = conversations.history(session, conversation)
    is_first_message = conversations.visible_messages(history) == []
    patient_sessions.touch(patient_session)

    conversations.append_patient_message(session, conversation, text)
    conversations.backfill_title(session, conversation)
    session.commit()

    # Everything already in the thread is earlier context. This turn's cards
    # must attach to this turn's own reply, never to a message the patient
    # wrote before the search ran: that would put an offer above the answer it
    # belongs to, and repeat it further down.
    turn_floor = conversations.last_message_id(session, conversation)
    reask = is_time_reask(text)
    request_before_turn = scheduling.scheduling_request(session, conversation.id)

    history = conversations.history(session, conversation)
    provider_messages: list[dict] = [
        {"role": "system", "content": _system_prompt(session, patient_session)},
        *conversations.to_provider_messages(history),
    ]
    _add_family_option_action(provider_messages, family_option_identity, family_option_request_id)

    context = ToolContext(
        session=session, patient_session=patient_session, conversation_id=conversation.id
    )
    tools = openai_tool_schemas()
    if reask and family_option_identity is None:
        tools = _availability_reask_tools(tools, request_before_turn)
    client = client or DeepSeekClient()
    availability_reask = reask and family_option_identity is None

    try:
        reply, cards = _run_loop(
            session,
            conversation=conversation,
            patient_session=patient_session,
            provider_messages=provider_messages,
            tools=tools,
            context=context,
            client=client,
            family_option_identity=family_option_identity,
            family_option_request_id=family_option_request_id,
            availability_reask=availability_reask,
        )
        if reask:
            verified = patient_sessions.verified_patient(session, patient_session)
            if verified is not None:
                refreshed = scheduling.refresh_scheduling_request(
                    session,
                    requester=scheduling.Requester(
                        patient=verified,
                        patient_session=patient_session,
                        conversation=conversation,
                    ),
                    alternative_day=is_alternative_day_request(text),
                    request=scheduling.scheduling_request(session, conversation.id),
                    alternative_day_anchor=request_before_turn,
                )
                if refreshed is not None:
                    refreshed_cards = _cards_from(refreshed)
                    reply = refreshed.get("message") or reply
                    cards = refreshed_cards
                    for message in reversed(conversations.history(session, conversation)):
                        if message.role == "assistant" and not message.tool_calls:
                            message.cards = refreshed_cards or None
                            message.content = reply
                            session.flush()
                            break
        reply, noticed = _ensure_emergency_notice(reply, cards)
        if noticed:
            _replace_last_assistant_text(session, conversation, reply)
            session.commit()
    except DeepSeekError as exc:
        # Type only: provider messages never enter the log.
        logger.warning("DeepSeek failure during turn: %s", type(exc).__name__)
        log_event(
            logger,
            "deepseek.failed",
            failure=type(exc).__name__,
            conversation=conversation.public_id,
            level=logging.WARNING,
        )
        reply = exc.user_message
        _persist_safe_assistant_text(session, conversation, reply)
        session.commit()
        return TurnResult(
            reply=reply,
            messages=_visible(session, conversation),
            degraded=True,
        )
    except Exception:  # noqa: BLE001 - never leave a half-written turn
        logger.exception("Unexpected failure while running conversation turn")
        session.rollback()
        reply = FALLBACK_REPLY
        _persist_safe_assistant_text(session, conversation, reply)
        session.commit()
        return TurnResult(
            reply=reply,
            messages=_visible(session, conversation),
            degraded=True,
        )

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
    # "Cancelled"). Those cards already sit on the tool turn the loop stored, so
    # they are not attached a second time: doing so is what showed the same
    # cancelled box twice, with an empty message between them.
    carried: set[str] = set()
    carried_row = _cards_on_latest_tool_turn(session, conversation)
    for card in carried_row:
        carried.add(_card_identity(card))
    to_attach = cards
    if cards and carried:
        to_attach = [card for card in cards if _card_identity(card) not in carried]
    if to_attach:
        cards = scheduling.with_live_proposals(session, conversation.id, to_attach)
        # A grid this request has already held or booked is context, not an
        # offer, however it was produced.
        cards = scheduling.settle_superseded_grids(session, conversation.id, cards)
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
            replayed = scheduling.with_live_proposals(
                session, conversation.id, earlier
            )
            cards = scheduling.settle_superseded_grids(
                session, conversation.id, replayed
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
        _persist_safe_assistant_text(session, conversation, reply)
        session.commit()
    elif cards:
        # Enforce the "cards, not prose" rule regardless of what the model wrote:
        # echoes of times are stripped and an empty intro becomes a card-derived
        # one. The cards were attached to the turn's text above, so a refresh
        # replays them in place.
        formatted, reformatted = reply_format.format_assistant_text(
            reply, cards, refreshed=reask
        )
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
        # style rule is applied here. The text was already stored by the tool
        # loop; rewrite that same row rather than appending a second message.
        clean = reply_format.strip_dashes(reply)
        if clean != reply:
            reply = clean
            _replace_last_assistant_text(session, conversation, clean)
            session.commit()

    if is_first_message:
        logger.info("First patient message stored for conversation %s", conversation.public_id)

    return TurnResult(reply=reply, messages=_visible(session, conversation), cards=cards)


def _run_loop(
    session: Session,
    *,
    conversation: Conversation,
    patient_session: PatientSession,
    provider_messages: list[dict],
    tools: list[dict],
    context: ToolContext,
    client: DeepSeekClient,
    family_option_identity: str | None = None,
    family_option_request_id: str | None = None,
    availability_reask: bool = False,
) -> tuple[str, list[dict]]:
    """Run the tool loop, returning the final prose and the cards to render.

    Cards come from tool results: each scheduling tool returns ``cards`` it built
    from real database state. Only the *latest* round's cards survive, because
    they supersede earlier searches in the same turn (the patient should not see
    a stale slot grid next to the proposal that replaced it).
    """
    cards: list[dict] = []
    search_dispatched = False
    availability_exhausted = False
    last_tool_message = ""
    for round_index in range(MAX_TOOL_ROUNDS):
        tools_this_round: list[dict] = (
            []
            if (availability_reask and search_dispatched) or availability_exhausted
            else tools
        )
        turn = client.chat(
            messages=provider_messages,
            tools=tools_this_round,
        )

        # After tools are withdrawn (empty search, re-ask already searched), the
        # provider sometimes still emits tool_calls or DSML markup. Executing
        # those and replaying synthetic ids on the next round yields HTTP 400.
        if turn.tool_calls and not tools_this_round:
            reply = (turn.content or "").strip() or last_tool_message or (
                "I checked what's open and don't have a complete set for everyone "
                "on those days. Would you like a different day, or is a different "
                "time of day okay?"
                if availability_exhausted
                else ""
            )
            conversations.append_assistant_message(
                session, conversation, content=reply, cards=cards or None
            )
            session.commit()
            return reply, cards

        if not turn.tool_calls:
            reply = turn.content.strip() or last_tool_message or ""
            conversations.append_assistant_message(
                session, conversation, content=reply, cards=cards or None
            )
            session.commit()
            return reply, cards

        calls = turn.tool_calls[:MAX_TOOL_CALLS_PER_ROUND]
        pending = _provider_tool_calls(turn)[: len(calls)]
        dsml_sourced = any(call.id.startswith("dsml_") for call in calls)

        stored = conversations.append_assistant_message(
            session,
            conversation,
            content=turn.content,
            tool_calls=pending,
        )
        session.commit()

        provider_messages.append(
            {"role": "assistant", "content": turn.content or "", "tool_calls": pending}
        )

        round_cards: list[dict] = []
        for call in calls:
            if call.name in {"search_availability", "search_family_availability"}:
                search_dispatched = True
            result = _execute(
                session,
                context,
                call,
                family_option_identity=family_option_identity,
                family_option_request_id=family_option_request_id,
            )
            if isinstance(result, dict) and result.get("message"):
                last_tool_message = str(result["message"])
            if (
                call.name
                in {"search_availability", "search_family_availability", "create_staff_request"}
                and isinstance(result, dict)
                and result.get("availability_checked") is True
                and not result.get("cards")
            ):
                # A complete empty search is terminal for this model turn. Do
                # not let the provider turn its own no-result response into a
                # repeated search or callback loop.
                availability_exhausted = True
            round_cards.extend(_cards_from(result))
            conversations.append_tool_message(
                session,
                conversation,
                tool_call_id=call.id,
                tool_name=call.name,
                payload=result,
            )
            session.commit()
            provider_messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": _json(result)}
            )
        if round_cards:
            cards = _merge_round_cards(cards, round_cards)

        stored.finished_at = utcnow()
        # Verification can happen inside a tool call, so the patient binding and
        # the system prompt are both refreshed before the next model round.
        _sync_patient(session, conversation, patient_session)
        _refresh_system_prompt(provider_messages, session, patient_session)
        _add_family_option_action(
            provider_messages, family_option_identity, family_option_request_id
        )

        # Synthetic DSML tool_call ids are not accepted on the next provider
        # round. Stop here and speak from the tool result instead of risking a
        # 400 that surfaces as "Something went wrong on my end."
        if dsml_sourced:
            reply = last_tool_message or (
                "I've got that noted. What would you like to do next?"
            )
            conversations.append_assistant_message(
                session, conversation, content=reply, cards=cards or None
            )
            session.commit()
            return reply, cards

        if round_index == MAX_TOOL_ROUNDS - 1:
            logger.info("Tool round limit reached for conversation %s", conversation.public_id)
            return (
                last_tool_message
                or (
                    "I've gathered what I need for now. The front desk will follow up to "
                    "confirm the next step."
                ),
                cards,
            )

    return "", cards


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
    output: list[dict] = []
    seen: set[str] = set()
    for card in merged:
        identity = _card_identity(card)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(card)
    return output


def _card_identity(card: dict) -> str:
    """What makes two cards the same thing, so one is never shown twice."""
    if card.get("proposal_id"):
        return f"proposal:{card['proposal_id']}:{card.get('status')}"
    if card.get("slot_id"):
        return f"slot:{card['slot_id']}"
    if card.get("type") == "family_option":
        return f"family:{card.get('option_id')}"
    return f"{card.get('type')}"


def _cards_on_latest_tool_turn(session: Session, conversation: Conversation) -> list[dict]:
    """Cards this turn has already rendered on a prose reply.

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
            if isinstance(card, dict)
            and card.get("type") == "slot"
            and not card.get("settled")
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
        if (
            message.role == "assistant"
            and not message.tool_calls
            and not message.cards
        ):
            message.content = message.content or ""
            message.cards = cards
            return
    conversations.append_assistant_message(session, conversation, content="", cards=cards)


def _execute(
    session: Session,
    context: ToolContext,
    call: ToolCall,
    *,
    family_option_identity: str | None = None,
    family_option_request_id: str | None = None,
) -> dict:
    if call.name == "hold_family_option" and family_option_identity:
        try:
            arguments = json.loads(call.arguments) if call.arguments.strip() else {}
        except (TypeError, ValueError):
            arguments = {}
        if isinstance(arguments, dict):
            # The browser action is the authority for which family option was
            # clicked. The model supplies the family members and index, but it
            # cannot accidentally select a different historical option.
            arguments["option_identity"] = family_option_identity
            arguments["request_id"] = family_option_request_id
            call = ToolCall(
                id=call.id,
                name=call.name,
                arguments=json.dumps(arguments),
            )
    try:
        result = execute_tool(context, call.name, call.arguments)
    except Exception:  # noqa: BLE001 - execute_tool already guards; belt and braces
        logger.exception("Tool %s failed outside its guard", call.name)
        result = {"ok": False, "error": "tool_failed"}
    session.flush()
    # Outcome only: the tool name and whether it succeeded. Arguments are never
    # logged because they carry patient details.
    log_event(
        logger,
        "tool.executed",
        tool=call.name,
        ok=bool(result.get("ok")) if isinstance(result, dict) else False,
        error=(result.get("error") if isinstance(result, dict) else None),
        cards=len(result.get("cards") or []) if isinstance(result, dict) else 0,
    )
    return result


def _refresh_system_prompt(
    provider_messages: list[dict], session: Session, patient_session: PatientSession
) -> None:
    """Session state changes mid-turn (verification), so refresh the prompt."""
    if provider_messages and provider_messages[0].get("role") == "system":
        provider_messages[0]["content"] = _system_prompt(session, patient_session)


def _json(payload: dict) -> str:
    return json.dumps(payload, default=str)


def _persist_safe_assistant_text(session: Session, conversation: Conversation, text: str) -> None:
    # Every fallback goes through here, so dash normalisation happens once, in
    # one place, for the error and greeting paths alike.
    conversations.append_assistant_message(
        session, conversation, content=reply_format.strip_dashes(text)
    )


def _replace_last_assistant_text(
    session: Session, conversation: Conversation, text: str
) -> None:
    """Rewrite the turn's own assistant message in place.

    The tool loop stored the model's text before this turn's style pass runs.
    Editing that row keeps one message per turn, so a refresh shows the cleaned
    text instead of displaying the raw one and then a second copy.
    """
    history = conversations.history(session, conversation)
    for message in reversed(history):
        if message.role == "assistant" and not message.tool_calls:
            message.content = text
            session.flush()
            return
    conversations.append_assistant_message(session, conversation, content=text)


def _sync_patient(
    session: Session, conversation: Conversation, patient_session: PatientSession
) -> None:
    if patient_session.verified_patient_id:
        conversations.sync_verified_patient(conversation, patient_session.verified_patient_id)
        session.commit()


def _visible(session: Session, conversation: Conversation) -> list[dict]:
    return [
        {
            **entry,
            "cards": scheduling.reconcile_proposal_cards(
                session,
                scheduling.settle_superseded_grids(
                    session, conversation.id, entry.get("cards")
                ),
            ),
        }
        for entry in conversations.visible_messages(conversations.history(session, conversation))
    ]


def greeting_reply(session: Session, patient_session: PatientSession) -> str:
    """Opening line for a new thread.

    Deterministic and model-free so the first paint never waits on the network.
    A returning, already-verified patient gets their name used greeting.
    """
    patient = patient_sessions.verified_patient(session, patient_session)
    if patient is not None:
        first = patients.first_name(patient.full_name)
        greeting = f"Hi again, {first}. " if first else "Welcome back. "
        return (
            f"{greeting}What can I help you with? I can book an appointment, "
            "or answer a question about the clinic."
        )
    return reply_format.strip_dashes(prompts.build_greeting())


def quick_suggestions(session: Session, patient_session: PatientSession) -> list[str]:
    """Starter chips for an empty thread. Hidden by the UI after the first message."""
    patient = patient_sessions.verified_patient(session, patient_session)
    if patient is None:
        return [
            "I need to book a cleaning",
            "My tooth hurts. Can I get in today?",
            "Do you take my insurance?",
            "Where are you located?",
        ]
    first = patients.first_name(patient.full_name)
    if not first:
        return [
            "What appointments do I have?",
            "Book a cleaning",
            "I need to change my appointment",
            "What insurance do you accept?",
        ]
    return [
        f"What appointments do I have, {first}?",
        "Book a cleaning",
        "I need to change my appointment",
        "What insurance do you accept?",
    ]


def practice_context_for_clinic_page(session: Session) -> dict:
    return practice.practice_summary(session)


__all__ = [
    "MAX_TOOL_ROUNDS",
    "TurnResult",
    "greeting_reply",
    "practice_context_for_clinic_page",
    "quick_suggestions",
    "run_turn",
]
