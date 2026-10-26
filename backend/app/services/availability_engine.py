"""Availability search and slot validation.

This module is read-only. It answers "what could be booked?" and "is this exact
interval still bookable?"; the booking itself lives in ``app/services/proposals.py``
and ``app/services/scheduling.py`` and runs under ``BEGIN IMMEDIATE``.

Invariants implemented here
---------------------------
* A slot is offered only when ``status == available`` *and* no active claim
  references it. Held slots are therefore never advertised.
* An appointment occupies a **run** of contiguous 30-minute intervals for one
  provider. Every interval in the run must be free and must belong to the same
  provider and the same business day.
* Identical (provider, start) options are emitted once.
* Patient preferences (days, time windows, weekday filters, requested provider)
  narrow the result. Nothing is widened to make the answer look better.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AvailabilitySlot,
    Patient,
    Provider,
    SlotClaim,
    SlotStatus,
)
from app.observability import log_event
from app.services import provider_rules
from app.services.date_parsing import ResolvedPreferences
from app.tenancy import ClinicContext

logger = logging.getLogger(__name__)

# A run may not span more than a working day, and never more than four hours of
# grid. The cap used to be a flat 8 slots, which silently assumed a 30-minute
# clinic; on a 20-minute grid the same 8 slots would only cover 2+ hours, so the
# limit is now expressed in the time it was always meant to guard and converted
# per tenant by :func:`max_run_slots`.
MAX_RUN_MINUTES = 240
MAX_OPTIONS = 24


def max_run_slots(clinic: ClinicContext) -> int:
    """How many consecutive slots a single visit may occupy at this clinic."""
    by_time = MAX_RUN_MINUTES // clinic.config.slot_minutes
    return max(1, min(by_time, clinic.config.slots_per_day))


@dataclass
class SlotOption:
    """One bookable run of intervals, as shown to the patient."""

    slot_ids: list[int]
    stable_id: str
    provider_id: int
    provider_name: str
    provider_credentials: str
    provider_specialties: list[str]
    appointment_type_code: str
    appointment_type_name: str
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int
    patient_id: int | None = None
    patient_first_name: str | None = None

    @property
    def day(self) -> date:
        return self.starts_at.date()

    def as_card(self) -> dict:
        card = {
            "slot_id": self.stable_id,
            "starts_at": self.starts_at.isoformat(timespec="minutes"),
            "ends_at": self.ends_at.isoformat(timespec="minutes"),
            "day": self.day.isoformat(),
            "time": self.starts_at.strftime("%-I:%M %p"),
            "time_range": f"{self.starts_at.strftime('%-I:%M %p')} - {self.ends_at.strftime('%-I:%M %p')}",
            "provider": self.provider_name,
            "provider_credentials": self.provider_credentials,
            "duration_minutes": self.duration_minutes,
            "appointment_type": self.appointment_type_name,
            "appointment_type_code": self.appointment_type_code,
        }
        if self.patient_first_name:
            card["patient_first_name"] = self.patient_first_name
        return card


@dataclass
class SearchResult:
    options: list[SlotOption] = field(default_factory=list)
    providers_considered: list[str] = field(default_factory=list)
    days_considered: int = 0
    closed_days: list[date] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.options)


def slots_required(clinic: ClinicContext, duration_minutes: int) -> int:
    """How many of *this clinic's* slots a visit of that length occupies."""
    return clinic.config.slots_required(duration_minutes)


def stable_slot_id(provider_id: int, starts_at: datetime) -> str:
    return f"slot_{provider_id}_{starts_at.strftime('%Y%m%d%H%M')}"


def resolve_appointment_type(
    session: Session, clinic: ClinicContext, code: str | None
) -> tuple[AppointmentType | None, str | None]:
    """Look up a type by code or a friendly alias. Returns (type, error).

    ``clinic`` is explicit because ``code`` is only unique *within* a clinic: both
    demo practices run a ``cleaning`` type with different durations, and letting
    the query cross tenants would book tenant A's hour-long cleaning on tenant B's
    forty-minute grid.
    """
    normalized = (code or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "clean": "cleaning",
        "hygiene": "cleaning",
        "cleaning": "cleaning",
        "check": "checkup",
        "check_up": "checkup",
        "checkup": "checkup",
        "exam": "checkup",
        "general_checkup": "checkup",
        "new_patient": "checkup",
        "urgent": "emergency",
        "emergency": "emergency",
        "pain": "emergency",
        "toothache": "emergency",
    }
    if not normalized:
        return None, "missing_type"
    wanted = aliases.get(normalized, normalized)
    appointment_type = session.scalar(
        select(AppointmentType).where(
            AppointmentType.code == wanted,
            AppointmentType.clinic_id == clinic.clinic_id,
        )
    )
    if appointment_type is None:
        return None, "unknown_type"
    return appointment_type, None


def _load_free_intervals(
    session: Session,
    *,
    first_day: date,
    last_day: date,
    provider_ids: list[int] | None,
    now: datetime,
) -> dict[int, list[AvailabilitySlot]]:
    """Free intervals grouped by provider, ordered by start, within the range.

    Tenant filtering is carried by the session binding: ``AvailabilitySlot`` is a
    full entity select, so the loader criteria applies and a provider id that
    happens to exist in both clinics cannot pool the two grids.
    """
    window_start = datetime.combine(first_day, datetime.min.time())
    window_end = datetime.combine(last_day + timedelta(days=1), datetime.min.time())

    claimed = (
        select(SlotClaim.availability_slot_id)
        .where(SlotClaim.active.is_(True))
        .execution_options(tenant_scoped=False)
    )
    statement = (
        select(AvailabilitySlot)
        .where(
            AvailabilitySlot.status == SlotStatus.AVAILABLE.value,
            AvailabilitySlot.starts_at >= max(window_start, now),
            AvailabilitySlot.starts_at < window_end,
            AvailabilitySlot.id.not_in(claimed),
        )
        .order_by(AvailabilitySlot.provider_id, AvailabilitySlot.starts_at)
    )
    if provider_ids is not None:
        statement = statement.where(AvailabilitySlot.provider_id.in_(provider_ids))

    grouped: dict[int, list[AvailabilitySlot]] = {}
    for slot in session.scalars(statement):
        grouped.setdefault(slot.provider_id, []).append(slot)
    return grouped


def _consecutive_runs(
    slots: list[AvailabilitySlot], required: int
) -> list[list[AvailabilitySlot]]:
    """Group an ordered provider list into runs of exactly ``required`` intervals."""
    runs: list[list[AvailabilitySlot]] = []
    for index in range(len(slots) - required + 1):
        window = slots[index : index + required]
        ok = True
        for offset in range(1, required):
            if window[offset].starts_at != window[offset - 1].ends_at:
                ok = False
                break
        if ok and window[0].starts_at.date() == window[-1].starts_at.date():
            runs.append(window)
    return runs


def _patient_conflict(
    session: Session,
    clinic: ClinicContext,
    patient_ids: list[int],
    starts_at: datetime,
    ends_at: datetime,
) -> bool:
    """True when any of these patients already has a live appointment overlapping.

    The projection is ``Appointment.id``, not the entity, so the ORM tenant
    criteria does not rewrite it; the clinic predicate is written out here for that
    reason. Two clinics sharing a patient row would otherwise let one clinic's
    booking block the other's, and a double-booked patient is the one failure this
    engine is not allowed to make.
    """
    if not patient_ids:
        return False
    return (
        session.scalar(
            select(Appointment.id).where(
                Appointment.clinic_id == clinic.clinic_id,
                Appointment.patient_id.in_(patient_ids),
                Appointment.status == AppointmentStatus.BOOKED.value,
                Appointment.starts_at < ends_at,
                Appointment.ends_at > starts_at,
            )
        )
        is not None
    )


def load_providers(session: Session, provider_ids: list[int] | None = None) -> list[Provider]:
    statement = (
        select(Provider)
        .where(Provider.active.is_(True))
        .options(selectinload(Provider.specialties))
        .order_by(Provider.id)
    )
    if provider_ids is not None:
        statement = statement.where(Provider.id.in_(provider_ids) if provider_ids else False)
    return list(session.scalars(statement))


def match_provider_by_name(providers: list[Provider], name: str | None) -> list[Provider]:
    """Fuzzy provider match on surname or full name. Empty when nothing matches."""
    if not name or not name.strip():
        return providers
    needle = name.strip().lower()
    needle = needle.removeprefix("dr.").removeprefix("dr ").strip()
    matches = [
        provider
        for provider in providers
        if needle in provider.full_name.lower()
        or needle in provider.full_name.lower().replace("dr. ", "")
    ]
    return matches


def search_availability(
    session: Session,
    clinic: ClinicContext,
    *,
    appointment_type: AppointmentType,
    preferences: ResolvedPreferences,
    patients: list[Patient] | None = None,
    provider_name: str | None = None,
    limit: int = MAX_OPTIONS,
    per_day: int = 3,
    now: datetime | None = None,
) -> SearchResult:
    """Find bookable runs honouring every patient preference.

    ``preferences`` carries the resolved days, windows, and weekday filters.
    Provider and patient names are *not* trusted from the model here: callers
    resolve the patients from the session first.

    ``clinic`` is required and separate from ``preferences``: the preferences say
    what the patient asked for, the clinic says what grid it has to be found on.
    """
    config = clinic.config
    current = now or config.now()
    required = slots_required(clinic, appointment_type.duration_minutes)
    if required > max_run_slots(clinic):
        raise ValueError("appointment type duration exceeds the supported maximum")

    first_day, last_day = preferences.scan_bounds
    if first_day < preferences.today:
        first_day = preferences.today

    providers = load_providers(session)
    if provider_name:
        providers = match_provider_by_name(providers, provider_name)
    eligible = [p for p in providers if provider_rules.provider_eligible(p, appointment_type)]
    if not eligible:
        return SearchResult()

    grouped = _load_free_intervals(
        session,
        first_day=first_day,
        last_day=last_day,
        provider_ids=[provider.id for provider in eligible],
        now=current,
    )

    patient_ids = [patient.id for patient in (patients or [])]
    by_provider = {provider.id: provider for provider in eligible}
    options: list[SlotOption] = []
    seen: set[tuple[int, str]] = set()
    closed_days: list[date] = []

    for provider_id, slots in grouped.items():
        provider = by_provider[provider_id]
        for run in _consecutive_runs(slots, required):
            start, end = run[0].starts_at, run[-1].ends_at
            day = start.date()
            if not preferences.accepts_day(day):
                continue
            if not preferences.accepts_time(start):
                continue
            if not config.is_open_weekday(day):
                continue
            if _patient_conflict(session, clinic, patient_ids, start, end):
                continue
            key = (provider_id, start.strftime("%Y-%m-%dT%H:%M"))
            if key in seen:
                continue
            seen.add(key)
            options.append(
                SlotOption(
                    slot_ids=[slot.id for slot in run],
                    stable_id=stable_slot_id(provider_id, start),
                    provider_id=provider_id,
                    provider_name=provider.full_name,
                    provider_credentials=provider.credentials,
                    provider_specialties=provider.specialty_names,
                    appointment_type_code=appointment_type.code,
                    appointment_type_name=appointment_type.name,
                    starts_at=start,
                    ends_at=end,
                    duration_minutes=appointment_type.duration_minutes,
                )
            )

    options.sort(key=lambda option: (option.starts_at, option.provider_name))

    if preferences.days:
        for day in preferences.days:
            if not config.is_open_weekday(day):
                closed_days.append(day)

    # Spread the answer across days so the patient sees alternatives, not six
    # times on the same morning. The cap is deterministic.
    trimmed: list[SlotOption] = []
    counts: dict[date, int] = {}
    for option in options:
        day = option.day
        if counts.get(day, 0) >= per_day:
            continue
        counts[day] = counts.get(day, 0) + 1
        trimmed.append(option)
        if len(trimmed) >= limit:
            break

    days_considered = len({option.day for option in options})
    log_event(
        logger,
        "availability.search",
        appointment_type=appointment_type.code,
        providers_considered=len(eligible),
        raw_options=len(options),
        returned=len(trimmed),
        closed_days=len(closed_days),
        duration_minutes=appointment_type.duration_minutes,
    )
    return SearchResult(
        options=trimmed,
        providers_considered=[provider.full_name for provider in eligible],
        days_considered=days_considered,
        closed_days=closed_days,
    )


def is_interval_bookable(
    session: Session,
    clinic: ClinicContext,
    *,
    provider_id: int,
    starts_at: datetime,
    duration_minutes: int,
    appointment_type: AppointmentType,
    self_hold_id: int | None = None,
    ignore_appointment_id: int | None = None,
    unheld_slot_ids: Iterable[int] | None = None,
) -> list[AvailabilitySlot] | None:
    """Return the interval rows when this exact appointment can be booked now.

    Used by confirmation to *revalidate* after the patient said yes. Returns
    ``None`` when anything changed: slot gone, claimed, wrong provider, closed
    day, outside business hours, or the type no longer qualifies for the
    provider.

    ``clinic`` decides the grid the interval is measured on and the calendar it is
    checked against. A proposal built on tenant A's 30-minute grid must fail
    revalidation at tenant B rather than silently passing because the timestamps
    happen to line up there too.

    ``self_hold_id`` excludes holds owned by the proposal being confirmed.
    Without it, a proposal's own hold would make its own slot look taken and a
    perfectly valid booking would be rejected after the patient confirmed.

    ``ignore_appointment_id`` does the same for the appointment being moved by a
    reschedule: its current intervals must not count against it. The caller
    releases those claims inside the write transaction before finalizing.
    """
    return _validate_run(
        session,
        clinic,
        provider_id=provider_id,
        starts_at=starts_at,
        duration_minutes=duration_minutes,
        appointment_type=appointment_type,
        self_hold_id=self_hold_id,
        ignore_appointment_id=ignore_appointment_id,
        unheld_slot_ids=unheld_slot_ids,
    )


def _validate_run(
    session: Session,
    clinic: ClinicContext,
    *,
    provider_id: int,
    starts_at: datetime,
    duration_minutes: int,
    appointment_type: AppointmentType,
    self_hold_id: int | None = None,
    ignore_appointment_id: int | None = None,
    unheld_slot_ids: Iterable[int] | None = None,
) -> list[AvailabilitySlot] | None:
    config = clinic.config
    required = slots_required(clinic, duration_minutes)
    day = starts_at.date()
    if not config.is_open_weekday(day):
        return None
    end = starts_at + config.slot_length * required

    # Intervals a reschedule/cancellation proposal relies on without holding:
    # their holder is the target appointment, which this proposal moves or
    # releases, so they are available to this proposal and nobody else.
    unheld = set(unheld_slot_ids or ())
    unheld_filters = []
    if unheld:
        unheld_filters.append(
            or_(
                AvailabilitySlot.id.in_(unheld),
                ~AvailabilitySlot.id.in_(
                    select(SlotClaim.availability_slot_id)
                    .where(
                        SlotClaim.active.is_(True),
                        SlotClaim.clinic_id == clinic.clinic_id,
                    )
                    .execution_options(tenant_scoped=False)
                ),
            )
        )

    claim_filters = [
        SlotClaim.active.is_(True),
        SlotClaim.availability_slot_id.in_(
            select(AvailabilitySlot.id)
            .where(
                AvailabilitySlot.clinic_id == clinic.clinic_id,
                AvailabilitySlot.provider_id == provider_id,
                AvailabilitySlot.starts_at >= starts_at,
                AvailabilitySlot.starts_at < end,
                *unheld_filters,
            )
            .execution_options(tenant_scoped=False)
        ),
    ]
    if self_hold_id is not None and ignore_appointment_id is not None:
        claim_filters.append(
            or_(
                SlotClaim.held_by_proposal_id.is_(None),
                SlotClaim.held_by_proposal_id != self_hold_id,
            )
        )
        claim_filters.append(
            or_(
                SlotClaim.appointment_id.is_(None),
                SlotClaim.appointment_id != ignore_appointment_id,
            )
        )
    elif self_hold_id is not None:
        claim_filters.append(
            or_(
                SlotClaim.held_by_proposal_id.is_(None),
                SlotClaim.held_by_proposal_id != self_hold_id,
            )
        )
    elif ignore_appointment_id is not None:
        claim_filters.append(
            or_(
                SlotClaim.appointment_id.is_(None),
                SlotClaim.appointment_id != ignore_appointment_id,
            )
        )

    rows = list(
        session.scalars(
            select(AvailabilitySlot)
            .where(
                # Written out rather than left to the session binding: this is the
                # lookup that decides whether a slot id handed back by an earlier
                # search is still the same slot, and a tenant-A id presented to
                # tenant B must simply not exist here.
                AvailabilitySlot.clinic_id == clinic.clinic_id,
                AvailabilitySlot.provider_id == provider_id,
                AvailabilitySlot.starts_at >= starts_at,
                AvailabilitySlot.starts_at < end,
            )
            .order_by(AvailabilitySlot.starts_at)
        )
    )
    if len(rows) != required:
        return None
    ignored_claimed_ids: set[int] = set()
    if ignore_appointment_id is not None:
        ignored_claimed_ids = set(
            session.scalars(
                select(SlotClaim.availability_slot_id)
                .where(
                    SlotClaim.appointment_id == ignore_appointment_id,
                    SlotClaim.active.is_(True),
                    SlotClaim.clinic_id == clinic.clinic_id,
                )
                .execution_options(tenant_scoped=False)
            )
        )
    for offset, slot in enumerate(rows):
        if slot.starts_at != starts_at + config.slot_length * offset:
            return None
        if slot.status != SlotStatus.AVAILABLE.value and slot.id not in ignored_claimed_ids:
            return None
    row_ids = [slot.id for slot in rows]
    if not unheld.issuperset(row_ids):
        active_claims = session.scalar(
            select(SlotClaim.id).where(
                SlotClaim.availability_slot_id.in_([i for i in row_ids if i not in unheld]),
                *claim_filters,
            )
        )
        if active_claims is not None:
            return None

    provider = session.get(Provider, provider_id)
    if provider is None or not provider.active or provider.clinic_id != clinic.clinic_id:
        return None
    if not provider_rules.provider_eligible(provider, appointment_type):
        return None
    return rows


def _provider_free_runs(
    session: Session,
    clinic: ClinicContext,
    *,
    provider_id: int,
    day: date,
    after: datetime,
    duration_minutes: int,
    now: datetime,
    before: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    config = clinic.config
    required = slots_required(clinic, duration_minutes)
    claimed = (
        select(SlotClaim.availability_slot_id)
        .where(SlotClaim.active.is_(True), SlotClaim.clinic_id == clinic.clinic_id)
        .execution_options(tenant_scoped=False)
    )
    statement = select(AvailabilitySlot).where(
        AvailabilitySlot.clinic_id == clinic.clinic_id,
        AvailabilitySlot.provider_id == provider_id,
        AvailabilitySlot.starts_at >= max(after, now),
        AvailabilitySlot.starts_at < datetime.combine(day + timedelta(days=1), datetime.min.time()),
        AvailabilitySlot.status == SlotStatus.AVAILABLE.value,
        AvailabilitySlot.id.not_in(claimed),
    )
    if before is not None:
        statement = statement.where(AvailabilitySlot.starts_at + config.slot_length <= before)
    rows = list(session.scalars(statement.order_by(AvailabilitySlot.starts_at)))
    return [(run[0].starts_at, run[-1].ends_at) for run in _consecutive_runs(rows, required)]


@dataclass
class FamilyMemberRequest:
    """One person in a family booking, resolved from the verified session."""

    patient_id: int
    patient_first_name: str
    appointment_type: AppointmentType
    provider_name: str | None = None


def search_family_availability(
    session: Session,
    clinic: ClinicContext,
    *,
    members: list[FamilyMemberRequest],
    preferences: ResolvedPreferences,
    back_to_back: bool = True,
    now: datetime | None = None,
) -> list[list[SlotOption]]:
    """Combined proposals for a family, ordered by how close the visits are.

    ``back_to_back`` selects the stricter continuous-block search. Every
    returned plan still contains one currently open slot for every member on
    the same day; partial plans are never returned.
    """
    search = search_back_to_back_plans if back_to_back else search_nearby_family_plans
    return search(
        session,
        clinic,
        members=members,
        preferences=preferences,
        max_plans=3,
        max_days=_preference_days(preferences),
        now=now,
    )


def _preference_days(preferences: ResolvedPreferences) -> int:
    """The number of calendar days the caller actually asked us to inspect."""
    first_day, last_day = preferences.scan_bounds
    return max(1, (last_day - first_day).days + 1)


MAX_FAMILY_CANDIDATES_PER_MEMBER = 32


def _family_candidates_for_day(
    session: Session,
    clinic: ClinicContext,
    *,
    members: list[FamilyMemberRequest],
    day: date,
    providers: list[Provider],
    preferences: ResolvedPreferences,
    now: datetime,
) -> list[list[SlotOption]]:
    """Return only live, bookable candidate runs for each member on ``day``."""
    candidates_by_member: list[list[SlotOption]] = []
    day_end = datetime.combine(day + timedelta(days=1), datetime.min.time())

    for member in members:
        eligible = [
            provider
            for provider in providers
            if provider_rules.provider_eligible(provider, member.appointment_type)
        ]
        if member.provider_name:
            eligible = match_provider_by_name(eligible, member.provider_name)
        if not eligible:
            return []

        candidates: list[SlotOption] = []
        seen: set[tuple[int, datetime]] = set()
        for provider in eligible:
            runs = _provider_free_runs(
                session,
                clinic,
                provider_id=provider.id,
                day=day,
                after=datetime.combine(day, datetime.min.time()),
                before=day_end,
                duration_minutes=member.appointment_type.duration_minutes,
                now=now,
            )
            for start, end in runs:
                key = (provider.id, start)
                if key in seen or not preferences.accepts_time(start):
                    continue
                if _patient_conflict(session, clinic, [member.patient_id], start, end):
                    continue
                slot_ids = [
                    row.id
                    for row in session.scalars(
                        select(AvailabilitySlot)
                        .where(
                            AvailabilitySlot.clinic_id == clinic.clinic_id,
                            AvailabilitySlot.provider_id == provider.id,
                            AvailabilitySlot.starts_at >= start,
                            AvailabilitySlot.starts_at < end,
                        )
                        .order_by(AvailabilitySlot.starts_at)
                    )
                ]
                if len(slot_ids) != slots_required(clinic, member.appointment_type.duration_minutes):
                    continue
                seen.add(key)
                candidates.append(
                    SlotOption(
                        slot_ids=slot_ids,
                        stable_id=stable_slot_id(provider.id, start),
                        provider_id=provider.id,
                        provider_name=provider.full_name,
                        provider_credentials=provider.credentials,
                        provider_specialties=provider.specialty_names,
                        appointment_type_code=member.appointment_type.code,
                        appointment_type_name=member.appointment_type.name,
                        starts_at=start,
                        ends_at=end,
                        duration_minutes=member.appointment_type.duration_minutes,
                        patient_id=member.patient_id,
                        patient_first_name=member.patient_first_name,
                    )
                )

        candidates.sort(key=lambda option: (option.starts_at, option.provider_id))
        candidates_by_member.append(candidates[:MAX_FAMILY_CANDIDATES_PER_MEMBER])

    return candidates_by_member


def _family_plan_is_non_overlapping(plan: list[SlotOption]) -> bool:
    ordered = sorted(plan, key=lambda option: option.starts_at)
    return all(
        previous.ends_at <= current.starts_at
        for previous, current in zip(ordered, ordered[1:], strict=False)
    )


def _nearby_family_plan_score(plan: list[SlotOption]) -> tuple[Any, ...]:
    """Prefer the smallest total gap, then the smallest individual gap."""
    ordered = sorted(plan, key=lambda option: option.starts_at)
    gaps = [
        max(0, int((current.starts_at - previous.ends_at).total_seconds()))
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ]
    return (
        sum(gaps),
        max(gaps, default=0),
        ordered[0].starts_at,
        tuple(option.starts_at for option in plan),
        tuple(option.provider_id for option in plan),
    )


def _nearby_plans_for_day(
    session: Session,
    clinic: ClinicContext,
    *,
    members: list[FamilyMemberRequest],
    day: date,
    providers: list[Provider],
    preferences: ResolvedPreferences,
    now: datetime,
    limit: int,
) -> list[list[SlotOption]]:
    candidates_by_member = _family_candidates_for_day(
        session,
        clinic,
        members=members,
        day=day,
        providers=providers,
        preferences=preferences,
        now=now,
    )
    if not candidates_by_member or any(not candidates for candidates in candidates_by_member):
        return []

    complete_plans: list[list[SlotOption]] = []

    def collect(index: int, chosen: list[SlotOption]) -> None:
        if index == len(candidates_by_member):
            if _family_plan_is_non_overlapping(chosen):
                complete_plans.append(list(chosen))
            return
        for option in candidates_by_member[index]:
            if any(
                option.starts_at < existing.ends_at
                and existing.starts_at < option.ends_at
                for existing in chosen
            ):
                continue
            collect(index + 1, [*chosen, option])

    collect(0, [])
    complete_plans.sort(key=_nearby_family_plan_score)
    return complete_plans[:limit]


def search_nearby_family_plans(
    session: Session,
    clinic: ClinicContext,
    *,
    members: list[FamilyMemberRequest],
    preferences: ResolvedPreferences,
    max_plans: int = 6,
    max_days: int = 14,
    now: datetime | None = None,
) -> list[list[SlotOption]]:
    """Find complete same-day family plans, closest appointments first.

    A plan may have gaps, but appointments never overlap. Partial family plans
    are deliberately excluded so the confirmation remains atomic.
    """
    current = now or clinic.config.now()
    if not members:
        return []

    providers = load_providers(session)
    scan_from = max(preferences.scan_bounds[0], current.date())
    last_day = min(preferences.scan_bounds[1], scan_from + timedelta(days=max(1, max_days) - 1))
    plans: list[list[SlotOption]] = []
    seen: set[tuple[str, ...]] = set()
    day = scan_from
    while day <= last_day and len(plans) < max_plans:
        if preferences.accepts_day(day) and clinic.config.is_open_weekday(day):
            for plan in _nearby_plans_for_day(
                session,
                clinic,
                members=members,
                day=day,
                providers=providers,
                preferences=preferences,
                now=current,
                limit=max_plans - len(plans),
            ):
                signature = tuple(option.stable_id for option in plan)
                if signature in seen:
                    continue
                seen.add(signature)
                plans.append(plan)
        day += timedelta(days=1)
    return plans


def search_back_to_back_plans(
    session: Session,
    clinic: ClinicContext,
    *,
    members: list[FamilyMemberRequest],
    preferences: ResolvedPreferences,
    max_plans: int = 6,
    max_days: int = 14,
    now: datetime | None = None,
) -> list[list[SlotOption]]:
    """Up to ``max_plans`` continuous blocks for a household, spread over days.

    Each plan is a real run of the schedule for every member, chained one after
    the other (the parent finishes, the child starts), so one confirmation can
    book the whole family. Plans are collected across days rather than one per
    day, because a single day often has room for only one block.
    """
    current = now or clinic.config.now()
    if not members:
        return []
    if len(members) > max_run_slots(clinic):
        raise ValueError("too many family members for one back-to-back block")

    providers = load_providers(session)
    scan_from = max(preferences.scan_bounds[0], current.date())
    last_day = min(
        preferences.scan_bounds[1],
        scan_from + timedelta(days=max(1, max_days) - 1),
    )

    plans: list[list[SlotOption]] = []
    seen: set[tuple[str, ...]] = set()
    day = scan_from
    while day <= last_day and len(plans) < max_plans:
        if preferences.accepts_day(day) and clinic.config.is_open_weekday(day):
            for plan in _blocks_for_day(
                session,
                clinic,
                members=members,
                day=day,
                providers=providers,
                preferences=preferences,
                now=current,
                limit=max_plans - len(plans),
            ):
                signature = tuple(option.stable_id for option in plan)
                if signature in seen:
                    continue
                seen.add(signature)
                plans.append(plan)
                if len(plans) >= max_plans:
                    break
        day += timedelta(days=1)
    return plans


def _blocks_for_day(
    session: Session,
    clinic: ClinicContext,
    *,
    members: list[FamilyMemberRequest],
    day: date,
    providers: list[Provider],
    preferences: ResolvedPreferences,
    now: datetime,
    limit: int,
) -> list[list[SlotOption]]:
    """Every continuous block on one day, latest-starting block dropped each pass."""
    blocks: list[list[SlotOption]] = []
    day_start = datetime.combine(day, datetime.min.time())
    day_end = datetime.combine(day + timedelta(days=1), datetime.min.time())
    cursor = day_start
    while len(blocks) < limit:
        plan: list[SlotOption] = []
        used: set[tuple[int, datetime]] = set()
        placeable = True
        for member in members:
            candidates = [
                provider
                for provider in providers
                if provider_rules.provider_eligible(provider, member.appointment_type)
            ]
            if member.provider_name:
                candidates = match_provider_by_name(candidates, member.provider_name)
            choice = _earliest_chain(
                session,
                clinic,
                member=member,
                candidates=candidates,
                day=day,
                after=cursor,
                before=day_end,
                used=used,
                preferences=preferences,
                now=now,
            )
            if choice is None:
                placeable = False
                break
            option = choice
            used.add((option.provider_id, option.starts_at))
            plan.append(option)
            cursor = option.ends_at
        if not placeable:
            break
        blocks.append(plan)
    return blocks


def _earliest_chain(
    session: Session,
    clinic: ClinicContext,
    *,
    member: FamilyMemberRequest,
    candidates: list[Provider],
    day: date,
    after: datetime,
    before: datetime,
    used: set[tuple[int, datetime]],
    preferences: ResolvedPreferences,
    now: datetime,
) -> SlotOption | None:
    for provider in candidates:
        runs = _provider_free_runs(
            session,
            clinic,
            provider_id=provider.id,
            day=day,
            after=after,
            before=before,
            duration_minutes=member.appointment_type.duration_minutes,
            now=now,
        )
        for start, end in runs:
            if not preferences.accepts_time(start):
                continue
            if (provider.id, start) in used:
                continue
            if _patient_conflict(session, clinic, [member.patient_id], start, end):
                continue
            slot_ids = [
                row.id
                for row in session.scalars(
                    select(AvailabilitySlot)
                    .where(
                        AvailabilitySlot.clinic_id == clinic.clinic_id,
                        AvailabilitySlot.provider_id == provider.id,
                        AvailabilitySlot.starts_at >= start,
                        AvailabilitySlot.starts_at < end,
                    )
                    .order_by(AvailabilitySlot.starts_at)
                )
            ]
            if len(slot_ids) != slots_required(clinic, member.appointment_type.duration_minutes):
                continue
            return SlotOption(
                slot_ids=slot_ids,
                stable_id=stable_slot_id(provider.id, start),
                provider_id=provider.id,
                provider_name=provider.full_name,
                provider_credentials=provider.credentials,
                provider_specialties=provider.specialty_names,
                appointment_type_code=member.appointment_type.code,
                appointment_type_name=member.appointment_type.name,
                starts_at=start,
                ends_at=end,
                duration_minutes=member.appointment_type.duration_minutes,
                patient_id=member.patient_id,
                patient_first_name=member.patient_first_name,
            )
    return None


__all__ = [
    "MAX_OPTIONS",
    "MAX_RUN_MINUTES",
    "FamilyMemberRequest",
    "SearchResult",
    "SlotOption",
    "is_interval_bookable",
    "load_providers",
    "match_provider_by_name",
    "max_run_slots",
    "resolve_appointment_type",
    "search_availability",
    "search_back_to_back_plans",
    "search_family_availability",
    "search_nearby_family_plans",
    "slots_required",
    "stable_slot_id",
]
