"""Patient chat endpoints.

Session model
-------------
``POST /api/chat/sessions`` is the only unauthenticated route. It mints an
opaque token and, in the same response, the first conversation plus its greeting.
Every other route requires ``X-Patient-Session`` and resolves conversations
*through* that session, so a conversation ID alone is inert.

Run the model call off the event loop: ``run_turn`` is synchronous and performs
blocking HTTP and SQLite writes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response, status
from starlette.concurrency import run_in_threadpool

from app.agent import runner
from app.api.deps import CurrentPatientSession, DbSession
from app.clinic import CLINIC
from app.config import get_settings
from app.models import Conversation, ConversationStatus, PatientSession, utcnow
from app.schemas import (
    ChatConversationListResponse,
    ChatConversationResponse,
    ChatConversationSummaryResponse,
    ChatMessageResponse,
    ChatSendMessageRequest,
    ChatSendMessageResponse,
    ChatSessionResponse,
    ChatSessionState,
    ChatStartSessionRequest,
)
from app.services import conversations, generations, patient_sessions, patients, scheduling

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["patient chat"])


def _session_state(db: DbSession, record: PatientSession) -> ChatSessionState:
    patient = patient_sessions.verified_patient(db, record)
    complete = bool(patient and all(patients.registration_completeness(patient).values()))
    return ChatSessionState(
        verified=patient is not None,
        registration_complete=complete,
        assistant_configured=get_settings().deepseek_configured,
    )


def _summary(db: DbSession, conversation: Conversation) -> ChatConversationSummaryResponse:
    messages = conversations.history(db, conversation)
    payload = conversations.conversation_summary(conversation, messages)
    return ChatConversationSummaryResponse(
        **{key: value for key, value in payload.items() if key != "conversation_id"}
    )


def _messages_payload(db: DbSession, conversation: Conversation) -> list[ChatMessageResponse]:
    # A restored thread must show what SQLite knows *now*: once this request is
    # held or booked, grids stored as clickable come back as context.
    return [
        ChatMessageResponse(
            **{
                "cards": scheduling.settle_superseded_grids(
                    db, conversation.id, entry.get("cards")
                ),
                **entry,
            }
        )
        for entry in conversations.visible_messages(conversations.history(db, conversation))
    ]


def _pending_cards(db: DbSession, conversation: Conversation) -> list[dict]:
    """Proposals still pending in SQLite, as actionable cards.

    Returned with a restored thread so the browser can re-enable a hold without
    a second round-trip; the client still re-reads ``/scheduling/pending`` to
    retire anything that is no longer live.
    """
    return scheduling.live_proposal_cards(db, conversation.id)


def _load_conversation(db: DbSession, record: PatientSession, public_id: str) -> Conversation:
    conversation = conversations.get_for_session(db, patient_session=record, public_id=public_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found in this session.",
        )
    return conversation


@router.post(
    "/sessions",
    response_model=ChatSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a patient chat session",
)
def start_session(
    db: DbSession, payload: ChatStartSessionRequest | None = None
) -> ChatSessionResponse:
    """Mint an opaque patient session and open (or resume) its conversation.

    A browser that refreshes keeps its token and never reaches this route. The
    optional ``resume_conversation_id`` covers the case where the token is gone
    but the thread is not (the server restarted, or one storage key was
    cleared): a stored thread may be re-owned by this session only when the
    session is verified as the same patient, which the caller re-establishes.
    An unverified session is always given a brand-new conversation.
    """
    issued = patient_sessions.create_patient_session(db)
    record = db.get(PatientSession, issued.session_id)
    if record is None:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not start session."
        )

    conversation = None
    if payload and payload.resume_conversation_id:
        # A refused resume is not an error: the client simply gets a new thread.
        conversation = conversations.resume_for_session(
            db, patient_session=record, public_id=payload.resume_conversation_id
        )

    if conversation is not None:
        greeting = runner.greeting_reply(db, record)
        suggestions = runner.quick_suggestions(db, record)
    else:
        conversation = conversations.create_conversation(
            db, patient_session=record, generation=issued.reset_generation
        )
        greeting = runner.greeting_reply(db, record)
        conversations.append_assistant_message(db, conversation, content=greeting)
        suggestions = runner.quick_suggestions(db, record)
    db.commit()

    return ChatSessionResponse(
        session_token=issued.token,
        session=_session_state(db, record),
        reset_generation=issued.reset_generation,
        conversation_id=conversation.public_id,
        greeting=greeting,
        suggestions=suggestions,
        clinic_name=CLINIC.name,
    )


@router.get(
    "/conversations",
    response_model=ChatConversationListResponse,
    summary="List this session's conversations",
)
def list_conversations(
    db: DbSession, record: CurrentPatientSession, limit: int = 30
) -> ChatConversationListResponse:
    rows = conversations.list_for_session(db, record, limit=max(1, min(limit, 50)))
    return ChatConversationListResponse(
        conversations=[
            conversations.conversation_summary(row, conversations.history(db, row)) for row in rows
        ],
        session=_session_state(db, record),
    )


@router.post(
    "/conversations",
    response_model=ChatConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new conversation in this session",
)
def start_conversation(db: DbSession, record: CurrentPatientSession) -> ChatConversationResponse:
    generation = generations.ensure_generation(db)
    conversation = conversations.create_conversation(
        db, patient_session=record, generation=generation
    )
    greeting = runner.greeting_reply(db, record)
    conversations.append_assistant_message(db, conversation, content=greeting)
    db.commit()
    return ChatConversationResponse(
        conversation_id=conversation.public_id,
        conversation=_summary(db, conversation),
        messages=_messages_payload(db, conversation),
        suggestions=runner.quick_suggestions(db, record),
        session=_session_state(db, record),
        pending_proposals=[],
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ChatConversationResponse,
    summary="Restore one conversation",
)
def get_conversation(
    conversation_id: str, db: DbSession, record: CurrentPatientSession
) -> ChatConversationResponse:
    """Replay one stored thread.

    This is the refresh path: the browser sends the conversation id it stored
    and gets back the chronological transcript (each turn carrying the cards it
    was rendered with) plus the proposals that are still pending *now*, so an
    active hold survives a reload clickable.
    """
    conversation = _load_conversation(db, record, conversation_id)
    return ChatConversationResponse(
        conversation_id=conversation.public_id,
        conversation=_summary(db, conversation),
        messages=_messages_payload(db, conversation),
        suggestions=runner.quick_suggestions(db, record),
        session=_session_state(db, record),
        pending_proposals=_pending_cards(db, conversation),
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ChatSendMessageResponse,
    summary="Send a patient message and run one assistant turn",
)
async def send_message(
    conversation_id: str,
    payload: ChatSendMessageRequest,
    db: DbSession,
    record: CurrentPatientSession,
) -> ChatSendMessageResponse:
    conversation = _load_conversation(db, record, conversation_id)

    text = payload.message.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Message cannot be empty.",
        )

    if not get_settings().deepseek_configured:
        logger.warning("Message received but DEEPSEEK_API_KEY is not configured")

    result = await run_in_threadpool(
        runner.run_turn,
        db,
        conversation=conversation,
        patient_session=record,
        patient_text=text,
    )

    db.refresh(conversation)
    return ChatSendMessageResponse(
        conversation_id=conversation.public_id,
        reply=result.reply,
        messages=[ChatMessageResponse(**entry) for entry in result.messages],
        cards=result.cards,
        session=_session_state(db, record),
        degraded=result.degraded,
    )


@router.post(
    "/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Start over within this browser session",
)
def reset_within_session(db: DbSession, record: CurrentPatientSession) -> Response:
    """Close every thread in this session and start a fresh one.

    This is the in-app "new chat" for the *client*; it deliberately keeps the
    patient session (and therefore the verification state) because the same
    person is still on the other end. A demo *data* reset is different: it is
    run server-side, deletes sessions, and invalidates tokens.
    """
    now = utcnow()
    for conversation in conversations.list_for_session(db, record, limit=50):
        if conversation.status == ConversationStatus.ACTIVE.value:
            conversation.status = ConversationStatus.CLOSED.value
            conversation.ended_at = now
    record.last_seen_at = now
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
