"""Idempotent demo-data seeding.

Design rules
------------
* Every seeded row carries a stable ``seed_key`` (or an equivalent natural key).
  Seeding looks rows up and creates only what is missing, so ``make seed`` can
  run repeatedly without duplicating data and without deleting anything.
* Slots are generated only for days that currently have **no** slots for the
  provider. That gives a rolling 60-day horizon on later runs while never
  touching existing (possibly occupied) intervals.
* Everything happens inside one transaction, so a failed seed rolls back whole.
* The anchor date is the next Monday at/after the clinic-local today, which is
  deterministic per day and keeps fixture weekdays stable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict, deque
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AvailabilitySlot,
    BookingProposal,
    Conversation,
    FamilyGroup,
    FamilyMember,
    IdempotencyRecord,
    Message,
    Patient,
    PendingRequest,
    ProposalItem,
    Provider,
    ProviderSpecialty,
    SeedManifest,
    SlotClaim,
    SlotStatus,
    StaffAlert,
    StaffUser,
    utcnow,
)
from app.seed_fixtures import (
    FIXTURE_SET,
    FIXTURE_VERSION,
    SEED_PLAN,
    AppointmentFixture,
    SeedSummary,
    anchor_date,
    appointment_bounds,
)
from app.services import audit, generations
from app.services.security import hash_password
from app.timeutil import (
    CLOSE_TIME,
    OPEN_TIME,
    SEED_HORIZON_DAYS,
    SLOT_LENGTH,
    clinic_now,
    is_open_weekday,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def seed_database(today: date | None = None) -> SeedSummary:
    """Apply all fixtures, skipping anything that already exists."""
    anchor = anchor_date(today)
    counters: defaultdict[str, int] = defaultdict(int)

    with session_scope() as session:
        generations.ensure_generation(session)

        providers = _seed_providers(session, counters)
        patients, families = _seed_families_and_patients(session, counters)
        types = _seed_appointment_types(session, counters)
        _seed_slots(
            session,
            providers,
            today=today or anchor_date(),
            anchor=anchor,
            counters=counters,
        )
        _seed_appointments(session, providers, patients, types, families, anchor, counters)
        staff = _seed_staff(session, counters)
        _seed_alerts(session, patients, counters)
        _seed_pending_requests(session, patients, staff, counters)
        conversations = _seed_conversations(session, patients, counters)
        _seed_proposals(session, conversations, providers, patients, types, anchor, counters)
        _seed_idempotency(session, counters)
        _touch_manifest(session)

        audit.record(
            session,
            action="demo.seed",
            entity_type="fixture_set",
            entity_id=FIXTURE_SET,
            details=dict(sorted(counters.items())),
        )

    summary = SeedSummary(
        anchor_date=anchor,
        horizon_days=SEED_HORIZON_DAYS,
        providers_created=counters["providers"],
        patients_created=counters["patients"],
        families_created=counters["families"],
        appointment_types_created=counters["appointment_types"],
        slots_created=counters["slots"],
        appointments_created=counters["appointments"],
        staff_created=counters["staff"],
        alerts_created=counters["alerts"],
        pending_requests_created=counters["pending_requests"],
        conversations_created=counters["conversations"],
        messages_created=counters["messages"],
        proposals_created=counters["proposals"],
        proposal_items_created=counters["proposal_items"],
        claim_conflicts=counters["claim_conflicts"],
        skipped_existing=counters["skipped"],
    )
    logger.info("Seed complete: %s", summary.as_dict())
    return summary


def database_is_seeded(session: Session) -> bool:
    """True when the fixture manifest has been applied at least once."""
    return (
        session.scalar(
            select(func.count())
            .select_from(SeedManifest)
            .where(SeedManifest.fixture_set == FIXTURE_SET)
        )
        or 0
    ) > 0


def is_empty_database(session: Session) -> bool:
    """An empty database has no patients, providers, or fixtures at all."""
    patient_count = session.scalar(select(func.count()).select_from(Patient)) or 0
    provider_count = session.scalar(select(func.count()).select_from(Provider)) or 0
    return patient_count == 0 and provider_count == 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _checksum() -> str:
    payload = {
        "version": FIXTURE_VERSION,
        "providers": [item.key for item in SEED_PLAN.providers],
        "families": [item.key for item in SEED_PLAN.families],
        "patients": [item.key for item in SEED_PLAN.patients],
        "types": [item.code for item in SEED_PLAN.appointment_types],
        "appointments": [item.key for item in SEED_PLAN.upcoming + SEED_PLAN.past],
        "staff": [item.key for item in SEED_PLAN.staff],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _touch_manifest(session: Session) -> None:
    now = utcnow()
    manifest = session.scalar(select(SeedManifest).where(SeedManifest.fixture_set == FIXTURE_SET))
    if manifest is None:
        session.add(
            SeedManifest(
                fixture_set=FIXTURE_SET,
                fixture_version=FIXTURE_VERSION,
                checksum=_checksum(),
                applied_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        manifest.fixture_version = FIXTURE_VERSION
        manifest.checksum = _checksum()
        manifest.applied_at = now
    generations.mark_seeded(session, now)
    session.flush()


def _slot_at(session: Session, provider_id: int, starts_at: datetime) -> AvailabilitySlot | None:
    return session.scalar(
        select(AvailabilitySlot).where(
            AvailabilitySlot.provider_id == provider_id,
            AvailabilitySlot.starts_at == starts_at,
        )
    )


# --------------------------------------------------------------------------- #
# Fixture groups
# --------------------------------------------------------------------------- #


def _seed_providers(session: Session, counters: defaultdict[str, int]) -> dict[str, Provider]:
    keys = [item.key for item in SEED_PLAN.providers]
    existing = {
        provider.seed_key: provider
        for provider in session.scalars(select(Provider).where(Provider.seed_key.in_(keys)))
    }
    result: dict[str, Provider] = {}
    for fixture in SEED_PLAN.providers:
        provider = existing.get(fixture.key)
        if provider is None:
            provider = Provider(
                seed_key=fixture.key,
                full_name=fixture.full_name,
                credentials=fixture.credentials,
                email=fixture.email,
                bio=fixture.bio,
                active=True,
            )
            session.add(provider)
            counters["providers"] += 1
        else:
            counters["skipped"] += 1
        session.flush()
        result[fixture.key] = provider

        known = {specialty.specialty for specialty in provider.specialties}
        for specialty in fixture.specialties:
            if specialty not in known:
                session.add(ProviderSpecialty(provider_id=provider.id, specialty=specialty))
    session.flush()
    return result


def _seed_families_and_patients(
    session: Session, counters: defaultdict[str, int]
) -> tuple[dict[str, Patient], dict[str, FamilyGroup]]:
    families: dict[str, FamilyGroup] = {}
    for fixture in SEED_PLAN.families:
        group = session.scalar(select(FamilyGroup).where(FamilyGroup.seed_key == fixture.key))
        if group is None:
            group = FamilyGroup(
                seed_key=fixture.key,
                name=fixture.name,
                shared_phone=fixture.shared_phone,
                notes=fixture.notes,
            )
            session.add(group)
            counters["families"] += 1
        else:
            counters["skipped"] += 1
        session.flush()
        families[fixture.key] = group

    patients: dict[str, Patient] = {}
    for fixture in SEED_PLAN.patients:
        patient = session.scalar(select(Patient).where(Patient.seed_key == fixture.key))
        if patient is None:
            patient = Patient(
                seed_key=fixture.key,
                full_name=fixture.full_name,
                phone=fixture.phone,
                date_of_birth=date.fromisoformat(fixture.dob),
                insurance_provider=fixture.insurance_provider,
                insurance_member_id=fixture.insurance_member_id,
                notes=fixture.notes,
            )
            session.add(patient)
            counters["patients"] += 1
        else:
            counters["skipped"] += 1
        session.flush()
        patients[fixture.key] = patient

    for fixture in SEED_PLAN.patients:
        if fixture.family_group_key is None:
            continue
        group = families[fixture.family_group_key]
        patient = patients[fixture.key]
        membership = session.scalar(
            select(FamilyMember).where(
                FamilyMember.family_group_id == group.id,
                FamilyMember.patient_id == patient.id,
            )
        )
        if membership is None:
            session.add(
                FamilyMember(
                    family_group_id=group.id,
                    patient_id=patient.id,
                    relation_to_primary=fixture.relationship or "member",
                    is_primary_contact=fixture.is_primary_contact,
                )
            )
        else:
            counters["skipped"] += 1
    session.flush()
    return patients, families


def _seed_appointment_types(
    session: Session, counters: defaultdict[str, int]
) -> dict[str, AppointmentType]:
    codes = [item.code for item in SEED_PLAN.appointment_types]
    existing = {
        item.code: item
        for item in session.scalars(select(AppointmentType).where(AppointmentType.code.in_(codes)))
    }
    result: dict[str, AppointmentType] = {}
    for index, fixture in enumerate(SEED_PLAN.appointment_types):
        item = existing.get(fixture.code)
        if item is None:
            item = AppointmentType(
                code=fixture.code,
                name=fixture.name,
                description=fixture.description,
                duration_minutes=fixture.duration_minutes,
                is_emergency=fixture.is_emergency,
                required_specialty=fixture.required_specialty,
                sort_order=index,
            )
            session.add(item)
            counters["appointment_types"] += 1
        else:
            counters["skipped"] += 1
        session.flush()
        result[fixture.code] = item
    return result


def _seed_slots(
    session: Session,
    providers: dict[str, Provider],
    *,
    today: date,
    anchor: date,
    counters: defaultdict[str, int],
) -> None:
    """Create the 60-day, 30-minute grid for days that have no slots yet."""
    days = [today + timedelta(days=offset) for offset in range(SEED_HORIZON_DAYS)]

    for provider_fixture in SEED_PLAN.providers:
        provider = providers[provider_fixture.key]
        covered_days = set(
            session.scalars(
                select(func.date(AvailabilitySlot.starts_at)).where(
                    AvailabilitySlot.provider_id == provider.id
                )
            )
        )
        rows: list[dict] = []
        for day in days:
            if not is_open_weekday(day):
                continue
            if day.isoformat() in covered_days:
                counters["skipped"] += 1
                continue
            cursor = datetime.combine(day, OPEN_TIME)
            close = datetime.combine(day, CLOSE_TIME)
            while cursor < close:
                rows.append(
                    {
                        "provider_id": provider.id,
                        "starts_at": cursor,
                        "ends_at": cursor + SLOT_LENGTH,
                        "status": SlotStatus.AVAILABLE.value,
                    }
                )
                cursor += SLOT_LENGTH
        if rows:
            session.execute(AvailabilitySlot.__table__.insert(), rows)
            counters["slots"] += len(rows)

    _backfill_historical_slots(session, providers, anchor=anchor, counters=counters)
    session.flush()


def _backfill_historical_slots(
    session: Session,
    providers: dict[str, Provider],
    *,
    anchor: date,
    counters: defaultdict[str, int],
) -> None:
    """Materialise slots on past days that fixture appointments need.

    The forward grid intentionally starts today, but the demo includes completed
    visits. Only the exact business days those visits touch are created, so the
    historical data stays small and reproducible.
    """
    needed: dict[int, set[date]] = defaultdict(set)
    for fixture in SEED_PLAN.past:
        provider = providers[fixture.provider_key]
        needed[provider.id].add(anchor + timedelta(days=fixture.day_offset))

    for provider_id, days in needed.items():
        existing_days = set(
            session.scalars(
                select(func.date(AvailabilitySlot.starts_at)).where(
                    AvailabilitySlot.provider_id == provider_id,
                    AvailabilitySlot.starts_at >= anchor + timedelta(days=-SEED_HORIZON_DAYS),
                    AvailabilitySlot.starts_at < anchor,
                )
            )
        )
        rows: list[dict] = []
        for day in sorted(days):
            if not is_open_weekday(day) or day.isoformat() in existing_days:
                continue
            cursor = datetime.combine(day, OPEN_TIME)
            close = datetime.combine(day, CLOSE_TIME)
            while cursor < close:
                rows.append(
                    {
                        "provider_id": provider_id,
                        "starts_at": cursor,
                        "ends_at": cursor + SLOT_LENGTH,
                        "status": SlotStatus.AVAILABLE.value,
                    }
                )
                cursor += SLOT_LENGTH
        if rows:
            session.execute(AvailabilitySlot.__table__.insert(), rows)
            counters["slots"] += len(rows)
    session.flush()


def _seed_appointments(
    session: Session,
    providers: dict[str, Provider],
    patients: dict[str, Patient],
    types: dict[str, AppointmentType],
    families: dict[str, FamilyGroup],
    anchor: date,
    counters: defaultdict[str, int],
) -> None:
    now_local = clinic_now()
    fixtures: list[AppointmentFixture] = [*SEED_PLAN.upcoming, *SEED_PLAN.past]
    patient_groups = {fixture.key: fixture.family_group_key for fixture in SEED_PLAN.patients}

    for fixture in fixtures:
        if session.scalar(select(Appointment).where(Appointment.seed_key == fixture.key)):
            counters["skipped"] += 1
            continue

        provider = providers[fixture.provider_key]
        appointment_type = types[fixture.type_code]
        starts_at, ends_at = appointment_bounds(fixture, anchor)

        interval_starts: list[datetime] = []
        cursor = starts_at
        while cursor < ends_at:
            interval_starts.append(cursor)
            cursor += SLOT_LENGTH

        slots = [_slot_at(session, provider.id, start) for start in interval_starts]
        if any(slot is None for slot in slots):
            logger.warning(
                "Fixture %s targets %s with no availability grid (closed day or "
                "outside the seeded horizon); skipped.",
                fixture.key,
                starts_at.isoformat(timespec="minutes"),
            )
            counters["claim_conflicts"] += 1
            continue
        if any(slot.status != SlotStatus.AVAILABLE.value for slot in slots if slot):
            logger.warning("Fixture %s overlaps an already-occupied slot; skipped.", fixture.key)
            counters["claim_conflicts"] += 1
            continue

        status = (
            AppointmentStatus.COMPLETED.value
            if starts_at < now_local
            else AppointmentStatus.BOOKED.value
        )
        appointment = Appointment(
            seed_key=fixture.key,
            patient_id=patients[fixture.patient_key].id,
            provider_id=provider.id,
            appointment_type_id=appointment_type.id,
            starts_at=starts_at,
            ends_at=ends_at,
            duration_minutes=appointment_type.duration_minutes,
            status=status,
            reason=fixture.reason,
            created_by="seed",
            family_group_id=(
                families[patient_groups[fixture.patient_key]].id
                if patient_groups.get(fixture.patient_key)
                else None
            ),
        )
        session.add(appointment)
        session.flush()

        for slot in slots:
            assert slot is not None  # guarded above
            slot.status = SlotStatus.OCCUPIED.value
            session.add(
                SlotClaim(
                    availability_slot_id=slot.id,
                    appointment_id=appointment.id,
                    active=True,
                )
            )
        counters["appointments"] += 1
    session.flush()


def _seed_staff(session: Session, counters: defaultdict[str, int]) -> dict[str, StaffUser]:
    keys = [item.key for item in SEED_PLAN.staff]
    existing = {
        user.seed_key: user
        for user in session.scalars(select(StaffUser).where(StaffUser.seed_key.in_(keys)))
    }
    result: dict[str, StaffUser] = {}
    for fixture in SEED_PLAN.staff:
        user = existing.get(fixture.key)
        if user is None:
            user = StaffUser(
                seed_key=fixture.key,
                email=fixture.email,
                display_name=fixture.display_name,
                role=fixture.role,
                username=fixture.username,
                password_hash=hash_password(fixture.demo_password),
                active=True,
            )
            session.add(user)
            counters["staff"] += 1
        else:
            # Backfill the Phase 4 login fields onto accounts seeded before the
            # dashboard existed: an upgraded demo must still be able to sign in
            # as the documented ``root`` user, and its password must stay hashed.
            if fixture.username and user.username != fixture.username:
                user.username = fixture.username
                user.password_hash = hash_password(fixture.demo_password)
            counters["skipped"] += 1
        session.flush()
        result[fixture.key] = user
    return result


def _seed_alerts(
    session: Session, patients: dict[str, Patient], counters: defaultdict[str, int]
) -> None:
    for fixture in SEED_PLAN.alerts:
        exists = session.scalar(
            select(StaffAlert).where(
                StaffAlert.kind == fixture.kind, StaffAlert.message == fixture.message
            )
        )
        if exists:
            counters["skipped"] += 1
            continue
        session.add(
            StaffAlert(
                patient_id=patients[fixture.patient_key].id if fixture.patient_key else None,
                kind=fixture.kind,
                severity=fixture.severity,
                message=fixture.message,
                status=fixture.status,
            )
        )
        counters["alerts"] += 1
    session.flush()


def _seed_pending_requests(
    session: Session,
    patients: dict[str, Patient],
    staff: dict[str, StaffUser],
    counters: defaultdict[str, int],
) -> None:
    staff_queue = deque(staff.values())
    for fixture in SEED_PLAN.pending_requests:
        patient = patients[fixture.patient_key]
        exists = session.scalar(
            select(PendingRequest).where(
                PendingRequest.details == fixture.details,
                PendingRequest.patient_id == patient.id,
            )
        )
        if exists:
            counters["skipped"] += 1
            continue
        assigned_to = None
        if fixture.status != "pending" and staff_queue:
            assigned_to = staff_queue[0].id
        session.add(
            PendingRequest(
                patient_id=patient.id,
                request_type=fixture.request_type,
                status=fixture.status,
                details=fixture.details,
                assigned_to=assigned_to,
            )
        )
        counters["pending_requests"] += 1
    session.flush()


def _seed_conversations(
    session: Session, patients: dict[str, Patient], counters: defaultdict[str, int]
) -> dict[str, Conversation]:
    generation = generations.get_generation(session) or generations.new_generation()
    result: dict[str, Conversation] = {}
    for fixture in SEED_PLAN.conversations:
        public_id = f"demo-{fixture.key}"
        existing = session.scalar(select(Conversation).where(Conversation.public_id == public_id))
        if existing:
            counters["skipped"] += 1
            result[fixture.key] = existing
            continue

        started_at = clinic_now() + timedelta(days=fixture.day_offset)
        conversation = Conversation(
            public_id=public_id,
            status=fixture.status,
            caller_phone=fixture.caller_phone,
            patient_id=patients[fixture.patient_key].id,
            reset_generation=generation,
            started_at=started_at,
            ended_at=started_at if fixture.status == "closed" else None,
        )
        session.add(conversation)
        session.flush()
        for sequence, (role, content, minute_offset) in enumerate(fixture.messages, start=1):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role=role,
                    content=content,
                    sequence=sequence,
                    created_at=started_at + timedelta(minutes=minute_offset),
                )
            )
            counters["messages"] += 1
        counters["conversations"] += 1
        result[fixture.key] = conversation
    session.flush()
    return result


def _seed_proposals(
    session: Session,
    conversations: dict[str, Conversation],
    providers: dict[str, Provider],
    patients: dict[str, Patient],
    types: dict[str, AppointmentType],
    anchor: date,
    counters: defaultdict[str, int],
) -> None:
    generation = generations.get_generation(session) or generations.new_generation()
    for fixture in SEED_PLAN.proposals:
        public_id = f"demo-proposal-{fixture.key}"
        if session.scalar(select(BookingProposal).where(BookingProposal.public_id == public_id)):
            counters["skipped"] += 1
            continue

        conversation = conversations[fixture.conversation_key]
        proposal = BookingProposal(
            public_id=public_id,
            conversation_id=conversation.id,
            status=fixture.status,
            summary=fixture.summary,
            reset_generation=generation,
        )
        session.add(proposal)
        session.flush()

        for order_index, item in enumerate(fixture.items):
            patient_key, provider_key, type_code, day_offset, slot_index = item
            day = anchor + timedelta(days=day_offset)
            proposed_start = datetime.combine(day, OPEN_TIME) + SLOT_LENGTH * slot_index
            slot = _slot_at(session, providers[provider_key].id, proposed_start)
            session.add(
                ProposalItem(
                    proposal_id=proposal.id,
                    patient_id=patients[patient_key].id,
                    provider_id=providers[provider_key].id,
                    appointment_type_id=types[type_code].id,
                    availability_slot_id=slot.id if slot else None,
                    proposed_starts_at=proposed_start,
                    duration_minutes=types[type_code].duration_minutes,
                    order_index=order_index,
                )
            )
            counters["proposal_items"] += 1
        counters["proposals"] += 1
    session.flush()


def _seed_idempotency(session: Session, counters: defaultdict[str, int]) -> None:
    """One completed record so the table is non-empty for demonstrations."""
    key, scope = "demo-seed", "demo.seed"
    if session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.key == key, IdempotencyRecord.scope == scope
        )
    ):
        counters["skipped"] += 1
        return
    session.add(
        IdempotencyRecord(
            key=key,
            scope=scope,
            response_status=200,
            response_body=json.dumps({"seeded": True}),
            reset_generation=generations.get_generation(session) or "unknown",
            created_at=utcnow(),
        )
    )
    session.flush()
