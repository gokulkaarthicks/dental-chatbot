"""Booking proposals: creation, holds, rejection, and atomic confirmation.

Three phases
------------
**Select (no lock).** A read-only search decides what to offer. Reading never
blocks a writer, so an LLM turn can think for seconds without holding SQLite.

**Claim (``BEGIN IMMEDIATE``).** Revalidates every interval *inside* the write
lock, inserts the proposal, and holds the intervals through ``slot_claims``
(``held_by_proposal_id`` set). A race fails here with ``SlotConflictError``
rather than producing a proposal for a slot someone else just took.

**Confirm (``BEGIN IMMEDIATE``).** Revalidates again, releases the holds, writes
one appointment per item, and marks the proposal accepted -- one transaction for
the whole group, so a family booking either lands completely or not at all.

Reschedules are a *second kind of confirmation*, not a booking
---------------------------------------------------------------
A ``reschedule`` proposal names the appointment it replaces. Confirming it, in
the same transaction:

1. revalidates the replacement run,
2. releases the original's slot claims (only after step 1 passed),
3. retires the original -- ``status = rescheduled`` plus ``rescheduled_to_id``,
   with its time, provider and duration left intact as history,
4. creates a **new** booked appointment for the same patient at the selected
   time, linked back through ``rescheduled_from_id``.

Nothing is mutated outside the transaction, so a rejection, expiry or conflict
leaves the original untouched and still booked. Replaying an accepted proposal
re-reads ``booking_groups.details`` and returns the committed ids.

Two invariants re-checked under the write lock
----------------------------------------------
* **Ownership.** The original must belong to the patient the confirming
  conversation is verified as -- checked again *inside* the write lock, so a
  forged or replayed proposal can never move somebody else's visit.
* **No double booking.** The replacement must not overlap one of the patient's
  other live appointments. Searching hides overlapping times, but the model can
  hand a reschedule an id from an earlier search; refusing here keeps the
  "exactly one active appointment" promise even when that search is stale.

Only the partial unique index on ``slot_claims`` (and the unique
``booking_groups.confirmation_key``) is load-bearing for correctness; the checks
here exist to produce clean, explainable errors.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import require_clinic_id
from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AvailabilitySlot,
    BookingGroup,
    BookingProposal,
    Conversation,
    FamilyMember,
    IdempotencyRecord,
    ProposalItem,
    ProposalStatus,
    SlotClaim,
    SlotStatus,
    utcnow,
)
from app.observability import log_event
from app.services import audit, generations, transactions
from app.services.availability_engine import SlotOption, is_interval_bookable
from app.services.transactions import SlotConflictError, WriteTransactionError, write_transaction
from app.tenancy import ClinicContext, context_from, load_clinic_row

logger = logging.getLogger(__name__)

# How long a patient's hold blocks the slot for everyone else. This is the clock
# the patient sees ("Held until 5:13 AM") and the one that must be short: a
# clicked time that nobody confirms is usually a patient who changed their mind,
# and half an hour of a dentist's day is a long time to reserve for a click that
# was reconsidered 20 seconds later. Unclicking releases it immediately (see
# ``release_hold_on_change`` and ``expire_proposal``); the window only bounds a
# patient who walks away from the tab.
HOLD_MINUTES = 2


@dataclass(frozen=True)
class DraftItem:
    """One appointment a proposal would create (or move, for a reschedule)."""

    patient_id: int
    patient_first_name: str
    appointment_type: AppointmentType
    option: SlotOption
    target_appointment_id: int | None = None


@dataclass
class Outcome:
    ok: bool
    error: str | None = None
    message: str | None = None
    proposal_id: str | None = None
    booking_group_id: str | None = None
    appointments: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    replayed: bool = False
    # Appointments this confirmation made inactive (a reschedule's original, a
    # cancellation's target). Empty for ordinary bookings.
    released_appointment_ids: list[int] = field(default_factory=list)
    # One entry per moved appointment: original id -> replacement id, with the
    # times and provider on either side.
    rescheduled: list[dict[str, Any]] = field(default_factory=list)


INTENTS = ("new_booking", "reschedule", "cancellation")


def _intent_for_kind(kind: str) -> str:
    """The patient-facing intent that corresponds to a proposal kind."""
    return kind if kind in INTENTS else "new_booking"


def proposal_intent(proposal: BookingProposal) -> str:
    """The recorded intent, falling back to the kind for pre-intent rows."""
    return proposal.intent or _intent_for_kind(proposal.kind)


def new_public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def new_confirmation_key() -> str:
    return f"confirm-{secrets.token_urlsafe(18)}"


# Scope for the confirmation idempotency records (see ``_resolve_confirm_key``).
IDEMPOTENCY_SCOPE = "proposal.confirm"


class ConfirmationRefused(RuntimeError):
    """A confirmation that cannot be made, carrying a clean patient-facing reason.

    Raised only from inside a confirmation transaction. Whatever had already been
    written in that transaction is rolled back, so a refusal always leaves the
    database exactly as it was -- for a reschedule, with the original still
    booked and its intervals still claimed.
    """

    def __init__(self, message: str, *, reason: str = "booking_conflict") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


def fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


def hold_deadline(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(minutes=HOLD_MINUTES)


# --------------------------------------------------------------------------- #
# Read helpers
# --------------------------------------------------------------------------- #


def find_by_public_id(session: Session, public_id: str) -> BookingProposal | None:
    return session.scalar(
        select(BookingProposal)
        .where(BookingProposal.public_id == public_id)
        .options(selectinload(BookingProposal.items))
    )


def _clinic(session: Session) -> ClinicContext:
    """This session's tenant snapshot, for the grid a proposal is checked against.

    Every row in here stamps ``clinic_id`` from the session's binding (see
    :func:`app.db.require_clinic_id`), and an interval is validated against the
    same binding rather than a parameter a caller could pass by mistake. One
    clinic's slot rows checked on another clinic's slot length is exactly the
    failure a proposal cannot recover from at confirmation time.

    ``tenancy.context_from`` caches on the clinic row's ``updated_at``, so this
    costs one indexed read on a cold cache and nothing on a warm one, even
    though a family booking validates several intervals in a transaction.
    """
    clinic_id = require_clinic_id(session)
    row = load_clinic_row(session, clinic_id)
    if row is None:
        raise RuntimeError(f"session is bound to a missing clinic {clinic_id}")
    return context_from(session, row)


def find_by_create_key(
    session: Session,
    clinic: ClinicContext,
    *,
    conversation_id: int,
    create_key: str,
) -> BookingProposal | None:
    """The proposal this turn key already created, if it is still live.

    A retry inside one turn must find its own hold. Without this the second
    attempt re-derives the options, sees the slots its own first attempt is
    holding, and reports a slot conflict for a time nobody else took.

    Scoped by ``conversation_id`` as well as the key: a turn key is derived from
    the turn, so two conversations can legitimately carry the same string and
    must never replay each other's hold. ``clinic_id`` is matched explicitly
    rather than left to the session's loader criterion, so a key from another
    tenant reads as "no such proposal" instead of a row the caller may act on.

    The caller decides what a lapsed or resolved proposal means: this returns
    the row whatever its status, and ``scheduling.replay_hold`` is the one that
    requires ``PENDING`` before handing a card back.
    """
    return session.scalar(
        select(BookingProposal)
        .where(
            BookingProposal.clinic_id == clinic.clinic_id,
            BookingProposal.conversation_id == conversation_id,
            BookingProposal.create_key == create_key,
        )
        .options(selectinload(BookingProposal.items))
    )


def pending_for_conversation(session: Session, conversation_id: int) -> list[BookingProposal]:
    return list(
        session.scalars(
            select(BookingProposal)
            .where(
                BookingProposal.conversation_id == conversation_id,
                BookingProposal.status == ProposalStatus.PENDING.value,
            )
            .options(selectinload(BookingProposal.items))
            .order_by(BookingProposal.id)
        )
    )


def release_holds(session: Session, proposal: BookingProposal, now: datetime) -> int:
    """Deactivate every hold this proposal owns. Returns how many were released."""
    rows = list(
        session.scalars(
            select(SlotClaim).where(
                SlotClaim.held_by_proposal_id == proposal.id,
                SlotClaim.active.is_(True),
            )
        )
    )
    for row in rows:
        row.active = False
        row.released_at = now
    return len(rows)


def expire_proposal(session: Session, proposal: BookingProposal, now: datetime) -> bool:
    """Release a proposal's holds. True when this call actually released any.

    It does **not** change ``status``: a lapsed hold ends the reservation, not
    the patient's choice. The card stays confirmable, and confirmation
    revalidates the interval inside its write transaction -- it books the time
    if it is still open and reports ``slot_taken`` if somebody else took it.
    Retiring a proposal outright is ``reject_proposal`` (declined) or
    ``supersede_pending`` (replaced by a newer choice).
    """
    if proposal.status != ProposalStatus.PENDING.value:
        return False
    return release_holds(session, proposal, now) > 0


def release_expired_holds(session: Session, conversation_id: int) -> list[str]:
    """Hand back held times whose window has passed. Returns the affected ids.

    A hold is short (``HOLD_MINUTES``) so a clicked time never sits on a
    dentist's calendar while the patient is still deciding. When the window
    passes, the slots are released for everyone else -- but the proposal itself
    stays pending and confirmable, because nothing was booked and the patient
    did not decline. Confirming a lapsed card revalidates the interval and books
    it if it is still open; if someone else took it, the usual "that time was
    just taken" path applies.

    Accepts a ``public_id`` too, so a caller holding the conversation record
    cannot pass the wrong numeric id.
    """
    identifier: Any = conversation_id
    if isinstance(identifier, str):
        row = session.scalar(
            select(Conversation.id).where(Conversation.public_id == identifier)
        )
        if row is None:
            return []
        identifier = row

    released: list[str] = []
    now = utcnow()
    for proposal in pending_for_conversation(session, identifier):
        # A missing ``expires_at`` counts as lapsed: fixtures and any row written
        # before ``create_proposal`` set the column carry no window, and skipping
        # them would leave their holds on the calendar forever. Only a window
        # still in the future keeps a hold alive.
        if proposal.expires_at is not None and proposal.expires_at > now:
            continue
        if expire_proposal(session, proposal, now):
            audit.record(
                session,
                action="proposal.hold_lapsed",
                actor_type="system",
                entity_type="booking_proposal",
                entity_id=proposal.public_id,
                conversation_id=proposal.conversation_id,
                details={"reason": "hold_window_passed", "hold_minutes": HOLD_MINUTES},
            )
            released.append(proposal.public_id)
    return released


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


def create_proposal(
    session: Session,
    *,
    conversation_id: int,
    items: list[DraftItem],
    kind: str = "booking",
    summary: str | None = None,
    from_ui: bool = False,
    create_key: str | None = None,
    family_group_id: int | None = None,
    target_appointment_id: int | None = None,
    cancellation_reason: str | None = None,
    intent: str | None = None,
    visit_shape: str | None = None,
    request_id: str | None = None,
) -> Outcome:
    """Create a pending proposal and hold its slots (or replay ``create_key``).

    ``target_appointment_id`` is required for ``reschedule`` and
    ``cancellation`` proposals; those kinds refer to one existing appointment.

    ``intent`` records *why* the proposal exists (``new_booking`` /
    ``reschedule`` / ``cancellation``). It defaults to the kind, and exists so a
    reschedule proposal can be told apart from an ordinary booking even by code
    that only reads the column -- the two must never be interchangeable.
    """
    if not items:
        return Outcome(ok=False, error="empty_proposal", message="There's nothing to propose.")
    if kind not in {"booking", "reschedule", "cancellation"}:
        return Outcome(ok=False, error="unknown_kind", message="That kind of change isn't supported.")
    requested_intent = intent or _intent_for_kind(kind)
    if requested_intent != _intent_for_kind(kind):
        # The three intents are not interchangeable: a booking that claims to be
        # a reschedule would have no original to retire, and a reschedule that
        # claims to be a booking would leave the visit it moves still active.
        return Outcome(
            ok=False,
            error="intent_mismatch",
            message="A move of an existing appointment must be proposed as a reschedule.",
        )
    if kind in {"reschedule", "cancellation"}:
        if target_appointment_id is None:
            return Outcome(
                ok=False,
                error="missing_target",
                message="Which appointment should this apply to?",
            )
        if len(items) != 1:
            return Outcome(
                ok=False,
                error="invalid_items",
                message="A reschedule or cancellation covers one appointment.",
            )
    for item in items:
        if kind == "booking" and item.target_appointment_id is not None:
            # An additional visit replaces nothing. Accepting a target here is how
            # a move gets recorded as a booking that never retires the original.
            return Outcome(
                ok=False,
                error="intent_mismatch",
                message="A new booking doesn't replace an existing appointment.",
            )
        if (
            kind == "reschedule"
            and item.target_appointment_id is not None
            and item.target_appointment_id != target_appointment_id
        ):
            return Outcome(
                ok=False,
                error="invalid_items",
                message="A reschedule moves the one appointment it names.",
            )
    if len(items) > 6:
        return Outcome(
            ok=False,
            error="too_many_items",
            message="One booking can cover at most six appointments.",
        )
    patient_ids = {item.patient_id for item in items}
    if len(patient_ids) != len(items) and kind == "booking":
        return Outcome(
            ok=False,
            error="duplicate_patient",
            message="Each person needs their own appointment in a booking.",
        )

    if create_key:
        # Capture the key's outcome before opening the write block: reading
        # inside it would share the caller's implicit read transaction, which
        # is then never committed.
        replayed = session.scalar(
            select(BookingProposal).where(
                BookingProposal.create_key == create_key,
                BookingProposal.conversation_id == conversation_id,
            )
        )
        if replayed is not None:
            return Outcome(ok=True, proposal_id=replayed.public_id, replayed=True)

    request_fingerprint = fingerprint(
        {
            "conversation_id": conversation_id,
            "kind": kind,
            "items": [
                (
                    item.patient_id,
                    item.appointment_type.id,
                    item.option.provider_id,
                    item.option.stable_id,
                )
                for item in items
            ],
        }
    )

    try:
        with write_transaction(session):
            now = utcnow()
            proposal = BookingProposal(
                clinic_id=require_clinic_id(session),
                public_id=new_public_id("prop"),
                conversation_id=conversation_id,
                status=ProposalStatus.PENDING.value,
                kind=kind,
                summary=summary,
                reset_generation=generations.get_generation(session) or "unknown",
                expires_at=now + timedelta(minutes=HOLD_MINUTES),
                from_ui=from_ui,
                create_key=create_key,
                family_group_id=family_group_id,
                target_appointment_id=target_appointment_id,
                cancellation_reason=cancellation_reason,
                intent=requested_intent,
                visit_shape=(
                    (visit_shape or "solo") if kind == "booking" else None
                ),
                request_id=request_id,
                # Ordinary bookings never reference existing intervals; reschedule
                # and cancellation fill this in below once validated.
                held_slot_ids=None,
            )
            session.add(proposal)
            session.flush()

            # Holds are inserted as each run validates (see _ProposalValidator),
            # so a concurrent claim fails on the unique index immediately.
            validator = _ProposalValidator(session, proposal.id)
            validated = [
                (
                    item,
                    validator.reserve(
                        item.option,
                        item.appointment_type,
                        ignore_appointment_id=(
                            item.target_appointment_id
                            if kind in {"reschedule", "cancellation"}
                            else None
                        ),
                    ),
                )
                for item in items
            ]
            if validator.unheld:
                proposal.held_slot_ids = sorted(validator.unheld)
                session.flush()

            # Existing time per target, read once: ``original_starts_at`` is
            # stored while the proposal is created, so a card read after the
            # move committed still says "was Tue, Oct 27 at 10:00 AM".
            original_starts = _original_starts_by_target(session, items)
            for order_index, (item, rows) in enumerate(validated):
                session.add(
                    ProposalItem(
                        clinic_id=require_clinic_id(session),
                        proposal_id=proposal.id,
                        patient_id=item.patient_id,
                        provider_id=item.option.provider_id,
                        appointment_type_id=item.appointment_type.id,
                        availability_slot_id=rows[0].id,
                        proposed_starts_at=item.option.starts_at,
                        duration_minutes=item.appointment_type.duration_minutes,
                        order_index=order_index,
                        stable_slot_id=item.option.stable_id,
                        provider_name=item.option.provider_name,
                        provider_credentials=item.option.provider_credentials,
                        appointment_type_code=item.appointment_type.code,
                        appointment_type_name=item.appointment_type.name,
                        patient_first_name=item.patient_first_name,
                        target_appointment_id=item.target_appointment_id,
                        original_starts_at=original_starts.get(
                            item.target_appointment_id, item.option.starts_at
                        ),
                    )
                )

            audit.record(
                session,
                action=f"proposal.created.{kind}",
                actor_type="ui" if from_ui else "assistant",
                entity_type="booking_proposal",
                entity_id=proposal.public_id,
                conversation_id=conversation_id,
                details={
                    "items": len(items),
                    "fingerprint": request_fingerprint,
                    "held_until": hold_deadline(now).isoformat(timespec="seconds"),
                },
                visibility="HOLD",
            )
            public_id = proposal.public_id
    except SlotConflictError as exc:
        return Outcome(
            ok=False,
            error="slot_taken",
            message=str(exc),
            conflicts=[{"slot_id": slot_id} for slot_id in exc.stable_slot_ids],
        )
    except WriteTransactionError as exc:
        return Outcome(ok=False, error="busy", message=str(exc))
    except IntegrityError:
        logger.info("Proposal lost a slot race (unique active claim)")
        return Outcome(
            ok=False,
            error="slot_taken",
            message=(
                "That new time was just taken, so nothing was changed. Let me show "
                "current options."
                if kind == "reschedule"
                else "One of those times was just taken. Let me show current options."
            ),
            conflicts=[{"slot_id": item.option.stable_id} for item in items],
        )

    # Written outside the transaction: only a committed create gets a trailer.
    _write_trailer(
        conversation_id=conversation_id,
        proposal_id=public_id,
        key=f"create:{public_id}",
        payload={"action": "proposal.created", "fingerprint": request_fingerprint},
    )
    log_event(
        logger,
        "proposal.created",
        proposal_id=public_id,
        kind=kind,
        items=len(items),
        conversation_id=conversation_id,
        from_ui=from_ui,
    )
    return Outcome(ok=True, proposal_id=public_id)


def _original_starts_by_target(
    session: Session, items: list[DraftItem]
) -> dict[int, datetime]:
    """The current start time of each appointment a proposal would replace.

    A reschedule's item has to record where the visit is *now* -- not the time
    being proposed -- otherwise the card cannot show "was X, now Y" and the
    patient cannot tell the move apart from a second booking.
    """
    targets = {item.target_appointment_id for item in items if item.target_appointment_id}
    if not targets:
        return {}
    rows = session.scalars(select(Appointment).where(Appointment.id.in_(sorted(targets))))
    return {row.id: row.starts_at for row in rows if row.starts_at is not None}


class _ProposalValidator:
    """Re-checks intervals inside an open write transaction."""

    def __init__(self, session: Session, proposal_id: int) -> None:
        self.session = session
        self.proposal_id = proposal_id
        # The tenant grid every interval in this transaction is measured on.
        # Resolved once here rather than per item: a family booking validates
        # several runs, and the snapshot is cached on the clinic row anyway.
        self.clinic = _clinic(session)
        self.reserved: set[int] = set()
        # Interval ids this proposal relies on without holding them (the target
        # appointment's own intervals for a reschedule or cancellation).
        self.unheld: set[int] = set()

    def reserve(
        self,
        option: SlotOption,
        appointment_type: AppointmentType,
        *,
        ignore_appointment_id: int | None = None,
    ) -> list[AvailabilitySlot]:
        """Validate the run and immediately hold it.

        Holding here (rather than after all validation) keeps the insert close
        to the check it depends on, and turns a lost race into the driver's
        UNIQUE violation instead of a stale "looks free" read. Like every other
        write in ``create_proposal``, the hold is discarded if the transaction
        rolls back.

        ``ignore_appointment_id`` is the visit a reschedule or cancellation is
        about. Its existing claims must not count as a conflict against the
        proposal that will release or move it, otherwise neither kind of
        proposal could ever be created. When the option *is* that appointment's
        current time, nothing is held: the appointment's own claims already
        reserve it, and a slot allows only one active claim.
        """
        rows = is_interval_bookable(
            self.session,
            self.clinic,
            provider_id=option.provider_id,
            starts_at=option.starts_at,
            duration_minutes=appointment_type.duration_minutes,
            appointment_type=appointment_type,
            ignore_appointment_id=ignore_appointment_id,
        )
        if rows is None:
            raise SlotConflictError(
                f"The {option.starts_at.strftime('%-I:%M %p')} time with "
                f"{option.provider_name} is no longer open.",
                stable_slot_ids=[option.stable_id],
            )
        if ignore_appointment_id is not None:
            current = set(
                self.session.scalars(
                    select(SlotClaim.availability_slot_id).where(
                        SlotClaim.appointment_id == ignore_appointment_id,
                        SlotClaim.active.is_(True),
                    )
                )
            )
            if current and all(row.id in current for row in rows):
                # A reschedule/cancellation proposal for the appointment's
                # *current* intervals holds nothing: the appointment's own claims
                # are the reservation, and a slot allows only one active claim.
                # Record the ids so confirmation can tell this apart from a
                # booking that happens to target the same time.
                self.unheld.update(row.id for row in rows)
                return rows
        for row in rows:
            if row.id in self.reserved:
                raise SlotConflictError(
                    "Two people were assigned the same time. Pick a different option.",
                    stable_slot_ids=[option.stable_id],
                )
            self.reserved.add(row.id)
            self.session.add(
                SlotClaim(
                    clinic_id=require_clinic_id(self.session),
                    availability_slot_id=row.id,
                    appointment_id=None,
                    held_by_proposal_id=self.proposal_id,
                    active=True,
                )
            )
        # Flush now: the partial unique index rejects a concurrent hold here,
        # not later at commit time where the cause would be harder to explain.
        self.session.flush()
        return rows


# --------------------------------------------------------------------------- #
# Rejection / superseding
# --------------------------------------------------------------------------- #


def reject_proposal(
    session: Session,
    *,
    proposal: BookingProposal,
    reason: str | None = None,
    actor: str = "patient",
) -> Outcome:
    """Decline a pending proposal and release its held intervals."""
    if proposal.status != ProposalStatus.PENDING.value:
        return _already_resolved(proposal)
    try:
        with write_transaction(session):
            now = utcnow()
            release_holds(session, proposal, now)
            proposal.status = ProposalStatus.REJECTED.value
            proposal.resolved_at = now
            audit.record(
                session,
                action="proposal.rejected",
                actor_type=actor,
                entity_type="booking_proposal",
                entity_id=proposal.public_id,
                conversation_id=proposal.conversation_id,
                details={"reason": reason or "declined"},
            )
    except WriteTransactionError as exc:
        return Outcome(ok=False, error="busy", message=str(exc))
    return Outcome(ok=True, proposal_id=proposal.public_id, message="Proposal declined.")


def supersede_pending(
    session: Session,
    *,
    conversation_id: int,
    kinds: tuple[str, ...] = ("booking",),
    keep_public_id: str | None = None,
    target_appointment_ids: set[int] | None = None,
    intent: str | None = None,
) -> list[str]:
    """Mark still-pending proposals as superseded.

    Called when the patient changes their mind ("actually, Friday instead"), so
    only one proposal is awaiting confirmation and stale holds are released
    instead of quietly blocking slots until they expire. A superseded proposal is
    *not* reported to the patient as "rejected"; it was replaced.

    ``target_appointment_ids`` limits the replacement to proposals about those
    existing visits, regardless of kind.

    ``intent`` limits it to one kind of request. A refresh of a household search
    must not release a booking card the household already has waiting: that card
    is the one they are about to confirm, and superseding it would make them
    search again for a time they had already chosen.
    """
    stale = [
        proposal
        for proposal in pending_for_conversation(session, conversation_id)
        if proposal.kind in kinds and proposal.public_id != keep_public_id
    ]
    if intent is not None:
        stale = [proposal for proposal in stale if proposal_intent(proposal) == intent]
    target_ids = target_appointment_ids or set()
    if target_ids:
        # A newer proposal *about the same visit* replaces the older one too,
        # even across kinds ("actually, just cancel it"). Without this, an old
        # reschedule card and a fresh cancellation card would both stay pending
        # for one appointment and the patient could confirm the stale one.
        stale.extend(
            proposal
            for proposal in pending_for_conversation(session, conversation_id)
            if proposal.public_id != keep_public_id
            and proposal not in stale
            and proposal.kind in {"reschedule", "cancellation"}
            and (
                proposal.target_appointment_id in target_ids
                or any(
                    item.target_appointment_id in target_ids for item in proposal.items
                )
            )
        )
    if not stale:
        return []
    superseded: list[str] = []
    try:
        with write_transaction(session):
            now = utcnow()
            for proposal in stale:
                release_holds(session, proposal, now)
                proposal.status = ProposalStatus.SUPERSEDED.value
                proposal.resolved_at = now
                audit.record(
                    session,
                    action="proposal.superseded",
                    actor_type="system",
                    entity_type="booking_proposal",
                    entity_id=proposal.public_id,
                    conversation_id=conversation_id,
                    details={"reason": "replaced_by_newer_proposal"},
                )
                superseded.append(proposal.public_id)
    except WriteTransactionError:
        logger.warning("Could not supersede proposals in conversation %s", conversation_id)
    return superseded


def _already_resolved(proposal: BookingProposal) -> Outcome:
    label = {
        ProposalStatus.REJECTED.value: "was cancelled",
        ProposalStatus.EXPIRED.value: "expired",
        ProposalStatus.SUPERSEDED.value: "was replaced by a newer choice",
    }.get(proposal.status, "is no longer open")
    return Outcome(
        ok=False,
        error=f"proposal_{proposal.status}",
        proposal_id=proposal.public_id,
        message=f"This proposal {label}. I can find current times for you.",
    )


# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #


def confirm_proposal(
    session: Session,
    *,
    proposal: BookingProposal,
    confirm_key: str | None = None,
    actor: str = "patient",
    patient_session_id: int | None = None,
) -> Outcome:
    """Revalidate and book everything in the proposal in one transaction.

    ``confirm_key`` makes confirmation idempotent: a double click or a retried
    request returns the original result instead of booking twice. A concurrent
    duplicate is rejected by the UNIQUE index on ``booking_groups`` and then
    replayed from that group.
    """
    if proposal.status == ProposalStatus.ACCEPTED.value:
        return _replay_accepted(session, proposal)
    # A lapsed *hold* is not a lost choice: the click still counts, and the
    # transaction below decides whether the time is still there to take. Only
    # cancellation, supersession and a confirmed booking are irreversible.
    if proposal.status != ProposalStatus.PENDING.value:
        return _already_resolved(proposal)

    items = sorted(proposal.items, key=lambda item: item.id)
    if not items:
        return Outcome(ok=False, error="empty_proposal", message="This proposal has no times.")

    key = _resolve_confirm_key(
        session,
        proposal=proposal,
        confirm_key=confirm_key,
        actor=actor,
        patient_session_id=patient_session_id,
    )
    trailer: dict[str, Any] | None = None
    try:
        with write_transaction(session):
            # Re-read the whole proposal under the write lock: another request
            # may have resolved it, and the items this transaction validates
            # must be the committed rows, not a snapshot taken before the lock.
            session.refresh(proposal)
            if proposal.status == ProposalStatus.ACCEPTED.value:
                return _replay_accepted(session, proposal)
            if proposal.status != ProposalStatus.PENDING.value:
                return _already_resolved(proposal)
            items = sorted(proposal.items, key=lambda item: item.id)
            outcome, trailer = _commit_group(
                session,
                proposal=proposal,
                items=items,
                confirm_key=key,
                actor=actor,
                patient_session_id=patient_session_id,
            )
    except ConfirmationRefused as refusal:
        # A business-rule refusal raised inside the write block: the transaction
        # has been rolled back, so nothing was written and the original (if any)
        # is intact. A semantic conflict is permanently invalid and its holds
        # are released; a transient taken-slot conflict stays retryable.
        return _conflict_outcome(
            session,
            proposal,
            refusal.message,
            [],
            reason=refusal.reason,
        )
    except SlotConflictError as exc:
        log_event(
            logger,
            "booking.conflict_rejected",
            reason="slot_taken",
            proposal_id=proposal.public_id,
            level=logging.WARNING,
        )
        return _conflict_outcome(
            session,
            proposal,
            str(exc),
            exc.stable_slot_ids,
            reason=exc.reason,
        )
    except WriteTransactionError as exc:
        log_event(
            logger,
            "booking.conflict_rejected",
            reason="write_transaction",
            proposal_id=proposal.public_id,
            level=logging.WARNING,
        )
        return Outcome(ok=False, error="busy", message=str(exc))
    except IntegrityError:
        logger.info("Confirmation lost a race; replaying by confirmation key")
        log_event(
            logger,
            "idempotency.hit",
            kind="confirm",
            proposal_id=proposal.public_id,
        )
        replayed = _replay_by_key(session, key)
        if replayed is not None:
            return replayed
        # Nothing was committed: the proposal is still pending, and a reschedule's
        # original is still booked. Say so with the same code the UI knows.
        return _conflict_outcome(
            session,
            proposal,
            "That time was booked by someone else moments ago.",
            [item.stable_slot_id for item in items if item.stable_slot_id],
            reason="slot_taken",
        )

    if trailer is not None:
        _write_trailer(**trailer)
    if outcome.ok:
        # Logged after the transaction committed: a failure path never reaches
        # here, so `booking.commit` in the log means a row really exists.
        log_event(
            logger,
            "appointment.rescheduled" if proposal.kind == "reschedule" else "booking.commit",
            proposal_id=proposal.public_id,
            kind=proposal.kind,
            items=len(items),
            appointments=len(outcome.appointments) if outcome.appointments else 0,
            released=len(outcome.released_appointment_ids),
            replayed=outcome.replayed,
            actor=actor,
        )
    return outcome


def _commit_group(
    session: Session,
    *,
    proposal: BookingProposal,
    items: list[ProposalItem],
    confirm_key: str,
    actor: str,
    patient_session_id: int | None,
) -> tuple[Outcome, dict[str, Any] | None]:
    """Runs inside the caller's write transaction. Returns (outcome, trailer).

    Everything below happens under one ``BEGIN IMMEDIATE``: revalidation, the
    release of the original's claims, the original's status change, the
    replacement row, its claims, and the proposal's own status. Any exception
    rolls the lot back, so a reschedule can never leave the original released
    without a replacement (or vice versa).
    """
    now = utcnow()
    kind = proposal.kind
    _validate_semantics(proposal)

    # 1. For a reschedule, prove the visit being moved can still be moved
    #    *before* looking at the replacement. Checking it first means a stale or
    #    foreign target is reported as exactly that, and nothing has been
    #    revalidated against a claim that is about to be found ineligible.
    originals: dict[int, Appointment] = {}
    if kind == "reschedule":
        originals = _load_reschedule_originals(session, proposal, items)

    # 2. Revalidate *every* item before writing anything. A single unavailable
    #    row aborts the whole group, so a family booking never lands partially.
    #    A cancellation has nothing to revalidate: it only releases time.
    prepared: list[tuple[ProposalItem, AppointmentType, list[AvailabilitySlot]]] = []
    unavailable: list[str] = []
    if kind != "cancellation":
        for item in items:
            checked = _revalidate(session, proposal, item, originals=originals)
            if checked is None:
                unavailable.append(item.stable_slot_id or f"item-{item.id}")
            else:
                prepared.append(checked)
        if unavailable:
            raise SlotConflictError(
                _conflict_message(prepared, unavailable), stable_slot_ids=unavailable
            )
        if kind == "booking":
            _refuse_duplicate_booking(
                session, proposal, [(item, rows) for item, _type, rows in prepared]
            )
            # One request, one booking: a stale sibling card from the same
            # request can never become a second visit.
            _refuse_superseded_visit(session, proposal, prepared)
    elif proposal.target_appointment_id is None:
        raise SlotConflictError("That appointment no longer exists.")

    # 3. Release this proposal's own holds and the claims of any appointment it
    #    replaces. Still inside the transaction: nothing is visible outside it.
    release_holds(session, proposal, now)
    released: list[int] = []
    for appointment in originals.values():
        _release_claims(session, appointment.id, now)
        released.append(appointment.id)

    # 4. Create the group, then one appointment per item.
    group = BookingGroup(
        clinic_id=require_clinic_id(session),
        public_id=new_public_id("grp"),
        conversation_id=proposal.conversation_id,
        proposal_id=proposal.id,
        patient_session_id=patient_session_id,
        confirmation_key=confirm_key,
        kind=kind,
        family_group_id=proposal.family_group_id,
        summary=proposal.summary,
        reset_generation=generations.get_generation(session) or "unknown",
    )
    session.add(group)
    session.flush()  # UNIQUE(confirmation_key) is the idempotency guard
    _record_confirm_key(
        session,
        key=confirm_key,
        proposal=proposal,
        actor=actor,
        patient_session_id=patient_session_id,
    )
    created: list[dict[str, Any]] = []
    cancelled: dict[str, Any] | None = None
    reschedule_details: list[dict[str, Any]] = []
    if kind == "cancellation":
        target = session.get(Appointment, proposal.target_appointment_id)
        if target is None or target.status != AppointmentStatus.BOOKED.value:
            raise ConfirmationRefused("That appointment was already cancelled.")
        target.status = AppointmentStatus.CANCELLED.value
        target.cancelled_at = now
        # Release holds the cancelled appointment no longer needs.
        _release_claims(session, target.id, now)
        if target.id not in released:
            released.append(target.id)
        cancelled = {
            "appointment_id": target.id,
            "patient_id": target.patient_id,
            "starts_at": target.starts_at.isoformat(timespec="minutes"),
            "reason": proposal.cancellation_reason,
        }
        items[0].issue = None

    for item, appointment_type, rows in prepared:
        original = originals.get(item.target_appointment_id or 0)
        if original is not None:
            appointment, move = _create_reschedule_replacement(
                session,
                proposal=proposal,
                item=item,
                original=original,
                appointment_type=appointment_type,
                rows=rows,
                group=group,
                now=now,
                actor=actor,
            )
            reschedule_details.append(move)
            item.result_appointment_id = appointment.id
        else:
            appointment = Appointment(
                clinic_id=require_clinic_id(session),
                patient_id=item.patient_id,
                provider_id=item.provider_id,
                appointment_type_id=appointment_type.id,
                starts_at=rows[0].starts_at,
                ends_at=rows[-1].ends_at,
                duration_minutes=appointment_type.duration_minutes,
                status=AppointmentStatus.BOOKED.value,
                conversation_id=proposal.conversation_id,
                family_group_id=proposal.family_group_id,
                booking_group_id=group.id,
                visit_shape=proposal.visit_shape or _visit_shape(proposal),
                created_by=actor,
            )
            session.add(appointment)
            session.flush()
            _claim_rows(session, appointment.id, rows)
            item.result_appointment_id = appointment.id
        item.issue = None
        created.append(
            {
                "appointment_id": appointment.id,
                "patient_id": appointment.patient_id,
                "patient_name": item.patient_first_name,
                "provider": item.provider_name,
                "appointment_type": appointment_type.name,
                "appointment_type_code": appointment_type.code,
                "starts_at": appointment.starts_at.isoformat(timespec="minutes"),
                "ends_at": appointment.ends_at.isoformat(timespec="minutes"),
                "duration_minutes": appointment.duration_minutes,
                "status": appointment.status,
            }
        )

    proposal.status = ProposalStatus.ACCEPTED.value
    proposal.resolved_at = now
    proposal.confirmed_at = now
    proposal.confirm_key = confirm_key
    proposal.booking_group_id = group.id
    proposal.conflict_reason = None
    proposal.released_appointment_ids = sorted(set(released)) or None
    # Exactly what this confirmation did, written with the rows it describes.
    # ``kind`` and ``intent`` are captured too: a replay must report the action
    # that was taken, not the one the proposal happens to look like later.
    proposal.confirm_outcome = {
        "action": "cancelled" if kind == "cancellation" else ("moved" if reschedule_details else "created"),
        "kind": kind,
        "intent": proposal_intent(proposal),
        "appointment_ids": [entry["appointment_id"] for entry in created],
        "released_appointment_ids": sorted(set(released)),
    }
    group.details = {
        "appointments": created,
        "cancelled": cancelled,
        # Recorded in the same row the replay reads, so a repeated confirmation
        # describes the move from committed state rather than rebuilding it.
        "rescheduled": reschedule_details or None,
    }

    audit.record(
        session,
        action=f"proposal.confirmed.{kind}",
        actor_type="patient" if actor in {"patient", "ui"} else actor,
        entity_type="booking_proposal",
        entity_id=proposal.public_id,
        conversation_id=proposal.conversation_id,
        details={
            "booking_group": group.public_id,
            "appointments": [entry["appointment_id"] for entry in created],
            "released_appointments": sorted(set(released)),
            "fingerprint": fingerprint(created),
        },
        visibility="HOLD",
    )
    # Per-appointment entries: the staff dashboard's audit panel is keyed by
    # appointment id, so the move must be visible on both halves.
    for move in reschedule_details:
        _audit_reschedule(
            session,
            proposal=proposal,
            move=move,
            actor=actor,
        )

    trailer = {
        "conversation_id": proposal.conversation_id,
        "proposal_id": proposal.public_id,
        "key": f"confirm:{confirm_key}",
        "payload": {
            "booking_group": group.public_id,
            "appointments": [entry["appointment_id"] for entry in created],
            "released_appointments": sorted(set(released)),
        },
    }
    return (
        Outcome(
            ok=True,
            proposal_id=proposal.public_id,
            booking_group_id=group.public_id,
            appointments=created,
            released_appointment_ids=sorted(set(released)),
            rescheduled=reschedule_details,
        ),
        trailer,
    )


def _resolve_target_id(proposal: BookingProposal) -> int | None:
    """The one appointment a change proposal refers to, proposal-level first."""
    if proposal.target_appointment_id is not None:
        return proposal.target_appointment_id
    return next(
        (item.target_appointment_id for item in proposal.items if item.target_appointment_id),
        None,
    )


def _load_reschedule_originals(
    session: Session,
    proposal: BookingProposal,
    items: list[ProposalItem],
) -> dict[int, Appointment]:
    """The appointment this reschedule replaces, loaded under the write lock.

    A reschedule must name its original: the proposal, and every item, has to
    reference a real appointment that still exists and belongs to the patient
    whose conversation is confirming. A missing id means the move cannot be
    proven, and guessing would risk marking the wrong visit inactive -- so it is
    a conflict, not a silent no-op. Anything that fails here raises *before* a
    claim is released, which is what makes a rejected move a true no-op.
    """
    target_id = _resolve_target_id(proposal)
    if target_id is None:
        raise SlotConflictError(
            "This reschedule doesn't say which appointment to move. Nothing was changed."
        )
    for item in items:
        # The proposal-level target is authoritative: it is written when the
        # patient asks to move a visit, and never from a slot pick. An item that
        # disagrees with it is a corrupted row, not a second move.
        if item.target_appointment_id is not None and item.target_appointment_id != target_id:
            raise SlotConflictError("A reschedule can only move one appointment.")

    appointment = session.get(Appointment, target_id)
    if appointment is None:
        raise SlotConflictError("The appointment being moved no longer exists.")
    if appointment.status != AppointmentStatus.BOOKED.value or (
        appointment.rescheduled_to_id is not None
    ):
        # Checked under the write lock: a cancellation (or an earlier move) that
        # landed between "show me my appointments" and "move it" must not be
        # undone by a reschedule that silently revives the row.
        raise SlotConflictError(
            "That appointment is no longer booked, so it can't be moved. Nothing was changed."
        )
    # Ownership: the original must belong to the patient whose conversation is
    # confirming, and to every item that names it. A proposal replayed into
    # another thread would otherwise move somebody else's visit.
    if not _belongs_to_conversation_patient(proposal, appointment.patient_id):
        raise SlotConflictError("That appointment doesn't belong to this patient.")
    for item in items:
        if item.patient_id is not None and item.patient_id != appointment.patient_id:
            raise SlotConflictError("That appointment doesn't belong to this patient.")
    return {target_id: appointment}


def _create_reschedule_replacement(
    session: Session,
    *,
    proposal: BookingProposal,
    item: ProposalItem,
    original: Appointment,
    appointment_type: AppointmentType,
    rows: list[AvailabilitySlot],
    group: BookingGroup,
    now: datetime,
    actor: str,
) -> tuple[Appointment, dict[str, Any]]:
    """Reserve the replacement run, retire the original, and link the two.

    The original is *not* overwritten: it keeps its patient, time, provider and
    duration as history, flips to ``rescheduled``, and points at its
    replacement. The replacement is a new row -- with the original's patient and
    the selected provider/type/duration -- so a provider change can never be
    mistaken for "the same visit moved a few hours".
    """
    # The replacement's claims are inserted while the original's are already
    # released (step 3), so the partial unique index is the arbiter if another
    # writer slipped in between: the whole transaction rolls back on IntegrityError.
    replacement = Appointment(
        clinic_id=require_clinic_id(session),
        patient_id=original.patient_id,
        provider_id=item.provider_id or original.provider_id,
        appointment_type_id=appointment_type.id,
        starts_at=rows[0].starts_at,
        ends_at=rows[-1].ends_at,
        duration_minutes=appointment_type.duration_minutes,
        status=AppointmentStatus.BOOKED.value,
        reason=original.reason,
        family_group_id=original.family_group_id or proposal.family_group_id,
        conversation_id=proposal.conversation_id,
        booking_group_id=group.id,
        visit_shape=original.visit_shape,
        created_by=actor,
        rescheduled_from_id=original.id,
        rescheduled_from_starts_at=original.starts_at,
        rescheduled_from_provider_id=original.provider_id,
        rescheduled_from_appointment_type_id=original.appointment_type_id,
        rescheduled_from_duration_minutes=original.duration_minutes,
    )
    # Insert the replacement *before* touching the original. If another writer
    # took the interval after revalidation, this is where it surfaces -- and the
    # exception rolls back before the original has been marked at all.
    session.add(replacement)
    session.flush()
    _claim_rows(session, replacement.id, rows)

    original.status = AppointmentStatus.RESCHEDULED.value
    original.rescheduled_to_id = replacement.id
    original.rescheduled_at = now
    session.flush()

    move = {
        "original_appointment_id": original.id,
        "replacement_appointment_id": replacement.id,
        "patient_id": original.patient_id,
        "from_starts_at": original.starts_at.isoformat(timespec="minutes"),
        "to_starts_at": replacement.starts_at.isoformat(timespec="minutes"),
        "from_ends_at": original.ends_at.isoformat(timespec="minutes"),
        "to_ends_at": replacement.ends_at.isoformat(timespec="minutes"),
        "from_provider_id": original.rescheduled_from_provider_id or original.provider_id,
        "to_provider_id": replacement.provider_id,
        "appointment_type_code": appointment_type.code,
        "duration_minutes": replacement.duration_minutes,
    }
    return replacement, move


def _release_claims(session: Session, appointment_id: int, now: datetime) -> int:
    """Deactivate every active claim of one appointment. Returns the count."""
    rows = list(
        session.scalars(
            select(SlotClaim).where(
                SlotClaim.appointment_id == appointment_id,
                SlotClaim.active.is_(True),
            )
        )
    )
    for claim in rows:
        claim.active = False
        claim.released_at = now
        slot = session.get(AvailabilitySlot, claim.availability_slot_id)
        if slot is not None:
            slot.status = SlotStatus.AVAILABLE.value
    if rows:
        # Flush immediately so the partial unique index sees the release before
        # the replacement's claims are inserted in the same transaction.
        session.flush()
    return len(rows)


def _claim_rows(session: Session, appointment_id: int, rows: list[AvailabilitySlot]) -> None:
    for row in rows:
        row.status = SlotStatus.OCCUPIED.value
        session.add(
            SlotClaim(
                clinic_id=require_clinic_id(session),
                availability_slot_id=row.id,
                appointment_id=appointment_id,
                active=True,
            )
        )
    session.flush()


def _audit_reschedule(
    session: Session,
    *,
    proposal: BookingProposal,
    move: dict[str, Any],
    actor: str,
) -> None:
    """Write the move onto both appointment rows so either one explains it."""
    actor_type = "patient" if actor in {"patient", "ui"} else actor
    audit.record(
        session,
        action="appointment.rescheduled",
        actor_type=actor_type,
        entity_type="appointment",
        entity_id=move["original_appointment_id"],
        conversation_id=proposal.conversation_id,
        details={
            "from_starts_at": move["from_starts_at"],
            "to_starts_at": move["to_starts_at"],
            "to_appointment_id": move["replacement_appointment_id"],
            "proposal_id": proposal.public_id,
        },
    )
    audit.record(
        session,
        action="appointment.created.reschedule",
        actor_type=actor_type,
        entity_type="appointment",
        entity_id=move["replacement_appointment_id"],
        conversation_id=proposal.conversation_id,
        details={
            "from_appointment_id": move["original_appointment_id"],
            "from_starts_at": move["from_starts_at"],
            "starts_at": move["to_starts_at"],
            "proposal_id": proposal.public_id,
        },
    )


def _validate_semantics(proposal: BookingProposal) -> None:
    """Refuse a proposal whose recorded intent and kind disagree.

    The three kinds are not interchangeable, and confusing them is exactly the
    bug this guards: a ``booking`` has no original to retire (confirming it adds
    a visit), a ``reschedule`` must name the visit it moves, and a cancellation
    has nothing to book. A row that claims otherwise was tampered with or written
    by an older, buggy path -- refusing it is safer than guessing what it meant.
    """
    if proposal.kind == "reschedule":
        if proposal_intent(proposal) != "reschedule":
            raise ConfirmationRefused(
                "This proposal isn't a move of an existing appointment. Nothing was changed."
            )
        if _resolve_target_id(proposal) is None:
            raise ConfirmationRefused(
                "This reschedule doesn't say which appointment to move. Nothing was changed."
            )
    elif proposal.kind == "booking":
        if proposal_intent(proposal) != "new_booking":
            raise ConfirmationRefused("This proposal is not a new booking. Nothing was changed.")
        if proposal.target_appointment_id is not None or any(
            item.target_appointment_id is not None for item in proposal.items
        ):
            raise ConfirmationRefused(
                "A new booking never replaces an existing appointment. Nothing was changed."
            )


def _refuse_duplicate_booking(
    session: Session,
    proposal: BookingProposal,
    prepared: list[tuple[ProposalItem, list[AvailabilitySlot]]],
) -> None:
    """Refuse to book a visit the patient already has at that exact time.

    A new-booking proposal is an *additional* visit. If the patient already has a
    booked appointment of the same type at the same provider and start time, the
    "new" appointment would be the existing one all over again -- which is how a
    reschedule request that became a booking leaves two rows for one visit. The
    patient's own visit blocks the slot, so this only fires when the proposal was
    built around it (a stale card, or a tool handed the wrong ids).
    """
    for item, rows in prepared:
        if item.patient_id is None:
            continue
        duplicate = session.scalar(
            select(Appointment.id).where(
                Appointment.patient_id == item.patient_id,
                Appointment.status == AppointmentStatus.BOOKED.value,
                Appointment.provider_id == rows[0].provider_id,
                Appointment.starts_at == rows[0].starts_at,
                Appointment.appointment_type_id == item.appointment_type_id,
            )
        )
        if duplicate is not None:
            raise ConfirmationRefused(
                "That time is already your appointment, so booking it again would create a "
                "duplicate. Nothing was booked."
            )


def _belongs_to_conversation_patient(proposal: BookingProposal, patient_id: int) -> bool:
    """Whether ``patient_id`` is the person this conversation is verified as."""
    conversation = proposal.conversation
    if conversation is None or conversation.patient_id is None:
        # No binding yet (an unverified thread): nothing can be moved from it.
        return False
    return conversation.patient_id == patient_id


def _patient_allowed_on_proposal(
    session: Session, proposal: BookingProposal, patient_id: int
) -> bool:
    """Whether this conversation may confirm a visit for ``patient_id``.

    Solo bookings and reschedules are the verified patient only. A household
    booking may also include anyone else in the proposal's linked family group,
    including a spouse, matching ``check_family_authorisation`` when the hold was
    created. Applying the solo ownership rule to household items was rejecting a
    family member's time as ``slot_taken`` and superseding the hold on confirm.
    """
    if _belongs_to_conversation_patient(proposal, patient_id):
        return True
    if proposal.kind != "booking" or _visit_shape(proposal) != "household":
        return False
    conversation = proposal.conversation
    group_id = proposal.family_group_id
    if conversation is None or conversation.patient_id is None or group_id is None:
        return False
    owner_link = session.scalars(
        select(FamilyMember).where(
            FamilyMember.family_group_id == group_id,
            FamilyMember.patient_id == conversation.patient_id,
        )
    ).first()
    member_link = session.scalars(
        select(FamilyMember).where(
            FamilyMember.family_group_id == group_id,
            FamilyMember.patient_id == patient_id,
        )
    ).first()
    return owner_link is not None and member_link is not None


def _visit_shape(proposal: BookingProposal) -> str:
    """``solo`` or ``household``: one visit, or one per person in the party.

    Recorded on the proposal when it is created (see ``scheduling.propose_booking``).
    Older rows have no marker, so the party itself decides: a single named patient
    is a solo booking, several are a household booking.
    """
    recorded = (proposal.visit_shape or "").strip().lower()
    if recorded in {"solo", "household"}:
        return recorded
    people = {item.patient_id for item in proposal.items if item.patient_id is not None}
    return "household" if len(people) > 1 else "solo"


def _refuse_superseded_visit(
    session: Session,
    proposal: BookingProposal,
    prepared: list[tuple[ProposalItem, AppointmentType, list[AvailabilitySlot]]],
) -> None:
    """Refuse a second booking when the request already has one.

    A household or solo request may hold exactly one booking. If the patient
    confirmed one of its times already -- or the front desk booked them in the
    meantime -- every other card from that request is stale, even though its
    claims were released on confirmation and the slot still looks free.
    Confirming one of those would give the same party two visits they never
    asked for, so it is refused (and, like any refusal, nothing is written).
    """
    shape = _visit_shape(proposal)
    people = sorted({item.patient_id for item in proposal.items if item.patient_id is not None})
    if not people:
        return
    statement = select(Appointment.id, Appointment.visit_shape).where(
        Appointment.status == AppointmentStatus.BOOKED.value,
        Appointment.patient_id.in_(people),
        Appointment.created_at >= proposal.created_at,
    )
    expected = len(people) if shape == "household" else 1
    booked_here = sum(
        1
        for _appointment_id, booked_shape in session.execute(statement)
        if shape == "solo" or (booked_shape or "solo") != "solo"
    )
    if booked_here >= expected:
        raise ConfirmationRefused(
            "You already have a booking from this request, so this other time is no "
            "longer needed. Nothing new was booked; your confirmed appointment stands.",
            reason="already_booked",
        )
    if shape != "solo":
        return
    # "Still holding time" is a live row in ``slot_claims``, not a column on the
    # proposal: holds are written at creation and released on confirm, decline,
    # expiry, or a sibling booking. Reading the claim keeps this guard correct
    # even when ``expires_at``/status were drafted but the holds already went.
    live_hold = (
        select(SlotClaim.id)
        .where(
            SlotClaim.held_by_proposal_id == BookingProposal.id,
            SlotClaim.active.is_(True),
        )
        .exists()
    )
    hold = session.scalar(
        select(BookingProposal.id).where(
            BookingProposal.id != proposal.id,
            BookingProposal.conversation_id == proposal.conversation_id,
            BookingProposal.kind == "booking",
            BookingProposal.status == ProposalStatus.PENDING.value,
            BookingProposal.expires_at > utcnow(),
            BookingProposal.created_at < proposal.created_at,
            live_hold,
        )
    )
    if hold is None:
        return
    raise ConfirmationRefused(
        "A later time from this request is already waiting to be confirmed, so this earlier "
        "one would be a second visit. Nothing was booked.",
        reason="pending_booking_exists",
    )


def _patient_already_booked(
    session: Session,
    *,
    patient_id: int,
    starts_at: datetime,
    ends_at: datetime,
    ignore_appointment_id: int | None,
) -> bool:
    """True when the patient has another *booked* visit overlapping this run.

    The appointment being moved is excluded explicitly, so its own row can never
    make its replacement look like a double booking.
    """
    statement = select(Appointment.id).where(
        Appointment.patient_id == patient_id,
        Appointment.status == AppointmentStatus.BOOKED.value,
        Appointment.starts_at < ends_at,
        Appointment.ends_at > starts_at,
    )
    if ignore_appointment_id is not None:
        statement = statement.where(Appointment.id != ignore_appointment_id)
    return session.scalar(statement) is not None


def _revalidate(
    session: Session,
    proposal: BookingProposal,
    item: ProposalItem,
    *,
    originals: dict[int, Appointment] | None = None,
) -> tuple[ProposalItem, AppointmentType, list[AvailabilitySlot]] | None:
    """Re-check one item's interval under the write lock.

    ``originals`` is the reschedule target already proved movable by
    :func:`_load_reschedule_originals`; its claims and its own row are excluded
    from the availability and patient-conflict checks, because this transaction
    is about to release them. An ordinary booking has no originals, and a
    booking item must not name a target at all (see ``_validate_semantics``).
    """
    original = (originals or {}).get(item.target_appointment_id or 0)
    ignore_id = original.id if original is not None else None
    if item.provider_id is None or item.proposed_starts_at is None:
        item.issue = "This time is missing its provider or start time."
        return None
    if item.patient_id is not None and not _patient_allowed_on_proposal(
        session, proposal, item.patient_id
    ):
        # Reschedule/solo: only the conversation owner. Household booking: the
        # owner plus minors in the held family group. Rejecting children here
        # used to surface as a false slot_taken and retire the family card.
        item.issue = "This time belongs to a different patient."
        return None
    appointment_type = (
        session.get(AppointmentType, item.appointment_type_id)
        if item.appointment_type_id
        else None
    )
    if appointment_type is None:
        item.issue = "That appointment type is no longer offered."
        return None

    rows = is_interval_bookable(
        session,
        _clinic(session),
        provider_id=item.provider_id,
        starts_at=item.proposed_starts_at,
        duration_minutes=appointment_type.duration_minutes,
        appointment_type=appointment_type,
        # Our own hold must not count against us, and a reschedule target's
        # existing claim must not block its own replacement time. Intervals the
        # proposal relies on without holding are listed in held_slot_ids.
        self_hold_id=proposal.id,
        ignore_appointment_id=ignore_id,
        unheld_slot_ids=proposal.held_slot_ids,
    )
    if rows is None:
        item.issue = "That time is no longer available."
        return None
    if (
        item.patient_id is not None
        and ignore_id is not None
        and _patient_already_booked(
            session,
            patient_id=item.patient_id,
            starts_at=rows[0].starts_at,
            ends_at=rows[-1].ends_at,
            ignore_appointment_id=ignore_id,
        )
    ):
        # The move would leave the patient with two overlapping visits. The
        # original is untouched and the card stays live.
        item.issue = "That time overlaps another appointment."
        return None
    return item, appointment_type, rows


def _conflict_message(
    prepared: list[tuple[ProposalItem, AppointmentType, list[AvailabilitySlot]]],
    unavailable: list[str],
) -> str:
    if len(unavailable) == 1 and not prepared:
        return "That time was just taken. Nothing was booked. Here are current alternatives."
    return (
        f"{len(unavailable)} of {len(unavailable) + len(prepared)} times in this booking are no "
        "longer available, so nothing was booked. Here are current options."
    )


def _conflict_outcome(
    session: Session,
    proposal: BookingProposal,
    message: str,
    stable_slot_ids: list[str],
    *,
    reason: str = "slot_taken",
) -> Outcome:
    """Record why confirmation failed without consuming the proposal.

    A genuinely taken slot stays pending so the patient can retry. A semantic
    conflict cannot become valid by retrying the same card, so its holds are
    released and it is superseded. For a reschedule the rolled-back transaction
    never touched the original booking.

    ``reason`` is ``slot_taken`` (someone else has the time) or
    ``booking_conflict`` (the booking itself cannot be made: the visit is gone,
    already moved, duplicated, or not this patient's). Either way nothing was
    written.
    """
    annotated = False
    try:
        with write_transaction(session):
            fresh = session.get(BookingProposal, proposal.id)
            if fresh is not None and fresh.status == ProposalStatus.PENDING.value:
                fresh.conflict_reason = reason
                if reason != "slot_taken":
                    release_holds(session, fresh, utcnow())
                    fresh.status = ProposalStatus.SUPERSEDED.value
                    fresh.resolved_at = utcnow()
                annotated = True
    except Exception:  # noqa: BLE001 - annotation is best effort
        logger.warning("Could not annotate conflict on %s", proposal.public_id, exc_info=True)

    return Outcome(
        ok=False,
        error=reason,
        proposal_id=proposal.public_id,
        message=message,
        conflicts=[{"slot_id": slot_id, "annotated": annotated} for slot_id in stable_slot_ids],
        released_appointment_ids=[],
    )


def _replay_accepted(session: Session, proposal: BookingProposal) -> Outcome:
    """Describe what an accepted proposal already committed. Books nothing.

    The group's ``details`` is the record written in the confirming transaction,
    so a replay reports the same appointments, cancellations and moves as the
    first confirmation -- and is safe to call any number of times.
    """
    group = (
        session.get(BookingGroup, proposal.booking_group_id) if proposal.booking_group_id else None
    )
    appointments: list[dict[str, Any]] = []
    rescheduled: list[dict[str, Any]] = []
    released: list[int] = []
    cancelled: dict[str, Any] | None = None
    if isinstance(proposal.released_appointment_ids, list):
        released = [int(item) for item in proposal.released_appointment_ids]
    if group is not None and isinstance(group.details, dict):
        appointments = list(group.details.get("appointments") or [])
        rescheduled = list(group.details.get("rescheduled") or [])
        if isinstance(group.details.get("cancelled"), dict):
            cancelled = group.details["cancelled"]

    # The action is read from what the confirmation *did*. A move whose target
    # was already inactive would otherwise come back looking like a booking,
    # which is precisely the confusion this replay must not create.
    recorded = proposal.confirm_outcome if isinstance(proposal.confirm_outcome, dict) else {}
    action = recorded.get("action")
    if action is None:  # pre-marker rows: infer from the committed group
        if cancelled is not None:
            action = "cancelled"
        elif rescheduled:
            action = "moved"
        else:
            action = "created"
    if recorded.get("released_appointment_ids"):
        released = [int(item) for item in recorded["released_appointment_ids"]]

    if action == "cancelled" or proposal.kind == "cancellation":
        target_id = (
            cancelled.get("appointment_id")
            if isinstance(cancelled, dict)
            else proposal.target_appointment_id
        )
        return Outcome(
            ok=True,
            proposal_id=proposal.public_id,
            booking_group_id=group.public_id if group else None,
            message="This cancellation is already confirmed.",
            released_appointment_ids=[int(target_id)] if target_id is not None else released,
            replayed=True,
        )
    if action == "moved":
        return Outcome(
            ok=True,
            proposal_id=proposal.public_id,
            booking_group_id=group.public_id if group else None,
            appointments=appointments,
            released_appointment_ids=released,
            rescheduled=rescheduled,
            message="This new time is already confirmed. Your appointment was moved.",
            replayed=True,
        )
    return Outcome(
        ok=True,
        proposal_id=proposal.public_id,
        booking_group_id=group.public_id if group else None,
        appointments=appointments,
        released_appointment_ids=released,
        message="This booking is already confirmed.",
        replayed=True,
    )


def _resolve_confirm_key(
    session: Session,
    *,
    proposal: BookingProposal,
    confirm_key: str | None,
    actor: str,
    patient_session_id: int | None,
) -> str:
    """Pick the idempotency key for this confirmation.

    Precedence, and why:

    1. a key supplied by the caller (UI retry, ``confirm_key`` header), always
       honoured;
    2. a key this proposal already *completed* with, a genuine retry replays;
    3. a fresh random key.

    ``proposal.confirm_key`` alone is not enough. It is written by an attempt
    that may have been aborted before its booking group committed, so trusting
    it blindly makes the next attempt collide with a key that has no group to
    replay, which surfaces to the patient as a false "booked by someone else".
    The completed ``IdempotencyRecord`` is the record of "this key succeeded".
    """
    if confirm_key:
        return confirm_key

    recorded_key = proposal.confirm_key
    if recorded_key:
        completed = session.scalar(
            select(IdempotencyRecord.id).where(
                IdempotencyRecord.scope == IDEMPOTENCY_SCOPE,
                IdempotencyRecord.key == recorded_key,
                IdempotencyRecord.state == "complete",
            )
        )
        group = session.scalar(
            select(BookingGroup.id).where(BookingGroup.confirmation_key == recorded_key)
        )
        if completed is not None or group is not None:
            return recorded_key

    return new_confirmation_key()


def _record_confirm_key(
    session: Session,
    *,
    key: str,
    proposal: BookingProposal,
    actor: str,
    patient_session_id: int | None,
) -> None:
    """Mark ``key`` as used for this proposal. Runs inside the booking transaction.

    The UNIQUE(scope, key) index is the guard: a concurrent duplicate fails here
    (and is replayed by key) rather than creating a second booking group.
    """
    session.add(
        IdempotencyRecord(
            clinic_id=require_clinic_id(session),
            key=key,
            scope=IDEMPOTENCY_SCOPE,
            request_fingerprint=fingerprint(
                {
                    "proposal_id": proposal.public_id,
                    "actor": actor,
                    "patient_session_id": patient_session_id,
                }
            ),
            response_status=201,
            response_body=None,
            state="complete",
            owner_token=None,
            reset_generation=generations.get_generation(session) or "unknown",
            created_at=utcnow(),
            expires_at=None,
        )
    )
    session.flush()


def _replay_by_key(session: Session, confirm_key: str) -> Outcome | None:
    group = session.scalar(
        select(BookingGroup).where(BookingGroup.confirmation_key == confirm_key)
    )
    if group is None:
        return None
    proposal = session.get(BookingProposal, group.proposal_id) if group.proposal_id else None
    if proposal is None:
        return None
    return _replay_accepted(session, proposal)


def _write_trailer(
    *, conversation_id: int | None, proposal_id: str, key: str, payload: dict
) -> bool:
    """Record that the write truly committed (best effort, never fatal)."""
    try:
        with transactions.fresh_write_transaction() as session:
            audit.record(
                session,
                action="booking.commit",
                actor_type="system",
                entity_type="booking_proposal",
                entity_id=proposal_id,
                conversation_id=conversation_id,
                details={**payload, "trailer_key": key},
                visibility="COMMIT",
            )
        return True
    except Exception:  # noqa: BLE001 - diagnostics must not break the request
        logger.warning("Could not write commit trailer %s", key, exc_info=True)
        return False


__all__ = [
    "ConfirmationRefused",
    "DraftItem",
    "HOLD_MINUTES",
    "Outcome",
    "confirm_proposal",
    "create_proposal",
    "expire_proposal",
    "find_by_public_id",
    "fingerprint",
    "hold_deadline",
    "new_confirmation_key",
    "new_public_id",
    "pending_for_conversation",
    "proposal_intent",
    "reject_proposal",
    "release_expired_holds",
    "release_holds",
    "supersede_pending",
]
