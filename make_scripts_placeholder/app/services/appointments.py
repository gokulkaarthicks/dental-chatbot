"""Patient appointment history, rescheduling, and cancellation.

Patient identity rules
----------------------
Every function takes ``patient_id`` chosen by the *caller from the verified
session*, never from model output. There is no lookup-by-id that skips the
ownership check, so cross-patient access is impossible by construction rather
than by convention.

Nothing changes until confirmation
----------------------------------
Reschedule and cancellation both go through proposals. ``propose_*`` only
records intent and (for a reschedule) holds the proposed interval. The original
appointment is modified exclusively inside the confirming transaction, so a
declined or failed reschedule preserves the original booking exactly.

A reschedule is not a booking
-----------------------------
``propose_reschedule`` always creates a proposal of kind ``reschedule`` that
references ``appointment_id``. It never goes through ``propose_booking``: a
booking proposal has no original to retire, so confirming it would add a second
appointment instead of moving the first. The confirmation (see
``app/services/proposals.py``) retires the original and creates the replacement
in a single transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    Provider,
)
from app.services.availability_engine import SlotOption, stable_slot_id
from app.services.proposals import (
    DraftItem,
    Outcome,
    create_proposal,
    pending_for_conversation,
    supersede_pending,
)
from app.timeutil import clinic_now

logger = logging.getLogger(__name__)


@dataclass
class AppointmentView:
    appointment: Appointment
    provider: Provider | None = None
    appointment_type: AppointmentType | None = None

    @property
    def is_upcoming(self) -> bool:
        """Booked, in the future, and *not* the retired half of a reschedule.

        A retired original points at its replacement, so this is what makes
        "your upcoming appointments" show one visit after a move rather than
        both halves of it.
        """
        return (
            self.appointment.status == AppointmentStatus.BOOKED.value
            and self.appointment.rescheduled_to_id is None
            and self.appointment.starts_at >= clinic_now()
        )

    @property
    def replaced_by_id(self) -> int | None:
        return self.appointment.rescheduled_to_id

    @property
    def replaces_id(self) -> int | None:
        return self.appointment.rescheduled_from_id

    @property
    def first_name(self) -> str:
        patient = self.appointment.patient
        return patient.full_name.split(" ")[0] if patient else "Patient"

    def as_dict(self) -> dict[str, Any]:
        appt = self.appointment
        return {
            "appointment_id": appt.id,
            "starts_at": appt.starts_at.isoformat(timespec="minutes"),
            "ends_at": appt.ends_at.isoformat(timespec="minutes"),
            "day": appt.starts_at.date().isoformat(),
            "time": appt.starts_at.strftime("%-I:%M %p"),
            "duration_minutes": appt.duration_minutes,
            "status": appt.status,
            "provider": self.provider.full_name if self.provider else None,
            "provider_credentials": self.provider.credentials if self.provider else None,
            "appointment_type": self.appointment_type.name if self.appointment_type else None,
            "appointment_type_code": self.appointment_type.code if self.appointment_type else None,
            "is_upcoming": self.is_upcoming,
            "rescheduled_to_id": self.replaced_by_id,
            "rescheduled_from_id": self.replaces_id,
        }


def _views(session: Session, statement) -> list[AppointmentView]:
    rows = session.scalars(
        statement.options(
            selectinload(Appointment.provider),
            selectinload(Appointment.appointment_type),
            selectinload(Appointment.patient),
        )
    ).all()
    return [
        AppointmentView(
            appointment=row, provider=row.provider, appointment_type=row.appointment_type
        )
        for row in rows
    ]


def list_for_patient(session: Session, patient_id: int) -> list[AppointmentView]:
    """Upcoming first (soonest first), then past (most recent first).

    A reschedule leaves two rows for one visit: the retired original and its
    replacement. Both stay in the history -- nothing is deleted -- while
    ``is_upcoming`` narrows the "upcoming" view to exactly one active booking.
    """
    statement = (
        select(Appointment)
        .where(Appointment.patient_id == patient_id)
        .order_by(Appointment.starts_at)
    )
    views = _views(session, statement)
    upcoming = [view for view in views if view.is_upcoming]
    past = [view for view in views if not view.is_upcoming]
    past.sort(key=lambda view: view.appointment.starts_at, reverse=True)
    return upcoming + past


def upcoming_for_patient(session: Session, patient_id: int) -> list[AppointmentView]:
    return [view for view in list_for_patient(session, patient_id) if view.is_upcoming]


def active_for_patient(session: Session, patient_id: int) -> list[AppointmentView]:
    """The patient's live bookings: booked, future, not retired by a reschedule.

    One function for "exactly one active appointment remains", shared by the
    listing card and the tools so they cannot disagree.
    """
    return upcoming_for_patient(session, patient_id)


def owned_appointment(
    session: Session, patient_id: int, appointment_id: int
) -> AppointmentView | None:
    """The appointment only if ``patient_id`` owns it. Otherwise ``None``.

    Returning ``None`` for a mismatched owner (rather than a distinct error) is
    intentional: the caller cannot distinguish "not yours" from "does not
    exist", so appointment ids cannot be probed across patients.
    """
    row = session.get(Appointment, appointment_id)
    if row is None or row.patient_id != patient_id:
        return None
    return AppointmentView(
        appointment=row, provider=row.provider, appointment_type=row.appointment_type
    )


def _matches_hint(view: AppointmentView, needle: str) -> bool:
    """One appointment against a natural-language selector."""
    appt = view.appointment
    return (
        needle in {appt.starts_at.date().isoformat(), appt.starts_at.strftime("%A").lower()}
        or needle in appt.starts_at.strftime("%A %B %-d").lower()
        or needle == appt.starts_at.strftime("%-I:%M %p").lower()
    )


def pick_appointment(views: list[AppointmentView], hint: str | None) -> AppointmentView | None:
    """Resolve "which appointment" from a natural-language hint.

    Accepted hints: an appointment id, an ISO date (``2026-11-03``), a weekday
    name, or ``"next"``/``"soonest"``. Returns ``None`` when ambiguous so the
    caller can ask instead of guessing.
    """
    if not views:
        return None
    if len(views) == 1 and not hint:
        return views[0]
    needle = (hint or "").strip().lower()
    if not needle:
        return None
    if needle in {"next", "soonest", "first", "upcoming"}:
        return views[0]
    if needle.isdigit():
        for view in views:
            if view.appointment.id == int(needle):
                return view
        return None

    matches = [view for view in views if _matches_hint(view, needle)]
    if len(matches) == 1:
        return matches[0]
    return None


def _snapshot_option(view: AppointmentView, *, starts_at: datetime | None = None) -> SlotOption:
    """SlotOption-shaped snapshot used to render a proposal item."""
    appt = view.appointment
    start = starts_at or appt.starts_at
    provider = view.provider
    appt_type = view.appointment_type
    return SlotOption(
        slot_ids=[],
        stable_id=stable_slot_id(appt.provider_id, start),
        provider_id=appt.provider_id,
        provider_name=provider.full_name if provider else "Clinic",
        provider_credentials=provider.credentials if provider else "",
        provider_specialties=provider.specialty_names if provider else [],
        appointment_type_code=appt_type.code if appt_type else "",
        appointment_type_name=appt_type.name if appt_type else "",
        starts_at=start,
        ends_at=start + timedelta(minutes=appt.duration_minutes),
        duration_minutes=appt.duration_minutes,
        patient_id=appt.patient_id,
        patient_first_name=view.first_name,
    )


def propose_cancellation(
    session: Session,
    *,
    conversation_id: int,
    patient_id: int,
    view: AppointmentView,
    reason: str | None = None,
) -> Outcome:
    """Record intent to cancel. The appointment stays booked until confirmed."""
    appt = view.appointment
    if appt.status != AppointmentStatus.BOOKED.value:
        return Outcome(
            ok=False,
            error="not_booked",
            message="That appointment is already cancelled, so there's nothing to change.",
        )
    if view.appointment_type is None:
        return Outcome(ok=False, error="invalid", message="I can't find that appointment.")

    # Repeated cancellation requests replace the older pending one instead of
    # stacking several cancellation cards for the same visit.
    supersede_pending(session, conversation_id=conversation_id, kinds=("cancellation",))
    return create_proposal(
        session,
        conversation_id=conversation_id,
        kind="cancellation",
        intent="cancellation",
        summary=reason,
        target_appointment_id=appt.id,
        cancellation_reason=reason,
        items=[
            DraftItem(
                patient_id=patient_id,
                patient_first_name=view.first_name,
                appointment_type=view.appointment_type,
                option=_snapshot_option(view),
                target_appointment_id=appt.id,
            )
        ],
    )


def propose_reschedule(
    session: Session,
    *,
    conversation_id: int,
    patient_id: int,
    view: AppointmentView,
    option: SlotOption,
    appointment_type: AppointmentType,
) -> Outcome:
    """Record intent to move ``view`` to ``option``. Original stays booked.

    This is the *only* entry point for a reschedule. It always produces a
    proposal of kind ``reschedule`` whose top-level and per-item
    ``appointment_id`` reference the existing booking, so confirmation retires
    that exact row instead of booking a second appointment.

    ``patient_id`` and ``view`` come from the caller's verified session, and the
    view is only ever produced by ``owned_appointment``/``upcoming_for_patient``,
    so an id belonging to somebody else can never be moved here.
    """
    appt = view.appointment
    if appt.patient_id != patient_id:
        # Defensive: the caller must pass the appointment it already proved the
        # patient owns. A mismatch is never a reschedule we are willing to make.
        return Outcome(
            ok=False,
            error="unknown_appointment",
            message="I can't find that appointment.",
        )
    if appt.status != AppointmentStatus.BOOKED.value:
        if appt.status == AppointmentStatus.RESCHEDULED.value:
            message = "That appointment was already moved. Tell me which visit you mean."
        else:
            message = "That appointment is no longer booked, so there's nothing to move."
        return Outcome(
            ok=False,
            error="not_booked",
            message=message,
            appointment_unchanged=True,
        )
    if appt.rescheduled_to_id is not None:
        # Already retired by a move: proposing from it would release time the
        # replacement relies on.
        return Outcome(
            ok=False,
            error="not_booked",
            message="That appointment was already moved. Tell me which visit you mean.",
            appointment_unchanged=True,
        )
    if view.appointment_type is None:
        return Outcome(ok=False, error="invalid", message="I can't find that appointment.")

    # A newer time is held by a newer proposal; also drop any pending
    # cancellation for this visit, so the patient cannot later confirm a stale
    # "cancel it" card against the appointment they just moved.
    # A newer time replaces any pending move or cancellation for this visit, and
    # any pending *new* booking too: adding a second visit while the patient is
    # moving this one is never what they meant, and an old hold on the
    # replacement time would otherwise block this proposal.
    supersede_pending(
        session,
        conversation_id=conversation_id,
        kinds=("booking", "reschedule", "cancellation"),
        target_appointment_ids={appt.id},
    )
    return create_proposal(
        session,
        conversation_id=conversation_id,
        kind="reschedule",
        intent="reschedule",
        summary=f"Move {view.appointment_type.name.lower()} from "
        f"{appt.starts_at.strftime('%a, %b %-d at %-I:%M %p')}.",
        target_appointment_id=appt.id,
        items=[
            DraftItem(
                patient_id=patient_id,
                patient_first_name=view.first_name,
                appointment_type=appointment_type,
                option=option,
                target_appointment_id=appt.id,
            )
        ],
    )


class RescheduleTargetError(Exception):
    """The appointment to move could not be identified or is not movable."""

    def __init__(self, code: str, message: str, *, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def _movable(view: AppointmentView) -> bool:
    appt = view.appointment
    return appt.status == AppointmentStatus.BOOKED.value and appt.rescheduled_to_id is None


def resolve_reschedule_target(
    session: Session,
    *,
    patient_id: int,
    appointment_id: int | None = None,
    which: str | None = None,
) -> AppointmentView:
    """The one existing appointment a reschedule is about, for this patient.

    Ownership is proved here and only here, from the verified patient id the
    caller took from the session:

    * an explicit ``appointment_id`` must belong to this patient (a foreign id is
      reported exactly like a missing one, so ids cannot be probed);
    * a natural hint is matched against *this patient's* upcoming visits only;
    * with several visits and no hint, the caller must ask rather than guess.

    The returned view is the only thing ``propose_reschedule`` will accept, so a
    model cannot name an appointment it has not been shown.
    """
    if appointment_id is not None:
        view = owned_appointment(session, patient_id, appointment_id)
        if view is None:
            raise RescheduleTargetError(
                "unknown_appointment", "I can't find that appointment."
            )
        if view.appointment.status == AppointmentStatus.RESCHEDULED.value:
            raise RescheduleTargetError(
                "already_moved",
                "That appointment was already moved. Tell me which visit you mean.",
                extra={"appointment_unchanged": True},
            )
        if not _movable(view):
            raise RescheduleTargetError(
                "not_booked",
                "That appointment is no longer booked, so there's nothing to move.",
                extra={"appointment_unchanged": True},
            )
        return view

    upcoming = upcoming_for_patient(session, patient_id)
    if not upcoming:
        raise RescheduleTargetError(
            "no_active_booking",
            "You don't have an upcoming appointment to move. I can book a new one.",
        )
    chosen = pick_appointment(upcoming, which)
    if chosen is not None:
        return chosen
    if len(upcoming) > 1:
        raise RescheduleTargetError(
            "multiple_appointments",
            "There are several upcoming appointments. Which one should I move?",
            extra={"appointments": [view.as_dict() for view in upcoming]},
        )
    return upcoming[0]


def pending_flow(session: Session, conversation_id: int, kind: str) -> str | None:
    """Public id of the pending proposal of ``kind``, if any."""
    for proposal in pending_for_conversation(session, conversation_id):
        if proposal.kind == kind:
            return proposal.public_id
    return None


__all__ = [
    "AppointmentView",
    "RescheduleTargetError",
    "active_for_patient",
    "list_for_patient",
    "owned_appointment",
    "pending_flow",
    "pick_appointment",
    "propose_cancellation",
    "propose_reschedule",
    "resolve_reschedule_target",
    "upcoming_for_patient",
]
