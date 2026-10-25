"""Read-only availability queries.

Booking itself is Phase 3. Phase 2 only needs to *show* open times, and only
after registration is complete -- enforced in the tool layer, not here, so this
module stays a pure query surface.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AvailabilitySlot,
    Provider,
    SlotStatus,
)
from app.timeutil import (
    CLOSE_TIME,
    OPEN_TIME,
    SLOT_MINUTES,
    clinic_now,
    clinic_today,
    is_open_weekday,
)

MAX_RANGE_DAYS = 14
DEFAULT_RANGE_DAYS = 7


def _coerce_day(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return fallback


def _coerce_type(code: str | None) -> str | None:
    if not code:
        return None
    normalized = code.strip().lower()
    aliases = {
        "clean": "cleaning",
        "cleaning": "cleaning",
        "hygiene": "cleaning",
        "checkup": "checkup",
        "check_up": "checkup",
        "exam": "checkup",
        "general_checkup": "checkup",
        "emergency": "emergency",
        "urgent": "emergency",
    }
    return aliases.get(normalized)


def slot_grid_block() -> dict:
    """Describe the grid so the model can explain options accurately."""
    return {
        "slot_minutes": SLOT_MINUTES,
        "open": OPEN_TIME.strftime("%H:%M"),
        "close": CLOSE_TIME.strftime("%H:%M"),
        "days": "Mon-Sat",
        "closed_days": ["Sun"],
    }


def find_availability(
    session: Session,
    *,
    start_date: str | None = None,
    days: int = DEFAULT_RANGE_DAYS,
    appointment_type: str | None = None,
    provider_name: str | None = None,
    limit: int = 8,
    patient_id: int | None = None,
) -> dict:
    """Return the next open intervals, grouped by day.

    ``appointment_type`` filters to visits that fit in one contiguous run of
    free slots for the same provider (cleanings need two 30-minute intervals).
    """
    now = clinic_now()
    today = clinic_today()
    span = max(1, min(days, MAX_RANGE_DAYS))
    first_day = _coerce_day(start_date, today)
    if first_day < today:
        first_day = today
        span = min(span, MAX_RANGE_DAYS)

    type_code = _coerce_type(appointment_type)
    type_row = None
    if type_code:
        type_row = session.scalar(select(AppointmentType).where(AppointmentType.code == type_code))
        if type_row is None:
            return {
                "ok": False,
                "error": "unknown_appointment_type",
                "message": "I can look up cleanings, checkups, or emergency visits.",
            }
    required_slots = 1
    if type_row is not None:
        required_slots = max(1, type_row.duration_minutes // SLOT_MINUTES)

    # A patient already on the books must be offered a time that does not clash
    # with their own visit, even though Phase 2 does not book anything yet.
    existing_visits: list[dict] = []
    blocked_ranges: list[tuple[datetime, datetime]] = []
    if patient_id is not None:
        existing_visits = booked_visits(session, patient_id, limit=5)
        for visit in existing_visits:
            blocked_ranges.append(
                (
                    datetime.fromisoformat(visit["starts_at"]),
                    datetime.fromisoformat(visit["starts_at"])
                    + timedelta(minutes=visit["duration_minutes"]),
                )
            )

    query = (
        select(AvailabilitySlot, Provider)
        .join(Provider, Provider.id == AvailabilitySlot.provider_id)
        .where(
            AvailabilitySlot.status == SlotStatus.AVAILABLE.value,
            AvailabilitySlot.starts_at >= max(datetime.combine(first_day, time.min), now),
            AvailabilitySlot.starts_at
            < datetime.combine(first_day + timedelta(days=span), CLOSE_TIME),
        )
        .order_by(AvailabilitySlot.starts_at, AvailabilitySlot.provider_id)
    )
    if provider_name:
        query = query.where(Provider.full_name.ilike(f"%{provider_name.strip()}%"))

    rows = session.execute(query).all()

    # Build contiguous runs per provider so multi-interval visits are offered.
    grouped: dict[int, list[AvailabilitySlot]] = {}
    providers: dict[int, Provider] = {}
    for slot, provider in rows:
        grouped.setdefault(provider.id, []).append(slot)
        providers[provider.id] = provider

    offers: list[dict] = []
    for provider_id, slots in grouped.items():
        provider = providers[provider_id]
        if type_row is not None and type_row.required_specialty:
            if type_row.required_specialty.lower() not in {
                name.lower() for name in provider.specialty_names
            }:
                continue
        index = 0
        while index < len(slots):
            run: list[AvailabilitySlot] = [slots[index]]
            cursor = index + 1
            while (
                cursor < len(slots)
                and slots[cursor].starts_at == run[-1].starts_at + timedelta(minutes=SLOT_MINUTES)
                and len(run) < required_slots
            ):
                run.append(slots[cursor])
                cursor += 1
            index += 1
            if len(run) != required_slots:
                continue
            run_start, run_end = run[0].starts_at, run[-1].ends_at
            if any(run_start < end and start < run_end for start, end in blocked_ranges):
                continue
            offers.append(
                {
                    "starts_at": run_start.isoformat(timespec="minutes"),
                    "ends_at": run_end.isoformat(timespec="minutes"),
                    "provider": provider.full_name,
                    "provider_credentials": provider.credentials,
                    "specialties": provider.specialty_names,
                    "duration_minutes": len(run) * SLOT_MINUTES,
                }
            )

    offers.sort(key=lambda item: item["starts_at"])

    today_offers: list[dict] = []
    by_day: dict[str, list[dict]] = {}
    for offer in offers:
        day = offer["starts_at"][:10]
        if day == today.isoformat():
            today_offers.append(offer)
            continue
        by_day.setdefault(day, []).append(offer)

    limited: list[dict] = []
    for day in sorted(by_day):
        for offer in by_day[day][:3]:
            if len(limited) >= limit:
                break
            limited.append(offer)
        if len(limited) >= limit:
            break

    if not limited and not today_offers:
        return {
            "ok": True,
            "available": False,
            "type": type_code,
            "range": {"from": first_day.isoformat(), "days": span},
            "grid": slot_grid_block(),
            "existing_visits": existing_visits,
            "note": (
                "No open times in that window. Tell the patient you can look at "
                "later dates or have the front desk call them."
            ),
        }

    return {
        "ok": True,
        "available": True,
        "type": type_code,
        "type_name": type_row.name if type_row else None,
        "range": {"from": first_day.isoformat(), "days": span},
        "grid": slot_grid_block(),
        "clinic_today": clinic_today().isoformat(),
        "clinic_now": now.isoformat(timespec="minutes"),
        "today_urgent_only": [offer for offer in today_offers[:3]],
        "options": limited,
        "existing_visits": existing_visits,
        "note": (
            "These are open times only. Do not say anything is booked; a confirmation "
            "comes from the front desk."
        ),
    }


def booked_visits(session: Session, patient_id: int, limit: int = 5) -> list[dict]:
    """The patient's own upcoming visits. Only call for a verified patient."""
    rows = session.execute(
        select(Appointment, Provider, AppointmentType)
        .join(Provider, Provider.id == Appointment.provider_id)
        .join(AppointmentType, AppointmentType.id == Appointment.appointment_type_id)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.status == AppointmentStatus.BOOKED.value,
            Appointment.starts_at >= clinic_now(),
        )
        .order_by(Appointment.starts_at)
        .limit(limit)
    ).all()
    return [
        {
            "starts_at": appointment.starts_at.isoformat(timespec="minutes"),
            "duration_minutes": appointment.duration_minutes,
            "provider": provider.full_name,
            "type": appointment_type.name,
            "status": appointment.status,
        }
        for appointment, provider, appointment_type in rows
    ]


def scheduling_context_summary(session: Session) -> dict:
    """Compact facts the system prompt can embed without a tool call."""
    today = clinic_today()
    next_open = session.scalar(
        select(func.min(AvailabilitySlot.starts_at)).where(
            AvailabilitySlot.status == SlotStatus.AVAILABLE.value,
            AvailabilitySlot.starts_at >= clinic_now(),
        )
    )
    return {
        "clinic_today": today.isoformat(),
        "clinic_now": clinic_now().isoformat(timespec="minutes"),
        "clinic_is_open_today": is_open_weekday(today),
        "next_open_slot": next_open.isoformat(timespec="minutes") if next_open else None,
    }
