"""Conversation persistence and provider-message reconstruction.

Every turn is stored so the thread can be replayed to the model exactly as it
happened, including tool calls and their matching results.

Replay shape (OpenAI-compatible):
    {"role": "user", "content": ...}
    {"role": "assistant", "content": ..., "tool_calls": [...]}
    {"role": "tool", "tool_call_id": ..., "content": "<json>"}

An assistant turn that only issued tool calls is replayed with
``content: ""`` and its ``tool_calls`` array, immediately followed by one
``tool`` message per call, in order. That pairing is what the provider requires.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Conversation, ConversationStatus, Message, PatientSession, utcnow

TITLE_MAX_LENGTH = 64
_INTERNAL_FAMILY_ACTION = re.compile(
    r"\s*The clicked card's option_identity is [^;\n]+;\s*"
    r"pass that exact option_identity to hold_family_option\.?",
    re.IGNORECASE,
)


def new_public_id() -> str:
    return f"conv_{secrets.token_urlsafe(18)}"


def create_conversation(
    session: Session, *, patient_session: PatientSession, generation: str
) -> Conversation:
    now = utcnow()
    conversation = Conversation(
        public_id=new_public_id(),
        patient_session_id=patient_session.id,
        status=ConversationStatus.ACTIVE.value,
        caller_phone=None,
        patient_id=patient_session.verified_patient_id,
        reset_generation=generation,
        started_at=now,
        last_message_at=now,
        title=None,
    )
    session.add(conversation)
    session.flush()
    return conversation


def get_for_session(
    session: Session, *, patient_session: PatientSession, public_id: str
) -> Conversation | None:
    """Load a conversation **only** if it belongs to this session."""
    return session.scalar(
        select(Conversation).where(
            Conversation.public_id == public_id,
            Conversation.patient_session_id == patient_session.id,
        )
    )


def get_by_public_id(session: Session, public_id: str) -> Conversation | None:
    """Load a conversation without session scoping.

    Only for server-internal decisions (``resume_for_session`` below); never
    expose this through a route that takes a conversation id from a client.
    """
    return session.scalar(select(Conversation).where(Conversation.public_id == public_id))


def newest_for_patient(
    session: Session, *, patient_id: int, generation: str
) -> Conversation | None:
    rows = session.scalars(
        select(Conversation)
        .where(
            Conversation.patient_id == patient_id,
            Conversation.reset_generation == generation,
        )
        .order_by(
            func.coalesce(Conversation.last_message_at, Conversation.started_at).desc(),
            Conversation.id.desc(),
        )
        .limit(1)
    ).all()
    return rows[0] if rows else None


def resume_for_session(
    session: Session, *, patient_session: PatientSession, public_id: str
) -> Conversation | None:
    """Hand an existing thread to a *newly issued* session for the same patient.

    Why this exists
    ---------------
    The browser normally keeps its session token across a refresh, so a restore
    is a plain ``get_for_session``. A new, *unverified* session has nothing to
    resume: adopting a thread would mean either trusting a thread id as a
    credential or guessing a patient, and both break the session model. Only a
    session that is already bound to a patient (verified again under the new
    session) may claim that patient's own thread.

    A conversation is therefore re-owned, never shared: its
    ``patient_session_id`` moves to the current session so every later lookup
    stays scoped to exactly one session. Expired handles and threads from an
    earlier reset generation are refused, so a demo reset can never be undone
    through this path.
    """
    if patient_session.verified_patient_id is None:
        return None
    conversation = get_by_public_id(session, public_id)
    if conversation is None:
        return None
    if conversation.reset_generation != patient_session.reset_generation:
        return None
    if conversation.patient_id != patient_session.verified_patient_id:
        return None
    if conversation.patient_session_id == patient_session.id:
        return conversation
    conversation.patient_session_id = patient_session.id
    if conversation.status == ConversationStatus.CLOSED.value:
        # The patient came back to this thread: it is the session's open one
        # again. The front desk keeps its history either way.
        conversation.status = ConversationStatus.ACTIVE.value
        conversation.ended_at = None
    session.flush()
    return conversation


def list_for_session(
    session: Session, patient_session: PatientSession, limit: int | None = 30
) -> list[Conversation]:
    """This session's threads, newest activity first. ``None`` means all."""
    statement = (
        select(Conversation)
        .where(Conversation.patient_session_id == patient_session.id)
        .order_by(
            func.coalesce(Conversation.last_message_at, Conversation.started_at).desc(),
            Conversation.id.desc(),
        )
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement).all())


def _next_sequence(session: Session, conversation_id: int) -> int:
    current = session.scalar(
        select(func.max(Message.sequence)).where(Message.conversation_id == conversation_id)
    )
    return int(current or 0) + 1


def append_patient_message(session: Session, conversation: Conversation, text: str) -> Message:
    now = utcnow()
    prior = count_patient_messages(session, conversation)
    message = Message(
        conversation_id=conversation.id,
        role="patient",
        content=text.strip(),
        sequence=_next_sequence(session, conversation.id),
        created_at=now,
        finished_at=now,
    )
    session.add(message)
    conversation.last_message_at = now
    # The title should describe the thread, not the opening "hi". Only the first
    # substantive message (>= 3 words) sets it.
    if conversation.title is None and prior == 0 and len(text.split()) >= 3:
        conversation.title = _title_from(text)
    session.flush()
    return message


def count_patient_messages(session: Session, conversation: Conversation) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation.id, Message.role == "patient")
        )
        or 0
    )


def backfill_title(session: Session, conversation: Conversation) -> None:
    """Give a thread a title later if its first message was too short."""
    if conversation.title:
        return
    first = session.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.role == "patient")
        .order_by(Message.sequence)
    )
    if first and len(first.content.split()) >= 3:
        conversation.title = _title_from(first.content)
        session.flush()


def _title_from(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= TITLE_MAX_LENGTH:
        return cleaned
    return cleaned[: TITLE_MAX_LENGTH - 1].rstrip() + "…"


def append_assistant_message(
    session: Session,
    conversation: Conversation,
    *,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    cards: list[dict[str, Any]] | None = None,
) -> Message:
    now = utcnow()
    message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=content,
        sequence=_next_sequence(session, conversation.id),
        created_at=now,
        tool_calls=tool_calls or None,
        finished_at=now if not tool_calls else None,
        cards=cards or None,
    )
    session.add(message)
    conversation.last_message_at = now
    session.flush()
    return message


def append_tool_message(
    session: Session,
    conversation: Conversation,
    *,
    tool_call_id: str,
    tool_name: str,
    payload: dict[str, Any],
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        role="tool",
        content=json.dumps(payload, default=str),
        sequence=_next_sequence(session, conversation.id),
        created_at=utcnow(),
        tool_call_id=tool_call_id,
        tool_name=tool_name,
    )
    session.add(message)
    session.flush()
    return message


def history(session: Session, conversation: Conversation) -> list[Message]:
    return list(
        session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.sequence)
        ).all()
    )


def last_message_id(session: Session, conversation: Conversation) -> int:
    """Id of the newest stored message, i.e. the floor for the current turn."""
    return int(
        session.scalar(
            select(func.max(Message.id)).where(Message.conversation_id == conversation.id)
        )
        or 0
    )


def to_provider_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert stored rows into the provider's message format.

    - ``system`` and ``staff`` rows are skipped: our system prompt is rebuilt per
      turn so it always carries current session state.
    - ``tool`` rows are attached to the assistant turn that requested them.
    - Orphaned tool rows (assistant turn missing) are dropped; sending them would
      be rejected by the provider.
    """
    pending: dict[str, list[Message]] = {}
    for message in messages:
        if message.role == "tool" and message.tool_call_id:
            pending.setdefault(message.tool_call_id, []).append(message)

    output: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "patient":
            output.append({"role": "user", "content": message.content})
        elif message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
            calls = message.tool_calls or []
            if calls:
                entry["tool_calls"] = calls
                output.append(entry)
                for call in calls:
                    call_id = call.get("id")
                    followers = pending.pop(call_id, []) if call_id else []
                    if followers:
                        output.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": followers[0].content,
                            }
                        )
                    else:
                        output.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": json.dumps({"ok": False, "error": "missing_result"}),
                            }
                        )
                continue
            output.append(entry)
        # tool/system/staff rows are handled above or intentionally omitted
    return output


def public_message(message: Message) -> dict[str, Any]:
    """Client-safe projection. Never includes tool payloads or arguments."""
    content = message.content
    if message.role == "patient":
        # Older card clicks put the opaque identity in the patient message.
        # Keep the stored/provider history intact, but never replay that
        # implementation detail to the browser.
        content = _INTERNAL_FAMILY_ACTION.sub("", content).strip()
    payload = {
        "id": message.id,
        "role": message.role,
        "content": content,
        "sequence": message.sequence,
        "created_at": message.created_at.isoformat(),
    }
    if message.cards:
        payload["cards"] = message.cards
    return payload


def visible_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """The transcript a patient should see: their messages and assistant output.

    An assistant turn that produced only cards (no prose) is still visible, so a
    clicked slot's proposal survives a refresh even if the model said nothing.
    """
    visible: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "patient":
            visible.append(public_message(message))
        elif message.role == "assistant" and (message.content or message.cards):
            visible.append(public_message(message))
    return visible


def conversation_summary(conversation: Conversation, messages: list[Message]) -> dict[str, Any]:
    visible = visible_messages(messages)
    last_at: datetime | None = conversation.last_message_at or conversation.started_at
    preview = ""
    for entry in reversed(visible):
        if entry["role"] == "assistant":
            preview = entry["content"]
            break
    return {
        "conversation_id": conversation.public_id,
        "title": conversation.title or "New conversation",
        "preview": preview[:120],
        "started_at": conversation.started_at.isoformat(),
        "last_message_at": last_at.isoformat() if last_at else None,
        "status": conversation.status,
        "message_count": sum(1 for entry in visible if entry["role"] == "patient"),
    }


def sync_verified_patient(conversation: Conversation, patient_id: int | None) -> None:
    if patient_id and conversation.patient_id != patient_id:
        conversation.patient_id = patient_id
