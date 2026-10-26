"""Aggregate, non-sensitive demo statistics for ``/api/demo/status``.

Counts and generations only -- never names, phone numbers, DOBs, or detailed
appointment contents, because this endpoint is public.

Every number here belongs to *one* clinic. The session is bound to a tenant by
the request's credential, and the clinic's own grid (slot length, horizon, today)
is read from its row, so tenant 2's status describes a 20-minute Eastern calendar
rather than echoing the first demo practice's constants.
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
from app.tenancy import ClinicContext
from app.timeutil import day_label, utc_now_iso

UPCOMING_WINDOW_DAYS = 60


def _scalar(session: Session, statement) -> int:
    return int(session.scalar(statement) or 0)


def _clinic_day_bounds(config, day: date) -> tuple[datetime, datetime]:
    """One business day on the tenant's own calendar.

    "Today" is the clinic's date, not the server's: a San Francisco practice is
    still mid-day while a Brooklyn one has closed.
    """
    start = datetime.combine(day, datetime.min.time())
    del config  # bounds are calendar days; the config names *which* day is today
    return start, start + timedelta(days=1)


def _next_available_slot(session: Session, clinic: ClinicContext, now: datetime) -> datetime | None:
    return session.scalar(
        select(func.min(AvailabilitySlot.starts_at))
        .where(
            AvailabilitySlot.clinic_id == clinic.clinic_id,
            AvailabilitySlot.status == SlotStatus.AVAILABLE.value,
            AvailabilitySlot.starts_at >= now,
        )
        # ``func.min`` is a projection, which the ORM criteria net does not
        # rewrite, so the tenant predicate above is the one that matters.
        .execution_options(tenant_scoped=False)
    )


def build_demo_status(session: Session, clinic: ClinicContext) -> dict:
    config = clinic.config
    now = config.now()
    today = config.today()
    horizon_end = now + timedelta(days=UPCOMING_WINDOW_DAYS)
    day_start, day_end = _clinic_day_bounds(config, today)

    slot_counts = dict(
        session.execute(
            select(AvailabilitySlot.status, func.count())
            .where(AvailabilitySlot.clinic_id == clinic.clinic_id)
            .group_by(AvailabilitySlot.status)
            .execution_options(tenant_scoped=False)
        ).all()
    )
    appointment_counts = dict(
        session.execute(
            select(Appointment.status, func.count())
            .where(Appointment.clinic_id == clinic.clinic_id)
            .group_by(Appointment.status)
            .execution_options(tenant_scoped=False)
        ).all()
    )

    upcoming = _scalar(
        session,
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.clinic_id == clinic.clinic_id,
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
            Appointment.clinic_id == clinic.clinic_id,
            Appointment.status == AppointmentStatus.BOOKED.value,
            Appointment.starts_at >= day_start,
            Appointment.starts_at < day_end,
        ),
    )

    active_proposals = _scalar(
        session,
        select(func.count())
        .select_from(BookingProposal)
        .where(
            BookingProposal.clinic_id == clinic.clinic_id,
            BookingProposal.status == ProposalStatus.PENDING.value,
        ),
    )
    open_alerts = _scalar(
        session,
        select(func.count())
        .select_from(PendingRequest)
        .where(
            PendingRequest.clinic_id == clinic.clinic_id,
            PendingRequest.status == RequestStatus.PENDING.value,
        ),
    )

    reset_generation = generations.get_generation(session, clinic_id=clinic.clinic_id)
    last_seeded_at = generations.get_last_seeded_at(session, clinic_id=clinic.clinic_id)
    next_slot = _next_available_slot(session, clinic, now)

    counts = {
        "providers": _scalar(
            session,
            select(func.count())
            .select_from(Provider)
            .where(Provider.clinic_id == clinic.clinic_id),
        ),
        "patients": _scalar(
            session,
            select(func.count())
            .select_from(Patient)
            .where(Patient.clinic_id == clinic.clinic_id),
        ),
        "family_groups": _scalar(
            session,
            select(func.count())
            .select_from(FamilyGroup)
            .where(FamilyGroup.clinic_id == clinic.clinic_id),
        ),
        "appointments": appointment_counts.get(AppointmentStatus.BOOKED.value, 0)
        + appointment_counts.get(AppointmentStatus.COMPLETED.value, 0),
        "upcoming_appointments": upcoming,
        "today_appointments": today_appointments,
        "availability_slots": sum(int(value) for value in slot_counts.values()),
        "available_slots": int(slot_counts.get(SlotStatus.AVAILABLE.value, 0)),
        "occupied_slots": int(slot_counts.get(SlotStatus.OCCUPIED.value, 0)),
        "staff_users": _scalar(
            session,
            select(func.count())
            .select_from(StaffUser)
            .where(StaffUser.clinic_id == clinic.clinic_id, StaffUser.active.is_(True)),
        ),
        "conversations": _scalar(
            session,
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.clinic_id == clinic.clinic_id),
        ),
        "messages": _scalar(
            session,
            select(func.count())
            .select_from(Message)
            .where(Message.clinic_id == clinic.clinic_id),
        ),
        "pending_proposals": active_proposals,
        "pending_requests": open_alerts,
        "audit_records": count_rows(session, "audit_records", clinic.clinic_id),
    }

    return {
        "generated_at": utc_now_iso(),
        "clinic_slug": clinic.slug,
        "clinic_id": clinic.clinic_id,
        "clinic_name": clinic.name,
        "reset_generation": reset_generation,
        "reset_count": generations.get_reset_count(session, clinic_id=clinic.clinic_id),
        "last_reset_at": generations.get_last_reset_at(session, clinic_id=clinic.clinic_id),
        "last_seeded_at": last_seeded_at,
        "seeded": last_seeded_at is not None,
        "database_empty": seed.is_empty_database(session),
        "clinic_today": today.isoformat(),
        "clinic_today_label": day_label(today),
        "clinic_now": now.isoformat(timespec="seconds"),
        "clinic_now_offset": config.now_iso_with_offset(),
        "horizon_days": config.seed_horizon_days,
        "slot_minutes": config.slot_minutes,
        "counts": counts,
        "fixtures": {
            "fixture_set": FIXTURE_SET,
            "fixture_version": FIXTURE_VERSION,
            "counts": fixture_counts(seed_plan_for(clinic)),
        },
        "next_available_slot": (next_slot.isoformat(timespec="minutes") if next_slot else None),
    }


def seed_plan_for(clinic: ClinicContext):
    """The tenant's fixture table, when the clinic is one of the shipped demos.

    A clinic created through ``create-clinic`` has no fixture plan, so its demo
    status reports no static counts rather than tenant 1's.
    """
    from app.seed_tenants import plan_for_slug

    plan = plan_for_slug(clinic.slug)
    return plan.fixtures if plan is not None else None


__all__ = ["build_demo_status"]
