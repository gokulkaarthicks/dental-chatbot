"""Staff-side appointment management.

Same rules as the patient chatbot, different entry point
--------------------------------------------------------
Staff are trusted to act, but they are **not** trusted to bypass the schedule.
Every create, reschedule, and cancellation here goes through exactly the same
primitives the patient flow uses:

* :func:`app.services.availability_engine.is_interval_bookable` validates
  business hours, provider eligibility, duration, contiguity, and existing
  claims -- the identical check that guards a patient confirmation.
* ``slot_claims`` carries the partial unique index, so a staff booking racing a
  patient confirmation still resolves to "someone already has that time".
* :func:`app.services.transactions.write_transaction` takes ``BEGIN IMMEDIATE``
  so the check and the write cannot interleave with another writer.

The structural difference is that staff do not need a *proposal*: they act
directly, in a single transaction, and the audit row is written inside that same
transaction. A staff booking therefore either exists **with** its audit record or
does not exist at all.

What this module will not do
----------------------------
* Create an overlapping booking for the same patient (the chatbot already
  refuses this; staff get the same rule so the dashboard cannot double-book).
* Move an appointment without first proving the new time is free. A failed
  reschedule leaves the original exactly as it was, because the original claims
  are only released after validation succeeds.
* Record patient health or contact details in the audit trail -- ids, codes and
  timestamps only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db import require_clinic_id
from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AuditRecord,
    BookingGroup,
    Patient,
    Provider,
    SlotClaim,
    StaffUser,
    utcnow,
)
from app.services import audit, generations
from app.services.availability_engine import (
    is_interval_bookable,
    resolve_appointment_type,
    stable_slot_id,
)
from app.services.proposals import new_public_id
from app.services.transactions import SlotConflictError, WriteTransactionError, write_transaction
from app.tenancy import ClinicContext

logger = logging.getLogger(__name__)

MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 100


class StaffActionError(Exception):
    """A staff action that could not be performed, carrying a safe message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AppointmentFilters:
    """What the dashboard asked for. Every field is optional and additive."""

    day: date | None = None
    day_from: date | None = None
    day_to: date | None = None
    provider_id: int | None = None
    status: str | None = None
    search: str | None = None
    upcoming_only: bool = False
    limit: int = DEFAULT_LIST_LIMIT


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def appointment_dict(clinic: ClinicContext, row: Appointment) -> dict[str, Any]:
    """One appointment in the shape the dashboard table renders.

    Patient contact details are included because this payload is only ever
    served to an authenticated staff session. It is not a public schema.

    ``clinic`` supplies "now": whether a visit still counts as upcoming depends
    on the practice's own timezone, not the server's.
    """
    patient = row.patient
    provider = row.provider
    appt_type = row.appointment_type
    now = clinic.config.now()
    return {
        "id": row.id,
        "starts_at": row.starts_at.isoformat(timespec="minutes"),
        "ends_at": row.ends_at.isoformat(timespec="minutes"),
        "day": row.starts_at.date().isoformat(),
        "day_label": row.starts_at.strftime("%a, %b %-d"),
        "time_label": row.starts_at.strftime("%-I:%M %p"),
        "end_time_label": row.ends_at.strftime("%-I:%M %p"),
        "duration_minutes": row.duration_minutes,
        "status": row.status,
        "is_upcoming": row.status == AppointmentStatus.BOOKED.value
        and row.rescheduled_to_id is None
        and row.starts_at >= now,
        "is_past": row.starts_at < now,
        "created_by": row.created_by,
        "reason": row.reason,
        "cancelled_at": row.cancelled_at.isoformat(timespec="seconds")
        if row.cancelled_at
        else None,
        # Reschedule history: the retired original names its replacement, and the
        # replacement names the visit it came from. The dashboard uses these to
        # show a move rather than two separate bookings.
        "rescheduled_at": row.rescheduled_at.isoformat(timespec="seconds")
        if row.rescheduled_at
        else None,
        "rescheduled_to_id": row.rescheduled_to_id,
        "rescheduled_from_id": row.rescheduled_from_id,
        "rescheduled_from_starts_at": row.rescheduled_from_starts_at.isoformat(
            timespec="minutes"
        )
        if row.rescheduled_from_starts_at
        else None,
        "rescheduled_from_provider_id": row.rescheduled_from_provider_id,
        "rescheduled_from_appointment_type_id": row.rescheduled_from_appointment_type_id,
        "rescheduled_from_duration_minutes": row.rescheduled_from_duration_minutes,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
        "patient": {
            "id": patient.id,
            "full_name": patient.full_name,
            "first_name": patient.full_name.split(" ")[0] if patient.full_name else "",
            "phone": patient.phone,
            "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
            "insurance_provider": patient.insurance_provider,
            "insurance_member_id": patient.insurance_member_id,
            "notes": patient.notes,
        }
        if patient
        else None,
        "provider": {
            "id": provider.id,
            "full_name": provider.full_name,
            "credentials": provider.credentials,
        }
        if provider
        else None,
        "appointment_type": {
            "id": appt_type.id,
            "code": appt_type.code,
            "name": appt_type.name,
            "duration_minutes": appt_type.duration_minutes,
            "is_emergency": appt_type.is_emergency,
        }
        if appt_type
        else None,
        "booking_group_id": row.booking_group_id,
    }


def _base_query() -> Select:
    return select(Appointment).options(
        selectinload(Appointment.patient),
        selectinload(Appointment.provider),
        selectinload(Appointment.appointment_type),
    )


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def list_appointments(session: Session, clinic: ClinicContext, filters: AppointmentFilters) -> list[Appointment]:
    """Filtered list, ordered the way a front desk reads it.

    A specific day (or an explicit end bound) reads chronologically. The default
    view puts upcoming visits first, soonest up, then past visits most-recent
    first, so today's work is at the top of the table.
    """
    statement = _base_query()
    explicit_day = filters.day is not None or filters.day_to is not None

    if filters.day is not None:
        start = datetime.combine(filters.day, datetime.min.time())
        statement = statement.where(
            Appointment.starts_at >= start,
            Appointment.starts_at < start + timedelta(days=1),
        )
    else:
        if filters.day_from is not None:
            statement = statement.where(
                Appointment.starts_at >= datetime.combine(filters.day_from, datetime.min.time())
            )
        if filters.day_to is not None:
            statement = statement.where(
                Appointment.starts_at
                < datetime.combine(filters.day_to, datetime.min.time()) + timedelta(days=1)
            )

    if filters.provider_id is not None:
        statement = statement.where(Appointment.provider_id == filters.provider_id)
    if filters.status:
        statement = statement.where(Appointment.status == filters.status)
    if filters.upcoming_only:
        # "Upcoming" means live bookings only: a rescheduled original is history
        # (its replacement is the live row), so it must not appear twice.
        statement = statement.where(
            Appointment.status == AppointmentStatus.BOOKED.value,
            Appointment.rescheduled_to_id.is_(None),
            Appointment.starts_at >= clinic.config.now(),
        )
    if filters.search:
        needle = f"%{filters.search.strip().lower()}%"
        statement = statement.join(Patient).where(
            or_(
                func.lower(Patient.full_name).like(needle),
                func.lower(Patient.phone).like(needle),
            )
        )

    statement = statement.order_by(Appointment.starts_at)
    statement = statement.limit(max(1, min(filters.limit, MAX_LIST_LIMIT)))
    rows = list(session.scalars(statement).unique())

    if not explicit_day:
        now = clinic.config.now()
        upcoming = [row for row in rows if row.starts_at >= now]
        past = [row for row in rows if row.starts_at < now]
        past.sort(key=lambda row: row.starts_at, reverse=True)
        rows = upcoming + past
    return rows


def get_appointment(session: Session, appointment_id: int) -> Appointment | None:
    return session.scalar(_base_query().where(Appointment.id == appointment_id))


def list_appointments_by_patient(
    session: Session, clinic: ClinicContext, patient_id: int
) -> list[Appointment]:
    """One patient's visits, soonest upcoming first, then most recent past."""
    rows = list(
        session.scalars(
            _base_query()
            .where(Appointment.patient_id == patient_id)
            .order_by(Appointment.starts_at)
        ).unique()
    )
    now = clinic.config.now()
    upcoming = [row for row in rows if row.starts_at >= now]
    past = [row for row in rows if row.starts_at < now]
    past.sort(key=lambda row: row.starts_at, reverse=True)
    return upcoming + past


def appointment_history(session: Session, appointment_id: int) -> list[dict[str, Any]]:
    """Audit trail for one appointment, newest first.

    Returns rows keyed to this appointment id, so the panel shows what was
    actually recorded rather than a derived narrative.
    """
    rows = session.scalars(
        select(AuditRecord)
        .where(
            AuditRecord.entity_id == str(appointment_id),
            AuditRecord.entity_type == "appointment",
        )
        .order_by(AuditRecord.id.desc())
        .limit(25)
    )
    return [
        {
            "action": row.action,
            "actor_type": row.actor_type,
            "actor_id": row.actor_id,
            "occurred_at": row.occurred_at.isoformat(timespec="seconds")
            if row.occurred_at
            else None,
            "details": row.details,
        }
        for row in rows
    ]


def provider_options(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(Provider).where(Provider.active.is_(True)).order_by(Provider.full_name)
    )
    return [{"id": row.id, "full_name": row.full_name, "credentials": row.credentials} for row in rows]


def appointment_type_options(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(AppointmentType).order_by(AppointmentType.name))
    return [
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "duration_minutes": row.duration_minutes,
            "is_emergency": row.is_emergency,
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #


def _audit(
    session: Session,
    *,
    action: str,
    staff: StaffUser,
    appointment: Appointment,
    details: dict[str, Any],
) -> None:
    """Record a staff action inside the caller's transaction.

    ``actor_type`` is ``staff`` (distinct from the assistant's trail) and
    ``actor_id`` is the staff user id. Details carry ids, codes and timestamps
    only -- never patient health or contact information.
    """
    audit.record(
        session,
        action=action,
        actor_type="staff",
        actor_id=staff.id,
        entity_type="appointment",
        entity_id=appointment.id,
        conversation_id=appointment.conversation_id,
        details={"staff_username": staff.username or staff.email, **details},
    )


def _patient_conflict(
    session: Session,
    clinic: ClinicContext,
    *,
    patient_id: int,
    starts_at: datetime,
    ends_at: datetime,
    ignore_appointment_id: int | None = None,
) -> bool:
    """True when this patient already has a booked visit overlapping the interval."""
    statement = select(Appointment.id).where(
        Appointment.clinic_id == clinic.clinic_id,
        Appointment.patient_id == patient_id,
        Appointment.status == AppointmentStatus.BOOKED.value,
        Appointment.starts_at < ends_at,
        Appointment.ends_at > starts_at,
    )
    if ignore_appointment_id is not None:
        statement = statement.where(Appointment.id != ignore_appointment_id)
    return session.scalar(statement) is not None


def _resolve_patient(session: Session, patient_id: int) -> Patient:
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise StaffActionError("unknown_patient", "That patient could not be found.")
    return patient


def _resolve_type(
    session: Session, clinic: ClinicContext, code_or_id: int | str | None
) -> AppointmentType:
    # The tenant is passed through rather than left to the session's filter:
    # ``code`` is only unique inside one clinic, and the dashboard can be pointed
    # at a clinic by header, so the lookup has to be scoped to the same tenant the
    # rest of the action is validated against.
    appointment_type, _ = resolve_appointment_type(
        session, clinic, None if code_or_id is None else str(code_or_id)
    )
    if appointment_type is None:
        raise StaffActionError("unknown_type", "That visit type is not offered.")
    return appointment_type


def _resolve_provider(session: Session, provider_id: int) -> Provider:
    provider = session.get(Provider, provider_id)
    if provider is None or not provider.active:
        raise StaffActionError("unknown_provider", "That dentist is not available.")
    return provider


def _validate_new_time(
    session: Session,
    clinic: ClinicContext,
    *,
    patient_id: int,
    provider_id: int,
    starts_at: datetime,
    appointment_type: AppointmentType,
    ignore_appointment_id: int | None = None,
) -> list:
    rows = is_interval_bookable(
        session,
        clinic,
        provider_id=provider_id,
        starts_at=starts_at,
        duration_minutes=appointment_type.duration_minutes,
        appointment_type=appointment_type,
        ignore_appointment_id=ignore_appointment_id,
    )
    if rows is None:
        raise StaffActionError(
            "slot_unavailable",
            "That time is no longer open. Pick another slot and try again.",
        )
    if _patient_conflict(
        session,
        clinic,
        patient_id=patient_id,
        starts_at=rows[0].starts_at,
        ends_at=rows[-1].ends_at,
        ignore_appointment_id=ignore_appointment_id,
    ):
        raise StaffActionError(
            "patient_conflict",
            "That patient already has an appointment overlapping this time.",
        )
    return rows


def create_appointment(
    session: Session,
    clinic: ClinicContext,
    *,
    staff: StaffUser,
    patient_id: int,
    provider_id: int,
    starts_at: datetime,
    appointment_type_code: str | int | None = "checkup",
    reason: str | None = None,
) -> Appointment:
    """Book a new appointment: validate, claim, audit, commit -- atomically."""
    patient = _resolve_patient(session, patient_id)
    provider = _resolve_provider(session, provider_id)
    appointment_type = _resolve_type(session, clinic, appointment_type_code)

    try:
        with write_transaction(session) as txn:
            rows = _validate_new_time(
                session,
                clinic,
                patient_id=patient.id,
                provider_id=provider.id,
                starts_at=starts_at,
                appointment_type=appointment_type,
            )
            now = utcnow()
            group = BookingGroup(
                clinic_id=require_clinic_id(session),
                public_id=new_public_id("grp"),
                conversation_id=None,
                proposal_id=None,
                patient_session_id=None,
                confirmation_key=f"staff-{new_public_id('cf')}",
                kind="staff_booking",
                family_group_id=None,
                summary=f"Booked by {staff.display_name}",
                reset_generation=generations.get_generation(session) or "unknown",
            )
            session.add(group)
            session.flush()

            appointment = Appointment(
                clinic_id=require_clinic_id(session),
                patient_id=patient.id,
                provider_id=provider.id,
                appointment_type_id=appointment_type.id,
                starts_at=rows[0].starts_at,
                ends_at=rows[-1].ends_at,
                duration_minutes=appointment_type.duration_minutes,
                status=AppointmentStatus.BOOKED.value,
                reason=reason,
                booking_group_id=group.id,
                created_by=f"staff:{staff.id}",
            )
            session.add(appointment)
            session.flush()

            for row in rows:
                session.add(
                    SlotClaim(
                        clinic_id=require_clinic_id(session),
                        availability_slot_id=row.id,
                        appointment_id=appointment.id,
                        active=True,
                    )
                )
            group.details = {
                "appointments": [
                    {
                        "appointment_id": appointment.id,
                        "patient_id": patient.id,
                        "starts_at": appointment.starts_at.isoformat(timespec="minutes"),
                    }
                ],
                "created_by_staff_id": staff.id,
                "created_at": now.isoformat(timespec="seconds"),
            }
            _audit(
                session,
                action="appointment.created.staff",
                staff=staff,
                appointment=appointment,
                details={
                    "patient_id": patient.id,
                    "provider_id": provider.id,
                    "appointment_type": appointment_type.code,
                    "starts_at": appointment.starts_at.isoformat(timespec="minutes"),
                    "booking_group": group.public_id,
                },
            )
            txn.commit()
    except StaffActionError:
        raise
    except SlotConflictError as exc:
        raise StaffActionError("slot_unavailable", str(exc)) from exc
    except WriteTransactionError as exc:
        raise StaffActionError("busy", "The schedule is busy. Try again.") from exc

    return get_appointment(session, appointment.id) or appointment


def reschedule_appointment(
    session: Session,
    clinic: ClinicContext,
    *,
    staff: StaffUser,
    appointment_id: int,
    starts_at: datetime,
    provider_id: int | None = None,
    appointment_type_code: str | int | None = None,
) -> Appointment:
    """Move an existing appointment. On any failure the original is untouched.

    Same shape as the patient confirmation: the new time is validated first,
    then the original is retired (``rescheduled``, linked to its replacement) and
    a new booked row is created for the new time. The dashboard therefore shows
    the visit's history on both rows instead of a single row whose old time
    vanished, and the "active appointment" count stays at one.
    """
    existing = get_appointment(session, appointment_id)
    if existing is None:
        raise StaffActionError("unknown_appointment", "That appointment no longer exists.")
    if existing.status == AppointmentStatus.CANCELLED.value:
        raise StaffActionError("not_booked", "A cancelled appointment cannot be moved.")
    if existing.status == AppointmentStatus.RESCHEDULED.value or existing.rescheduled_to_id:
        raise StaffActionError(
            "not_booked",
            "That appointment was already moved; edit its replacement instead.",
        )

    original_start = existing.starts_at
    original_provider = existing.provider_id
    original_type_id = existing.appointment_type_id
    original_duration = existing.duration_minutes
    target_provider_id = provider_id or existing.provider_id
    appointment_type = (
        _resolve_type(session, clinic, appointment_type_code)
        if appointment_type_code
        else existing.appointment_type
    )
    if appointment_type is None:
        raise StaffActionError("unknown_type", "That appointment type is no longer available.")

    try:
        with write_transaction(session) as txn:
            # Validation happens *before* any claim is released, so a rejected
            # move cannot leave the original appointment partially released.
            rows = _validate_new_time(
                session,
                clinic,
                patient_id=existing.patient_id,
                provider_id=target_provider_id,
                starts_at=starts_at,
                appointment_type=appointment_type,
                ignore_appointment_id=existing.id,
            )
            now = utcnow()
            for claim in session.scalars(
                select(SlotClaim).where(
                    SlotClaim.appointment_id == existing.id,
                    SlotClaim.active.is_(True),
                )
            ):
                claim.active = False
                claim.released_at = now
            session.flush()

            replacement = Appointment(
                clinic_id=require_clinic_id(session),
                patient_id=existing.patient_id,
                provider_id=target_provider_id,
                appointment_type_id=appointment_type.id,
                starts_at=rows[0].starts_at,
                ends_at=rows[-1].ends_at,
                duration_minutes=appointment_type.duration_minutes,
                status=AppointmentStatus.BOOKED.value,
                reason=existing.reason,
                family_group_id=existing.family_group_id,
                conversation_id=existing.conversation_id,
                booking_group_id=existing.booking_group_id,
                created_by=f"staff:{staff.id}",
                rescheduled_from_id=existing.id,
                rescheduled_from_starts_at=original_start,
                rescheduled_from_provider_id=original_provider,
                rescheduled_from_appointment_type_id=original_type_id,
                rescheduled_from_duration_minutes=original_duration,
            )
            session.add(replacement)
            session.flush()
            for row in rows:
                session.add(
                    SlotClaim(
                        clinic_id=require_clinic_id(session),
                        availability_slot_id=row.id,
                        appointment_id=replacement.id,
                        active=True,
                    )
                )

            existing.status = AppointmentStatus.RESCHEDULED.value
            existing.rescheduled_to_id = replacement.id
            existing.rescheduled_at = now

            _audit(
                session,
                action="appointment.rescheduled.staff",
                staff=staff,
                appointment=existing,
                details={
                    "from_starts_at": original_start.isoformat(timespec="minutes"),
                    "to_starts_at": replacement.starts_at.isoformat(timespec="minutes"),
                    "from_provider_id": original_provider,
                    "to_provider_id": target_provider_id,
                    "appointment_type": appointment_type.code,
                    "replacement_appointment_id": replacement.id,
                },
            )
            _audit(
                session,
                action="appointment.created.reschedule",
                staff=staff,
                appointment=replacement,
                details={
                    "from_appointment_id": existing.id,
                    "from_starts_at": original_start.isoformat(timespec="minutes"),
                    "starts_at": replacement.starts_at.isoformat(timespec="minutes"),
                },
            )
            txn.commit()
    except StaffActionError:
        raise
    except SlotConflictError as exc:
        raise StaffActionError("slot_unavailable", str(exc)) from exc
    except WriteTransactionError as exc:
        raise StaffActionError("busy", "The schedule is busy. Try again.") from exc

    return get_appointment(session, replacement.id) or replacement


def cancel_appointment(
    session: Session,
    *,
    staff: StaffUser,
    appointment_id: int,
    reason: str | None = None,
) -> Appointment:
    """Cancel an appointment and release its intervals."""
    existing = get_appointment(session, appointment_id)
    if existing is None:
        raise StaffActionError("unknown_appointment", "That appointment no longer exists.")
    if existing.status == AppointmentStatus.CANCELLED.value:
        # Idempotent: a double click is not an error the desk needs to solve.
        return existing

    try:
        with write_transaction(session) as txn:
            now = utcnow()
            existing.status = AppointmentStatus.CANCELLED.value
            existing.cancelled_at = now
            if reason:
                existing.reason = reason
            for claim in session.scalars(
                select(SlotClaim).where(
                    SlotClaim.appointment_id == existing.id,
                    SlotClaim.active.is_(True),
                )
            ):
                claim.active = False
                claim.released_at = now
            _audit(
                session,
                action="appointment.cancelled.staff",
                staff=staff,
                appointment=existing,
                details={
                    "starts_at": existing.starts_at.isoformat(timespec="minutes"),
                    "reason": reason,
                },
            )
            txn.commit()
    except WriteTransactionError as exc:
        raise StaffActionError("busy", "The schedule is busy. Try again.") from exc

    return get_appointment(session, appointment_id) or existing


def complete_appointment(session: Session, *, staff: StaffUser, appointment_id: int) -> Appointment:
    """Mark a finished visit completed, keeping the record and claim history."""
    existing = get_appointment(session, appointment_id)
    if existing is None:
        raise StaffActionError("unknown_appointment", "That appointment no longer exists.")
    if existing.status != AppointmentStatus.BOOKED.value:
        raise StaffActionError("not_booked", "Only a booked appointment can be completed.")
    try:
        with write_transaction(session) as txn:
            existing.status = AppointmentStatus.COMPLETED.value
            _audit(
                session,
                action="appointment.completed.staff",
                staff=staff,
                appointment=existing,
                details={"starts_at": existing.starts_at.isoformat(timespec="minutes")},
            )
            txn.commit()
    except WriteTransactionError as exc:
        raise StaffActionError("busy", "The schedule is busy. Try again.") from exc
    return get_appointment(session, appointment_id) or existing


def available_slots(
    session: Session,
    clinic: ClinicContext,
    *,
    provider_id: int | None,
    appointment_type_code: str | int | None,
    day: date,
) -> list[dict[str, Any]]:
    """Open start times for the booking form on one day.

    Read-only: it walks the grid, asks the same ``is_interval_bookable`` used
    everywhere else, and returns only times that are genuinely free right now.
    Nothing is held, so showing options never blocks a patient.
    """
    appointment_type = _resolve_type(session, clinic, appointment_type_code)
    providers = (
        [_resolve_provider(session, provider_id)]
        if provider_id is not None
        else list(
            session.scalars(
                select(Provider).where(Provider.active.is_(True)).order_by(Provider.full_name)
            )
        )
    )
    results: list[dict[str, Any]] = []
    for provider in providers:
        for start in clinic.config.day_slot_starts(day):
            rows = is_interval_bookable(
                session,
                clinic,
                provider_id=provider.id,
                starts_at=start,
                duration_minutes=appointment_type.duration_minutes,
                appointment_type=appointment_type,
            )
            if rows is None:
                continue
            results.append(
                {
                    "starts_at": rows[0].starts_at.isoformat(timespec="minutes"),
                    "ends_at": rows[-1].ends_at.isoformat(timespec="minutes"),
                    "time_label": rows[0].starts_at.strftime("%-I:%M %p"),
                    "slots_needed": len(rows),
                    "provider_id": provider.id,
                    "provider_name": provider.full_name,
                    "stable_slot_id": stable_slot_id(provider.id, rows[0].starts_at),
                }
            )
    results.sort(key=lambda item: (item["starts_at"], item["provider_name"]))
    return results


__all__ = [
    "AppointmentFilters",
    "StaffActionError",
    "appointment_dict",
    "appointment_history",
    "appointment_type_options",
    "available_slots",
    "cancel_appointment",
    "complete_appointment",
    "create_appointment",
    "get_appointment",
    "list_appointments",
    "list_appointments_by_patient",
    "provider_options",
    "reschedule_appointment",
]
