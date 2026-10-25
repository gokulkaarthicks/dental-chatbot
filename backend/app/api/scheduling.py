"""Scheduling endpoints backing the interactive cards.

Why these exist separately from chat
------------------------------------
Clicking a slot, confirming, and cancelling are *deterministic* actions. Routing
them through the model would make a button press depend on an LLM round-trip and
would let wording drift from database state. Instead the UI calls these directly;
each one runs the same services the tools use and returns the resulting cards.

Every route resolves the conversation through the patient session, so a
conversation id alone is never enough, and every write is idempotent through a
client-supplied key.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentPatientSession, DbSession
from app.models import Conversation, utcnow
from app.services import conversations, patient_sessions, scheduling, staff_handoff
from app.services.availability_engine import resolve_appointment_type
from app.services.proposals import find_by_public_id, pending_for_conversation
from app.services.scheduling import Requester

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduling", tags=["scheduling"])


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #


class SlotSelectionRequest(BaseModel):
    """A clicked slot. ``slot_ids`` come from a rendered card.

    ``intent`` / ``target_appointment_id`` come from that card. For a
    replacement time of an existing visit the card carries
    ``intent="reschedule"``, which makes this click continue the move instead of
    creating a new booking -- even when the card was rendered in a later turn.
    """

    slot_ids: list[str] = Field(min_length=1, max_length=6)
    appointment_type: str = Field(default="checkup")
    conversation_id: str | None = None
    intent: str | None = Field(default=None, description="The card's intent, e.g. 'reschedule'.")
    target_appointment_id: int | None = Field(
        default=None, description="The card's target_appointment_id, for a reschedule card."
    )
    require_open: bool = Field(
        default=False,
        description=(
            "The card came from a turn that searched the schedule and carries no "
            "target: it books a new visit even while a move is pending."
        ),
    )
    create_key: str | None = Field(
        default=None,
        description="Client-generated key; resending the same click returns the same proposal.",
    )
    summary: str | None = None
    additional_visit: bool = Field(
        default=False,
        description="The rendered card came from an explicitly requested additional visit.",
    )


class AvailabilityRefreshRequest(BaseModel):
    """Re-check one rendered grid. Read-only: shows times, books nothing."""

    conversation_id: str | None = None
    appointment_type: str = Field(default="checkup")
    slot_ids: list[str] = Field(min_length=1, max_length=12)
    patient_first_name: str | None = None
    intent: str | None = Field(default=None, description="The rendered grid's intent.")
    target_appointment_id: int | None = Field(
        default=None, description="The appointment a reschedule grid is moving."
    )
    additional_visit: bool = Field(
        default=False,
        description="Preserves an explicitly requested additional-visit grid during refresh.",
    )


class AvailabilityRefreshResponse(BaseModel):
    ok: bool
    proposal_id: str | None = None
    cards: list[dict] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    message: str | None = None
    # The conversation's still-pending proposals, re-derived from SQLite, so the
    # client can retire anything this read no longer reports as actionable.
    proposals: list[dict] = Field(default_factory=list)


class ProposalActionRequest(BaseModel):
    conversation_id: str | None = None
    confirm_key: str | None = Field(
        default=None,
        description="Client-generated key; safe to retry after a timeout.",
    )


class SchedulingActionResponse(BaseModel):
    ok: bool
    error: str | None = None
    message: str | None = None
    proposal_id: str | None = None
    # What happened, in the same vocabulary the cards and tools use.
    kind: str | None = None
    intent: str | None = None
    target_appointment_id: int | None = None
    appointment_unchanged: bool | None = None
    confirmed: bool | None = None
    replayed: bool | None = None
    released_appointment_ids: list[int] = Field(default_factory=list)
    cards: list[dict] = Field(default_factory=list)
    proposal: dict | None = None
    session: dict = Field(default_factory=dict)
    # Set when the action failed in a way that can never succeed as it stands
    # (its visit already moved, or booking it would duplicate one). The client
    # stops offering the card; nothing in the database changed.
    proposal_stale: bool = False
    # A booking card that is already waiting, when a hold was refused because of
    # it. The client shows that card instead of offering the clicked time again.
    pending_proposal_id: str | None = None
    next_step: str | None = None



class PendingProposalsResponse(BaseModel):
    """What is actionable right now, read fresh from SQLite.

    ``proposals`` are full proposal cards (the same shape the chat returns), so
    the client can reconcile the transcript against them: a card whose proposal
    is absent here is history, never a button.
    """

    proposals: list[dict] = Field(default_factory=list)
    handoff: list[dict] = Field(default_factory=list)
    conversation_id: str | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _conversation(
    db: DbSession, record: CurrentPatientSession, conversation_id: str | None
) -> Conversation:
    """Resolve the target conversation, defaulting to the session's newest."""
    if conversation_id:
        conversation = conversations.get_for_session(
            db, patient_session=record, public_id=conversation_id
        )
        if conversation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found in this session.",
            )
        return conversation

    rows = conversations.list_for_session(db, record, limit=1)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Start a conversation before scheduling.",
        )
    return rows[0]


def _requester(db: DbSession, record: CurrentPatientSession, conversation: Conversation):
    patient = patient_sessions.verified_patient(db, record)
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verify the patient before scheduling.",
        )
    return Requester(patient=patient, patient_session=record, conversation=conversation)


def _action_payload(result: dict) -> SchedulingActionResponse:
    return SchedulingActionResponse(
        ok=bool(result.get("ok")),
        error=result.get("error"),
        message=result.get("message"),
        proposal_id=result.get("proposal_id"),
        kind=result.get("kind"),
        intent=result.get("intent"),
        target_appointment_id=result.get("target_appointment_id"),
        appointment_unchanged=result.get("appointment_unchanged"),
        confirmed=result.get("confirmed"),
        replayed=result.get("replayed"),
        released_appointment_ids=[
            int(item) for item in (result.get("released_appointment_ids") or [])
        ],
        cards=[card for card in (result.get("cards") or []) if isinstance(card, dict)],
        proposal_stale=bool(result.get("proposal_stale")),
        pending_proposal_id=result.get("pending_proposal_id"),
        next_step=result.get("next_step"),
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get(
    "/pending",
    response_model=PendingProposalsResponse,
    summary="Pending proposals and staff handoffs for this session",
)
def pending(db: DbSession, record: CurrentPatientSession, conversation_id: str | None = None):
    """Everything still awaiting the patient's decision.

    Also used to restore state after a refresh and to reconcile the thread after a
    booking (or when the tab regains focus): pending proposals are persisted, so
    the client can rebuild its cards without replaying the whole transcript, and
    a proposal that is no longer pending here must not stay clickable there.
    Expired holds are also retired on this read, so a lapsed proposal never comes
    back as actionable.
    """
    conversation = _conversation(db, record, conversation_id)
    _expire_lapsed(db, conversation.id)
    live = scheduling.live_proposal_cards(db, conversation.id)
    return PendingProposalsResponse(
        proposals=live,
        handoff=staff_handoff.request_status(db, conversation_id=conversation.id),
        conversation_id=conversation.public_id,
    )


def _expire_lapsed(db: DbSession, conversation_id: int) -> int:
    """Release holds whose window has passed. Cheap, idempotent housekeeping.

    The hold window is short (``HOLD_MINUTES``), so this runs on every read of
    the pending set: a card whose hold lapsed is never handed back as actionable
    with a stale "Held until ..." line.
    """
    from app.services.proposals import release_expired_holds

    released = release_expired_holds(db, conversation_id)
    if released:
        db.commit()
    return len(released)


@router.post(
    "/slots/select",
    response_model=SchedulingActionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Hold a clicked slot and create an awaiting-confirmation proposal",
)
async def select_slot(
    payload: SlotSelectionRequest, db: DbSession, record: CurrentPatientSession
) -> SchedulingActionResponse:
    """Clicking a slot never books. It persists one proposal and returns its card.

    The click is routed by *what the card meant*, not by what the model says:
    a replacement card (``intent="reschedule"``, or any card while a move is
    pending and the clicked time is not the visit's own time) holds a replacement
    for the existing appointment, and everything else holds a new visit. That is
    what keeps "pick a time to move it" from silently becoming "book another
    appointment".
    """
    conversation = _conversation(db, record, payload.conversation_id)
    requester = _requester(db, record, conversation)

    def work() -> dict:
        from app.services import patients as patients_service

        missing = [
            name
            for name, present in patients_service.registration_completeness(
                requester.patient
            ).items()
            if not present
        ]
        if missing:
            return {
                "ok": False,
                "error": "registration_incomplete",
                "message": "Complete registration before booking.",
            }

        # A replacement click, or *any* click while a move is pending, continues
        # that move. Falling through to a booking here is how a chosen
        # replacement time became a second appointment.
        card_says_move = payload.intent == "reschedule" or (
            payload.target_appointment_id is not None
        )
        pending_move = scheduling.pending_target(db, requester)
        # A card that says it was just searched and carries no target is an
        # ordinary booking, even while a move is pending: that grid came from a
        # fresh search the patient is answering now. Routing it into the move
        # made the click fail or hold the wrong thing; the card's own meaning
        # wins. Untouched transcript cards (no marker) keep the old rule.
        fresh_booking = payload.intent != "reschedule" and payload.require_open
        if card_says_move or (pending_move is not None and not fresh_booking):
            return scheduling.replacement_for_slot(
                db,
                requester=requester,
                target_id=payload.target_appointment_id,
                slot_id=payload.slot_ids[0],
                appointment_type_code=payload.appointment_type,
                from_ui=True,
            )

        appointment_type, _ = resolve_appointment_type(db, payload.appointment_type)
        if appointment_type is None:
            return {
                "ok": False,
                "error": "unknown_type",
                "message": "Unknown appointment type.",
            }
        options = scheduling.options_for_slots(
            db,
            appointment_type=appointment_type,
            slot_ids=payload.slot_ids,
            patient_id=requester.patient.id,
            patient_first_name=requester.patient.full_name.split(" ")[0],
        )
        if not options:
            return {
                "ok": False,
                "error": "slot_unavailable",
                "message": "That time is no longer open. Refreshing current options.",
            }
        result = scheduling.propose_booking(
            db,
            requester=requester,
            appointment_type=appointment_type,
            options=options,
            summary=(
                payload.summary
                or ("additional visit requested" if payload.additional_visit else None)
            ),
            from_ui=True,
            create_key=payload.create_key,
        )
        db.commit()
        return result

    result = await run_in_threadpool(work)
    payload_out = _action_payload(result)
    payload_out.kind = result.get("kind")
    payload_out.intent = result.get("intent")
    payload_out.appointment_unchanged = result.get("appointment_unchanged")
    payload_out.target_appointment_id = result.get("target_appointment_id")
    if result.get("proposal_id"):
        proposal = find_by_public_id(db, result["proposal_id"])
        if proposal is not None:
            payload_out.proposal = scheduling.proposal_card(proposal)
    return payload_out


@router.post(
    "/availability",
    response_model=AvailabilityRefreshResponse,
    summary="Re-check the slots behind a rendered grid",
)
def refresh_availability(
    payload: AvailabilityRefreshRequest, db: DbSession, record: CurrentPatientSession
) -> AvailabilityRefreshResponse:
    """Which of these times can still be held, read fresh from SQLite.

    The client sends the slot ids it rendered; each one is decoded and checked
    for real bookability with the same primitive a booking uses. Ids that are no
    longer open come back in ``rejected`` so the UI can disable them without
    guessing, and the ones that survive come back as fresh cards. Nothing is
    held or booked here.
    """
    _conversation(db, record, payload.conversation_id)
    patient = patient_sessions.verified_patient(db, record)
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Verify the patient before scheduling."
        )
    appointment_type, _ = resolve_appointment_type(db, payload.appointment_type)
    if appointment_type is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown appointment type.",
        )

    requested = list(dict.fromkeys(payload.slot_ids))
    options = scheduling.options_for_slots(
        db,
        appointment_type=appointment_type,
        slot_ids=requested,
        patient_id=patient.id,
        patient_first_name=payload.patient_first_name
        or patient.full_name.split(" ")[0],
    )
    still_open = {option.stable_id for option in options}
    rejected = [slot_id for slot_id in requested if slot_id not in still_open]
    # Keep the meaning of the grid: a replacement time stays a reschedule card
    # when a move is pending, so re-rendering it cannot turn it into a booking.
    conversation = _conversation(db, record, payload.conversation_id)
    requester = _requester(db, record, conversation)
    # A pending move is the context these times belong to, even after another
    # turn rendered them as ordinary slots.
    target_id = scheduling.pending_target(db, requester)
    if target_id is not None:
        cards = scheduling.replacement_cards(
            db,
            requester=requester,
            options=options,
            appointment_type=appointment_type,
            target_id=target_id,
        )
    elif payload.intent == "reschedule" and payload.target_appointment_id:
        cards = scheduling.replacement_cards(
            db,
            requester=requester,
            options=options,
            appointment_type=appointment_type,
            target_id=payload.target_appointment_id,
        )
    else:
        # The grid keeps its meaning on refresh: once this request has a booking
        # held or confirmed, its other times come back as context ("settled"),
        # so the UI cannot offer them as fresh holds.
        settled = (
            False
            if payload.additional_visit
            else scheduling.session_has_booking_for(db, requester=requester)
        )
        cards = [
            scheduling.slot_card(
                option,
                appointment_type,
                intent="new_booking",
                settled=settled,
                additional_visit=payload.additional_visit,
            )
            for option in options
        ]
    return AvailabilityRefreshResponse(
        ok=True,
        cards=cards,
        rejected=rejected,
        message=None if cards else "No times from that list are still open.",
        proposals=scheduling.live_proposal_cards(db, conversation.id),
    )


@router.post(
    "/proposals/{proposal_id}/confirm",
    response_model=SchedulingActionResponse,
    summary="Confirm a proposal (revalidates, then books atomically)",
)
async def confirm(
    proposal_id: str,
    payload: ProposalActionRequest,
    db: DbSession,
    record: CurrentPatientSession,
) -> SchedulingActionResponse:
    conversation = _conversation(db, record, payload.conversation_id)
    requester = _requester(db, record, conversation)

    def work() -> dict:
        # One button, two kinds: a new-booking card books, a reschedule or
        # cancellation card moves/releases. Both go through the same atomic
        # confirmation; the service refuses the wrong kind for the proposal.
        # confirm() commits its own write transaction before returning; a second
        # commit here would only be able to fail after success was already staged.
        return scheduling.confirm(
            db,
            requester=requester,
            proposal_id=proposal_id,
            confirm_key=payload.confirm_key,
            actor="ui",
        )

    result = await run_in_threadpool(work)
    if not result.get("ok") and result.get("error") == "unknown_proposal":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.get("message"))
    return _action_payload(result)


@router.post(
    "/proposals/{proposal_id}/decline",
    response_model=SchedulingActionResponse,
    summary="Release a proposal without booking",
)
async def decline(
    proposal_id: str,
    payload: ProposalActionRequest,
    db: DbSession,
    record: CurrentPatientSession,
) -> SchedulingActionResponse:
    conversation = _conversation(db, record, payload.conversation_id)
    requester = _requester(db, record, conversation)

    def work() -> dict:
        return scheduling.decline(db, requester=requester, proposal_id=proposal_id)

    result = await run_in_threadpool(work)
    return _action_payload(result)


@router.get(
    "/appointments",
    response_model=SchedulingActionResponse,
    summary="The verified patient's real appointments",
)
def appointments_list(db: DbSession, record: CurrentPatientSession):
    from app.services import appointments as appointments_service

    patient = patient_sessions.verified_patient(db, record)
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Verify the patient first."
        )
    views = appointments_service.list_for_patient(db, patient.id)
    return SchedulingActionResponse(
        ok=True,
        cards=[scheduling.appointment_cards(views)] if views else [],
    )


@router.post(
    "/proposals/expire",
    response_model=SchedulingActionResponse,
    summary="Expire this conversation's stale pending proposals",
)
def expire_pending(
    db: DbSession, record: CurrentPatientSession, conversation_id: str | None = None
) -> SchedulingActionResponse:
    """Housekeeping used by the UI refresh path.

    Holds last 30 minutes; a proposal past its window should stop blocking slots
    even if nobody clicked anything.
    """
    conversation = _conversation(db, record, conversation_id)
    expired = _expire_lapsed(db, conversation.id)
    return SchedulingActionResponse(
        ok=True,
        message=f"Expired {expired} proposal(s)." if expired else None,
    )


__all__ = ["router"]
