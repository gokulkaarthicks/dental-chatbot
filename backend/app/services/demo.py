"""Aggregate, non-sensitive demo statistics for ``/api/demo/status``.

Counts and generations only -- never names, phone numbers, DOBs, or detailed
appointment contents, because this endpoint is public.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import count_rows
from app.models import (
    Appointment,
    AppointmentStatus,
    AvailabilitySlot,
    BookingProposal,
    Conversation,
    FamilyGroup,
    Message,
    Patient,
    PendingRequest,
    ProposalStatus,
    Provider,
    RequestStatus,
    SlotStatus,
    StaffUser,
)
from app.seed_fixtures import FIXTURE_SET, FIXTURE_VERSION, fixture_counts
from app.services import generations, seed
from app.timeutil import (
    SEED_HORIZON_DAYS,
    SLOT_MINUTES,
    clinic_day_label,
    clinic_now,
    clinic_now_iso_with_offset,
    clinic_today,
    local_now_iso,
)

UPCOMING_WINDOW_DAYS = 60


def _scalar(session: Session, statement) -> int:
    return int(session.scalar(statement) or 0)


def _clinic_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time())
    return start, start + timedelta(days=1)


def _next_available_slot(session: Session, now: datetime) -> datetime | None:
    return session.scalar(
        select(func.min(AvailabilitySlot.starts_at)).where(
            AvailabilitySlot.status == SlotStatus.AVAILABLE.value,
            AvailabilitySlot.starts_at >= now,
        )
    )


def build_demo_status(session: Session) -> dict:
    now = clinic_now()
    today = clinic_today()
    horizon_end = now + timedelta(days=UPCOMING_WINDOW_DAYS)
    day_start, day_end = _clinic_day_bounds(today)

    slot_counts = dict(
        session.execute(
            select(AvailabilitySlot.status, func.count()).group_by(AvailabilitySlot.status)
        ).all()
    )
    appointment_counts = dict(
        session.execute(
            select(Appointment.status, func.count()).group_by(Appointment.status)
        ).all()
    )

    upcoming = _scalar(
        session,
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.status == AppointmentStatus.BOOKED.value,
            Appointment.starts_at >= now,
            Appointment.starts_at < horizon_end,
        ),
    )
    today_appointments = _scalar(
        session,
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.status == AppointmentStatus.BOOKED.value,
            Appointment.starts_at >= day_start,
            Appointment.starts_at < day_end,
        ),
    )

    active_proposals = _scalar(
        session,
        select(func.count())
        .select_from(BookingProposal)
        .where(BookingProposal.status == ProposalStatus.PENDING.value),
    )
    open_alerts = _scalar(
        session,
        select(func.count())
        .select_from(PendingRequest)
        .where(PendingRequest.status == RequestStatus.PENDING.value),
    )

    reset_generation = generations.get_generation(session)
    last_seeded_at = generations.get_last_seeded_at(session)
    next_slot = _next_available_slot(session, now)

    return {
        "generated_at": local_now_iso(),
        "reset_generation": reset_generation,
        "reset_count": generations.get_reset_count(session),
        "last_reset_at": generations.get_last_reset_at(session),
        "last_seeded_at": last_seeded_at,
        "seeded": last_seeded_at is not None,
        "database_empty": seed.is_empty_database(session),
        "clinic_today": today.isoformat(),
        "clinic_today_label": clinic_day_label(today),
        "clinic_now": now.isoformat(timespec="seconds"),
        "clinic_now_offset": clinic_now_iso_with_offset(),
        "horizon_days": SEED_HORIZON_DAYS,
        "slot_minutes": SLOT_MINUTES,
        "counts": {
            "providers": _scalar(session, select(func.count()).select_from(Provider)),
            "patients": _scalar(session, select(func.count()).select_from(Patient)),
            "family_groups": _scalar(session, select(func.count()).select_from(FamilyGroup)),
            "appointments": appointment_counts.get(AppointmentStatus.BOOKED.value, 0)
            + appointment_counts.get(AppointmentStatus.COMPLETED.value, 0),
            "upcoming_appointments": upcoming,
            "today_appointments": today_appointments,
            "availability_slots": sum(int(value) for value in slot_counts.values()),
            "available_slots": int(slot_counts.get(SlotStatus.AVAILABLE.value, 0)),
            "occupied_slots": int(slot_counts.get(SlotStatus.OCCUPIED.value, 0)),
            "staff_users": _scalar(
                session,
                select(func.count()).select_from(StaffUser).where(StaffUser.active.is_(True)),
            ),
            "conversations": _scalar(session, select(func.count()).select_from(Conversation)),
            "messages": _scalar(session, select(func.count()).select_from(Message)),
            "pending_proposals": active_proposals,
            "pending_requests": open_alerts,
            "audit_records": count_rows(session, "audit_records"),
        },
        "fixtures": {
            "fixture_set": FIXTURE_SET,
            "fixture_version": FIXTURE_VERSION,
            "counts": fixture_counts(),
        },
        "next_available_slot": (next_slot.isoformat(timespec="minutes") if next_slot else None),
    }
