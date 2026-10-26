"""Public entry point for one patient turn: one message in, one reply out.

The orchestration lives in :mod:`app.agent.graph`. This module owns only what
the HTTP layer needs: input validation, dependency assembly, one turn at a time
per conversation, and the failure paths that must never leave a half-written
turn in the transcript.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from sqlalchemy.orm import Session

from app.agent import prompts, reply_format
from app.agent.context import TurnDeps
from app.agent.graph import TURN_GRAPH, persist_assistant_text, visible_thread_messages
from app.ai import AgentProviderError, build_chat_model, provider_user_message
from app.models import Conversation, PatientSession
from app.observability import log_event
from app.services import patient_sessions, patients
from app.tenancy import ClinicContext

logger = logging.getLogger(__name__)

MAX_PATIENT_MESSAGE_CHARS = 2000

FALLBACK_REPLY = "Sorry about that. I hit a snag on my side. Could you send that again?"
EMPTY_REPLY = "I didn't catch that. Could you type it again?"


class TurnInProgress(RuntimeError):
    """Another turn is already running for this conversation.

    The route answers it with 409, so the browser keeps its own reply instead of
    racing a second one against the same thread.
    """


# Conversations with a turn in flight right now.
#
# Two requests over one thread are not merely wasteful: each one holds the same
# SQLAlchemy session and the same SQLite rows, so a double tap or a client retry
# can hold one choice twice, interleave the transcript, and have two replies
# competing for the card slot the other just wrote. This guard is in-process
# because the API runs as a single worker (see the Dockerfile and
# docker-compose). Behind more than one worker the same rule needs a lease row on
# ``conversations``, and nothing here would have to change but where the entry is
# stored.
_busy_conversations: set[int] = set()
_busy_guard = threading.Lock()


@contextmanager
def _serialized_turn(conversation_id: int) -> Iterator[None]:
    with _busy_guard:
        if conversation_id in _busy_conversations:
            raise TurnInProgress(f"conversation {conversation_id} has a turn in progress")
        _busy_conversations.add(conversation_id)
    try:
        yield
    finally:
        with _busy_guard:
            _busy_conversations.discard(conversation_id)


@dataclass
class TurnResult:
    reply: str
    messages: list[dict] = field(default_factory=list)
    degraded: bool = False
    cards: list[dict] = field(default_factory=list)


def run_turn(
    session: Session,
    clinic: ClinicContext,
    *,
    conversation: Conversation,
    patient_session: PatientSession,
    patient_text: str,
    family_option_identity: str | None = None,
    family_option_request_id: str | None = None,
    llm: BaseChatModel | None = None,
) -> TurnResult:
    text = (patient_text or "").strip()
    if not text:
        return TurnResult(reply=EMPTY_REPLY, degraded=True)
    if len(text) > MAX_PATIENT_MESSAGE_CHARS:
        text = text[:MAX_PATIENT_MESSAGE_CHARS]

    if conversation.patient_session_id != patient_session.id:
        # Defence in depth: routes already enforce this.
        raise PermissionError("conversation does not belong to this session")

    try:
        model = llm or build_chat_model()
    except AgentProviderError as exc:
        _log_failure(conversation, exc)
        return _degraded(session, clinic, conversation, exc.user_message)

    deps = TurnDeps(
        session=session,
        conversation=conversation,
        patient_session=patient_session,
        llm=model,
        clinic=clinic,
        family_option_identity=family_option_identity,
        family_option_request_id=family_option_request_id,
    )

    try:
        with _serialized_turn(conversation.id):
            # One worker for the round's tool calls: ``ToolNode`` runs them in a
            # thread pool, and every tool shares this turn's single SQLAlchemy
            # session, so they are executed one after another rather than
            # concurrently against the same connection.
            final = TURN_GRAPH.invoke(
                {"deps": deps, "text": text}, config={"max_concurrency": 1}
            )
    except TurnInProgress:
        raise
    except Exception as exc:  # noqa: BLE001 - never leave a half-written turn
        _log_failure(conversation, exc)
        if isinstance(exc, AgentProviderError):
            return _degraded(session, clinic, conversation, exc.user_message)
        message = provider_user_message(exc)
        if message is not None:
            return _degraded(session, clinic, conversation, message)
        logger.exception("Unexpected failure while running conversation turn")
        session.rollback()
        return _degraded(session, clinic, conversation, FALLBACK_REPLY)

    return TurnResult(
        reply=final.get("reply", ""),
        messages=visible_thread_messages(session, clinic, conversation),
        cards=final.get("cards", []),
    )


def greeting_reply(session: Session, clinic: ClinicContext, patient_session: PatientSession) -> str:
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
    return reply_format.strip_dashes(prompts.build_greeting(clinic))


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


def _log_failure(conversation: Conversation, exc: BaseException) -> None:
    # Type only: provider messages never enter the log.
    logger.warning("Chat model failure during turn: %s", type(exc).__name__)
    log_event(
        logger,
        "agent.provider_failed",
        failure=type(exc).__name__,
        conversation=conversation.public_id,
        level=logging.WARNING,
    )


def _degraded(
    session: Session, clinic: ClinicContext, conversation: Conversation, reply: str
) -> TurnResult:
    """Persist a short, friendly reply so history never ends on a dangling call."""
    persist_assistant_text(session, conversation, reply)
    session.commit()
    return TurnResult(
        reply=reply,
        messages=visible_thread_messages(session, clinic, conversation),
        degraded=True,
    )


__all__ = [
    "MAX_PATIENT_MESSAGE_CHARS",
    "TurnInProgress",
    "TurnResult",
    "greeting_reply",
    "quick_suggestions",
    "run_turn",
]
