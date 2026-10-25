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
REASK_TIMES = re.compile(
    r"\b(?:any|other|more|another|different|else)\b[^?]{0,20}\b(?:slots?|times?|openings?)\b"
    r"|\bcurrent availability\b|\bwhat(?:'s| is) available\b|\banything (?:open|available|else)\b"
    r"|\b(?:show|see|check) (?:me )?(?:the )?(?:times|slots|availability)(?: again)?\b"
    r"|\b(?:refresh|refreshed)\b[^?]{0,20}\b(?:availability|times|slots)\b"
    r"|\bwhat (?:times|slots) do you have\b|\bshow me again\b",
    re.IGNORECASE,
)


def is_time_reask(text: str) -> bool:
    """Whether the patient asked to be shown times again, rather than a new day."""
    return bool(REASK_TIMES.search(text or ""))


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


def run_turn(
    session: Session,
    *,
    conversation: Conversation,
    patient_session: PatientSession,
    patient_text: str,
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

    history = conversations.history(session, conversation)
    provider_messages: list[dict] = [
        {"role": "system", "content": _system_prompt(session, patient_session)},
        *conversations.to_provider_messages(history),
    ]

    context = ToolContext(
        session=session, patient_session=patient_session, conversation_id=conversation.id
    )
    tools = openai_tool_schemas()
    client = client or DeepSeekClient()

    try:
        reply, cards = _run_loop(
            session,
            conversation=conversation,
            patient_session=patient_session,
            provider_messages=provider_messages,
            tools=tools,
            context=context,
            client=client,
        )
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
 ) -> tuple[str, list[dict]]:
    """Run the tool loop, returning the final prose and the cards to render.

    Cards come from tool results: each scheduling tool returns ``cards`` it built
    from real database state. Only the *latest* round's cards survive, because
    they supersede earlier searches in the same turn (the patient should not see
    a stale slot grid next to the proposal that replaced it).
    """
    cards: list[dict] = []
    for round_index in range(MAX_TOOL_ROUNDS):
        turn = client.chat(messages=provider_messages, tools=tools)

        if not turn.tool_calls:
            reply = turn.content.strip() or ""
            conversations.append_assistant_message(
                session, conversation, content=reply, cards=cards or None
            )
            session.commit()
            return reply, cards

        calls = turn.tool_calls[:MAX_TOOL_CALLS_PER_ROUND]
        pending = _provider_tool_calls(turn)[: len(calls)]

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
            result = _execute(session, context, call)
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
            cards = round_cards

        stored.finished_at = utcnow()
        # Verification can happen inside a tool call, so the patient binding and
        # the system prompt are both refreshed before the next model round.
        _sync_patient(session, conversation, patient_session)
        _refresh_system_prompt(provider_messages, session, patient_session)

        if round_index == MAX_TOOL_ROUNDS - 1:
            logger.info("Tool round limit reached for conversation %s", conversation.public_id)
            return (
                "I've gathered what I need for now. The front desk will follow up to "
                "confirm the next step.",
                cards,
            )

    return "", cards


def _cards_from(result: dict) -> list[dict]:
    raw = result.get("cards") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return []
    return [card for card in raw if isinstance(card, dict) and card.get("type")]


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
    already held cannot be offered twice.
    """
    for message in reversed(conversations.history(session, conversation)):
        if turn_floor is not None and message.id <= turn_floor:
            break
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


def _execute(session: Session, context: ToolContext, call: ToolCall) -> dict:
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
    return conversations.visible_messages(conversations.history(session, conversation))


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
