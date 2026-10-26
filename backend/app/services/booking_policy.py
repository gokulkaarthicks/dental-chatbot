"""How many visits a booking covers, and which household members may be in it.

The receptionist has two shapes of appointment request:

* **Solo.** One person, one visit. One confirmation books exactly one time, and
  that person cannot be booked again at a time that overlaps it.
* **Family (group).** Several people, one visit each, held and confirmed
  together. One confirmation books every member or nobody.

Without this module the model could hold *three separate* bookings for one
patient and confirm them one by one, because each proposal on its own looked
valid. The rule that closes that hole is stated once, here:

    A booking request covers exactly one visit per person. A second visit for
    someone who already has one in this thread is a new booking only when the
    patient explicitly says they want an additional visit.

Everything is derived from SQLite (booked appointments, active holds, pending
proposals), never from model arguments, so the same rule applies whether the
patient clicked a card, replied in words, or the model called a tool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    BookingProposal,
    Patient,
    ProposalStatus,
    SlotClaim,
)
from app.tenancy import ClinicContext

logger = logging.getLogger(__name__)


def back_to_back_tolerance(clinic: ClinicContext) -> int:
    """Minutes between a visit and its neighbour that still count as back-to-back.

    One interval of slack, measured on *this* clinic's grid: at a 30-minute
    practice a 30-minute visit followed immediately by the next one is inside the
    window, and at a 20-minute practice the same holds at 20 minutes. A constant
    here would make tenant B's calendar look non-contiguous to itself.
    """
    return clinic.config.slot_minutes


@dataclass(frozen=True)
class HeldInterval:
    """One interval a proposal would hold, with the person it is for."""

    patient_id: int
    starts_at: datetime
    duration_minutes: int

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True)
class VisitInForce:
    """A visit this conversation already has: booked, or held and unconfirmed."""

    patient_id: int
    starts_at: datetime
    ends_at: datetime
    state: str  # "booked" | "held"
    kind: str  # "booking" | "reschedule"
    label: str
    appointment_id: int | None = None
    proposal_id: str | None = None


@dataclass
class BookingScope:
    """What a booking in this conversation is allowed to do."""

    covered_patient_ids: set[int] = field(default_factory=set)
    visits: list[VisitInForce] = field(default_factory=list)
    pending_booking: BookingProposal | None = None

    @property
    def family(self) -> bool:
        return len(self.covered_patient_ids) > 1

    @property
    def pending(self) -> bool:
        return self.pending_booking is not None

    @property
    def has_booked_visit(self) -> bool:
        return any(visit.state == "booked" for visit in self.visits)

    @property
    def mode(self) -> str:
        """``pending`` while a hold waits, otherwise ``family`` or ``solo``."""
        if self.pending_booking is not None:
            return "pending"
        return "family" if self.family else "solo"

    def desired_slot_count(self) -> int:
        """How many times this request needs, i.e. one per person covered."""
        return max(1, len(self.covered_patient_ids))

    def summary(self) -> dict[str, Any]:
        """A model-safe description, returned by ``get_booking_context``."""
        pending = self.pending_booking
        first = (
            min(pending.items, key=lambda item: item.order_index, default=None)
            if pending is not None
            else None
        )
        return {
            "mode": self.mode,
            "family": self.family,
            "covered_people": len(self.covered_patient_ids),
            "desired_slot_count": self.desired_slot_count(),
            "already_booked": self.has_booked_visit,
            "visits": [
                {
                    "patient_id": visit.patient_id,
                    "starts_at": visit.starts_at.isoformat(timespec="minutes"),
                    "ends_at": visit.ends_at.isoformat(timespec="minutes"),
                    "state": visit.state,
                    "kind": visit.kind,
                    "label": visit.label,
                    "appointment_id": visit.appointment_id,
                    "proposal_id": visit.proposal_id,
                }
                for visit in self.visits
            ],
            "pending_booking": (
                {
                    "proposal_id": pending.public_id,
                    "kind": pending.kind,
                    "slot_count": len(pending.items),
                    "people": sorted(
                        {
                            item.patient_first_name
                            for item in pending.items
                            if item.patient_first_name
                        }
                    ),
                    "starts_at": (
                        first.proposed_starts_at.isoformat(timespec="minutes")
                        if first is not None and first.proposed_starts_at
                        else None
                    ),
                }
                if pending is not None
                else None
            ),
        }


# --------------------------------------------------------------------------- #
# Reading state
# --------------------------------------------------------------------------- #


def _pending_booking(session: Session, conversation_id: int | None) -> BookingProposal | None:
    if conversation_id is None:
        return None
    from app.services.proposals import pending_for_conversation

    for proposal in pending_for_conversation(session, conversation_id):
        if proposal.kind == "booking" and proposal.status == ProposalStatus.PENDING.value:
            return proposal
    return None


def _booking_group_of(session: Session, appointment: Appointment):
    from app.models import BookingGroup

    if appointment.booking_group_id is None:
        return None
    return session.get(BookingGroup, appointment.booking_group_id)


def _belongs_to_conversation(
    session: Session, appointment: Appointment, conversation_id: int | None
) -> bool:
    """Whether a booked visit came out of this conversation's booking.

    A visit counts when it was created here, or when it came out of a reschedule
    made here. Either way it is the booking the patient is in the middle of,
    which is what stops a second overlapping or back-to-back hold.
    """
    if conversation_id is None:
        return False
    group = _booking_group_of(session, appointment)
    if group is not None and group.conversation_id == conversation_id:
        return True
    if appointment.rescheduled_from_id is not None:
        original = session.get(Appointment, appointment.rescheduled_from_id)
        source = _booking_group_of(session, original) if original is not None else None
        if source is not None and source.conversation_id == conversation_id:
            return True
    return False


def _appointment_type_name(session: Session, appointment: Appointment) -> str:
    if appointment.appointment_type_id is None:
        return "appointment"
    appointment_type = session.get(AppointmentType, appointment.appointment_type_id)
    return (appointment_type.name if appointment_type is not None else "appointment").lower()


def _first_name(session: Session, patient_id: int) -> str:
    patient = session.get(Patient, patient_id)
    return patient.full_name.split(" ")[0] if patient is not None else "Patient"


def booked_visits(
    session: Session,
    clinic: ClinicContext,
    patients: list[Patient],
    conversation_id: int | None,
) -> list[VisitInForce]:
    """Visits this booking request already has, live on the books right now.

    Booked rows are joined through their *active* slot claims, so a reschedule
    replacement (whose claims belong to the original until confirmation) is not
    mistaken for a second appointment.
    """
    if not patients:
        return []
    patient_ids = [patient.id for patient in patients]
    rows = list(
        session.scalars(
            select(Appointment)
            .join(SlotClaim, SlotClaim.appointment_id == Appointment.id)
            .where(
                Appointment.clinic_id == clinic.clinic_id,
                Appointment.patient_id.in_(patient_ids),
                Appointment.status == AppointmentStatus.BOOKED.value,
                SlotClaim.active.is_(True),
                SlotClaim.clinic_id == clinic.clinic_id,
            )
            .distinct()
            .order_by(Appointment.starts_at)
        )
    )
    visits: list[VisitInForce] = []
    for appointment in rows:
        if appointment.rescheduled_from_id is not None:
            continue
        if not _belongs_to_conversation(session, appointment, conversation_id):
            continue
        visits.append(
            VisitInForce(
                patient_id=appointment.patient_id,
                starts_at=appointment.starts_at,
                ends_at=appointment.ends_at,
                state="booked",
                kind="booking",
                label=(
                    f"{_first_name(session, appointment.patient_id)} has a "
                    f"{_appointment_type_name(session, appointment)}"
                ),
                appointment_id=appointment.id,
            )
        )
    return visits


def held_booking_visits(
    session: Session,
    clinic: ClinicContext,
    patients: list[Patient],
    conversation_id: int | None,
) -> list[VisitInForce]:
    """A still-pending *booking* hold, described as the visit it will become."""
    proposal = _pending_booking(session, conversation_id)
    if proposal is None:
        return []
    allowed = {patient.id for patient in patients}
    visits: list[VisitInForce] = []
    for item in sorted(proposal.items, key=lambda row: row.order_index):
        if item.patient_id is None or item.proposed_starts_at is None:
            continue
        if allowed and item.patient_id not in allowed:
            continue
        visits.append(
            VisitInForce(
                patient_id=item.patient_id,
                starts_at=item.proposed_starts_at,
                ends_at=item.proposed_starts_at
                + timedelta(
                    minutes=item.duration_minutes or clinic.config.slot_minutes
                ),
                state="held",
                kind="booking",
                label="held and waiting for your confirmation",
                proposal_id=proposal.public_id,
            )
        )
    return visits


def booking_scope(
    session: Session,
    clinic: ClinicContext,
    *,
    patients: list[Patient],
    conversation_id: int | None,
) -> BookingScope:
    """Everything a booking in this thread must respect, read from SQLite."""
    covered = {patient.id for patient in patients}
    pending = _pending_booking(session, conversation_id)
    if pending is not None:
        # A held proposal is authoritative for who the booking is for, so a
        # correction to the household link never forgets a person already held.
        covered.update(item.patient_id for item in pending.items if item.patient_id is not None)
    booked = booked_visits(session, clinic, patients, conversation_id)
    covered.update(visit.patient_id for visit in booked)
    held = held_booking_visits(session, clinic, patients, conversation_id)
    covered.update(visit.patient_id for visit in held)
    return BookingScope(
        covered_patient_ids=covered,
        visits=[*booked, *held],
        pending_booking=pending,
    )


# --------------------------------------------------------------------------- #
# Checks used by proposal creation
# --------------------------------------------------------------------------- #


def requests_additional_visit(summary: str | None) -> bool:
    """Whether the patient explicitly asked for a *separate* extra visit."""
    if not summary:
        return False
    lowered = summary.lower()
    return any(
        marker in lowered
        for marker in ("additional", "extra", "separate visit", "another visit", "second visit")
    )


def _overlap(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return start < other_end and end > other_start


def _overlapping_pair(scope: BookingScope, intervals: list[HeldInterval]) -> dict | None:
    """Two held intervals that collide, or a hold on top of an existing visit."""
    ordered = sorted(intervals, key=lambda item: item.starts_at)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if _overlap(current.starts_at, current.ends_at, previous.starts_at, previous.ends_at):
            return {
                "patient_id": current.patient_id,
                "starts_at": current.starts_at.isoformat(timespec="minutes"),
                "against": previous.starts_at.isoformat(timespec="minutes"),
            }
    for interval in ordered:
        for visit in scope.visits:
            if visit.patient_id != interval.patient_id:
                continue
            if _overlap(interval.starts_at, interval.ends_at, visit.starts_at, visit.ends_at):
                return {
                    "patient_id": interval.patient_id,
                    "starts_at": interval.starts_at.isoformat(timespec="minutes"),
                    "against": visit.starts_at.isoformat(timespec="minutes"),
                    "state": visit.state,
                }
    return None


def pending_card_message(proposal: BookingProposal) -> str:
    """What the patient reads when a hold is already waiting for them."""
    if proposal.kind == "reschedule":
        return (
            "A move is already held for you. Confirm that card to finish it, or tell me you "
            "want different times."
        )
    if proposal.kind == "cancellation":
        return "A cancellation is waiting for your confirmation. Confirm or keep it first."
    if len(proposal.items) > 1:
        return (
            "I already have your family's times held. Confirm that card to book everyone "
            "together, or ask me for different times."
        )
    return (
        "I already have that time held for you. Confirm the card to book it, or ask me for "
        "different times."
    )


def already_booked_message(scope: BookingScope) -> str:
    booked = [visit for visit in scope.visits if visit.state == "booked"]
    if not booked:
        return "This request is already booked."
    if len(booked) > 1:
        return (
            "That's already booked for everyone in this request. If you need something "
            "changed I can move a visit, or tell me if you want a separate additional visit."
        )
    when = booked[0].starts_at.strftime("%a, %b %-d at %-I:%M %p")
    return (
        f"You're already booked for {when}, so I won't hold a second time. I can move that "
        "visit if you'd prefer a different time."
    )


def refusal_for_hold(
    session: Session,
    *,
    scope: BookingScope,
    intervals: list[HeldInterval],
    summary: str | None,
) -> dict | None:
    """Why these new intervals must not be held, or ``None`` when they may be.

    Two rules, both about the *same* booking request:

    * **One visit per person.** A new hold may not add a second visit for
      somebody who already has one booked, or a second pending hold while one is
      waiting. An explicit "add another visit" (carried in ``summary``) relaxes
      it.
    * **No overlapping holds.** Two people may be back-to-back, never on top of
      each other.
    """
    del session  # state is already read into ``scope``
    if not intervals:
        return None
    wants_additional = requests_additional_visit(summary)

    if scope.pending_booking is not None and not wants_additional:
        pending = scope.pending_booking
        return {
            "ok": False,
            "error": "pending_booking_exists",
            "message": pending_card_message(pending),
            "pending_proposal_id": pending.public_id,
            "next_step": (
                "A booking is already held for this request. Ask the patient to confirm (or "
                "decline) that card instead of holding another time. To replace it, call "
                "change_booking_proposal with the new slot ids."
            ),
            "cards": [],
        }

    if scope.has_booked_visit and not wants_additional:
        return {
            "ok": False,
            "error": "already_booked",
            "message": already_booked_message(scope),
            "already_booked": True,
            "visits": [visit.label for visit in scope.visits if visit.state == "booked"],
            "next_step": (
                "This booking request is already booked. Do not hold another time. If the "
                "patient wants a different time for the same visit, call "
                "reschedule_appointment with the existing appointment_id. If they want a "
                "separate additional visit, ask them to say so, then pass additional_visit=true "
                "with a one-line summary."
            ),
            "cards": [],
        }

    clash = _overlapping_pair(scope, intervals)
    if clash is not None:
        return {
            "ok": False,
            "error": "overlapping_booking",
            "message": (
                "That time overlaps a visit already in this booking, so nothing new was "
                "held. Pick a time that doesn't collide, or change the booking you have."
            ),
            "conflicts": [clash],
            "next_step": (
                "Two visits in one booking may be back-to-back but never overlap. Offer a "
                "different time, or use change_booking_proposal to move the held one."
            ),
            "cards": [],
        }
    return None


# --------------------------------------------------------------------------- #
# Availability shaping
# --------------------------------------------------------------------------- #


def restrict_plain_slots(options: list, scope: BookingScope) -> list:
    """Hide ordinary new-booking times this request could not accept anyway.

    A time is hidden when the person it is for already has that visit in force,
    or when it falls in the middle of a visit already on the books: a mid-visit
    start can never be booked, so offering it would only produce a refusal later.
    """
    if not options:
        return []
    kept = []
    for option in options:
        start = option.starts_at
        end = option.ends_at
        patient_id = getattr(option, "patient_id", None)
        collides = any(
            _overlap(start, end, visit.starts_at, visit.ends_at)
            for visit in scope.visits
            if patient_id is not None and visit.patient_id == patient_id
        )
        if collides:
            continue
        inside = any(
            visit.state == "booked" and visit.starts_at < start < visit.ends_at
            for visit in scope.visits
        )
        if inside:
            continue
        kept.append(option)
    return kept


def keeps_family_shape(
    *, plans: list[list], scope: BookingScope, wants_family: bool
) -> bool:
    """Whether an answer still matches how many times this request needs.

    The rule the patient sees: for two people, a valid answer is a complete
    same-day family option with one open visit for each person, not a list of
    single times that could only ever book one of them.
    """
    if scope.pending:
        return True
    if wants_family:
        return bool(plans)
    if not scope.family:
        return True
    if len(scope.covered_patient_ids) < 2:
        return True
    return bool(plans)


__all__ = [
    "BookingScope",
    "HeldInterval",
    "VisitInForce",
    "already_booked_message",
    "back_to_back_tolerance",
    "booking_scope",
    "booked_visits",
    "held_booking_visits",
    "keeps_family_shape",
    "pending_card_message",
    "refusal_for_hold",
    "restrict_plain_slots",
]
