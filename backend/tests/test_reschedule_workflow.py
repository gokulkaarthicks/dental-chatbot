"""Reschedule workflow: the October 27 -> October 29 regression, end to end.

These tests drive the *real* service layer (tools -> scheduling -> proposals ->
one SQLite write transaction) against a throwaway database. No model and no
server is involved, so what they protect is exactly the part that was broken: a
confirmed reschedule must retire the original and book the replacement
atomically, and must never turn into a second booking.

Covered
-------
* successful reschedule: the original becomes ``rescheduled``, the replacement
  ``booked``, the original slot free again, exactly one active appointment;
* rejection / expiry: the original is untouched;
* conflict at confirmation: the original is untouched and the proposal stays
  pending so the patient can retry;
* concurrent confirms: one moves the visit, the other replays -- never two;
* repeated confirmation: replays the first result and creates nothing;
* rollback: a failure *after* the original's claims were released restores
  everything, including the original's claims;
* routing: rescheduling is its own tool and neither confirmation tool can
  consume the other kind of proposal;
* ownership: another patient's appointment id cannot be moved.

Everything is configured by fixtures, so no environment variable is needed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.scheduling_tools import SCHEDULING_TOOL_NAMES
from app.agent.tools import ToolContext, execute_tool
from app.db import create_db_engine
from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AvailabilitySlot,
    Base,
    Conversation,
    FamilyGroup,
    FamilyMember,
    Patient,
    PatientSession,
    ProposalStatus,
    Provider,
    ProviderSpecialty,
    SlotClaim,
    SlotStatus,
    utcnow,
)
from app.services import appointments, patient_sessions, scheduling
from app.services.availability_engine import is_interval_bookable
from app.services.proposals import (
    expire_proposal,
    find_by_public_id,
    pending_for_conversation,
)
from app.services.scheduling import Requester
from app.timeutil import CLOSE_TIME, OPEN_TIME, SLOT_MINUTES

# 2030-05-01 is a Wednesday and 2030-05-03 a Friday. Both are open weekdays, so
# the scenario does not depend on the day the suite runs.
ORIGINAL_DAY = date(2030, 5, 1)
REPLACEMENT_DAY = date(2030, 5, 3)

SessionFactory = sessionmaker[Session]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@contextmanager
def open_session(factory: SessionFactory) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()


def at(day: date, start: time) -> datetime:
    return datetime.combine(day, start)


def shifted(day: date, start: time, intervals: int) -> time:
    return (at(day, start) + intervals * timedelta(minutes=SLOT_MINUTES)).time()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[SessionFactory]:
    """A throwaway on-disk SQLite database with the production pragmas.

    ``create_db_engine`` still applies WAL, foreign keys and the busy timeout, so
    the concurrency test exercises real locking. ``create_schema``/seed are
    deliberately *not* used: they write to the project's real database path.
    """
    from app import db as db_module

    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'reschedule-test.db'}")
    factory: SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
    previous_engine = db_module._engine
    previous_factory = db_module._session_factory
    db_module._engine = engine
    db_module._session_factory = factory
    Base.metadata.create_all(bind=engine)
    try:
        yield factory
    finally:
        db_module._engine = previous_engine
        db_module._session_factory = previous_factory
        engine.dispose()


class World:
    """The regression scenario's data, created once per test."""

    def __init__(self, session: Session) -> None:
        self.provider = Provider(
            full_name="Dr. Marion Varga",
            credentials="DDS",
            email="varga@example.test",
            bio=None,
            active=True,
        )
        session.add(self.provider)
        session.flush()
        session.add(
            ProviderSpecialty(provider_id=self.provider.id, specialty="General Dentistry")
        )

        self.cleaning = AppointmentType(
            code="cleaning",
            name="Cleaning",
            description=None,
            duration_minutes=60,
            is_emergency=False,
            required_specialty="General Dentistry",
            sort_order=1,
        )
        self.checkup = AppointmentType(
            code="checkup",
            name="Checkup",
            description=None,
            duration_minutes=30,
            is_emergency=False,
            required_specialty="General Dentistry",
            sort_order=2,
        )
        session.add_all([self.cleaning, self.checkup])

        self.patient = Patient(
            full_name="Dana Whitfield",
            phone="+15550101234",
            date_of_birth=date(1990, 4, 12),
            insurance_provider="Self-pay",
        )
        self.other_patient = Patient(
            full_name="Alex Moreau",
            phone="+15550109999",
            date_of_birth=date(1985, 1, 9),
            insurance_provider="Self-pay",
        )
        session.add_all([self.patient, self.other_patient])
        session.flush()

        self.slots: dict[date, dict[time, AvailabilitySlot]] = {
            day: self._grid(session, day) for day in (ORIGINAL_DAY, REPLACEMENT_DAY)
        }

        # The October 27, 10:00-11:00 AM cleaning.
        self.original = self.book(
            session,
            patient_id=self.patient.id,
            day=ORIGINAL_DAY,
            start=time(10, 0),
            intervals=2,
            appointment_type=self.cleaning,
        )

    def _grid(self, session: Session, day: date) -> dict[time, AvailabilitySlot]:
        rows: dict[time, AvailabilitySlot] = {}
        cursor = at(day, OPEN_TIME)
        end = at(day, CLOSE_TIME)
        while cursor < end:
            row = AvailabilitySlot(
                provider_id=self.provider.id,
                starts_at=cursor,
                ends_at=cursor + timedelta(minutes=SLOT_MINUTES),
                status=SlotStatus.AVAILABLE.value,
            )
            session.add(row)
            rows[cursor.time()] = row
            cursor += timedelta(minutes=SLOT_MINUTES)
        session.flush()
        return rows

    def slot(self, day: date, start: time) -> AvailabilitySlot:
        return self.slots[day][start]

    def stable_slot(self, day: date, start: time) -> str:
        """The id the UI and the model exchange -- never a database key."""
        return self.slot(day, start).stable_id

    def book(
        self,
        session: Session,
        *,
        patient_id: int,
        day: date,
        start: time,
        intervals: int,
        appointment_type: AppointmentType,
    ) -> Appointment:
        """Insert a booked appointment and its interval claims (fixture booking)."""
        rows = [self.slot(day, shifted(day, start, index)) for index in range(intervals)]
        appointment = Appointment(
            patient_id=patient_id,
            provider_id=self.provider.id,
            appointment_type_id=appointment_type.id,
            starts_at=rows[0].starts_at,
            ends_at=rows[-1].ends_at,
            duration_minutes=appointment_type.duration_minutes,
            status=AppointmentStatus.BOOKED.value,
            created_by="test",
        )
        session.add(appointment)
        session.flush()
        for row in rows:
            row.status = SlotStatus.OCCUPIED.value
            session.add(
                SlotClaim(availability_slot_id=row.id, appointment_id=appointment.id, active=True)
            )
        session.flush()
        return appointment


@contextmanager
def requester_for(session: Session, world: World) -> Iterator[Requester]:
    """A verified patient session plus its conversation.

    Committed as it is built, because every write path (proposal creation,
    confirmation) opens its own ``BEGIN IMMEDIATE`` transaction and would
    discard an uncommitted read transaction.
    """
    record = patient_sessions.create_patient_session(session)
    patient_session = session.get(PatientSession, record.session_id)
    patient_sessions.mark_verified(
        session,
        patient_session,
        world.patient,
        method=patient_sessions.VERIFICATION_BY_LOOKUP,
    )
    conversation = Conversation(
        public_id=f"conv_{patient_session.id}",
        patient_session_id=patient_session.id,
        status="active",
        patient_id=world.patient.id,
        reset_generation=record.reset_generation,
        started_at=utcnow(),
        last_message_at=utcnow(),
    )
    session.add(conversation)
    session.commit()
    yield Requester(
        patient=world.patient, patient_session=patient_session, conversation=conversation
    )
    session.commit()


@pytest.fixture()
def world_session(db: SessionFactory) -> Iterator[tuple[Session, World, Requester]]:
    with open_session(db) as session:
        world = World(session)
        session.commit()
        with requester_for(session, world) as requester:
            yield session, world, requester


# --------------------------------------------------------------------------- #
# Driving the workflow exactly as the model would
# --------------------------------------------------------------------------- #


def tool_ctx(session: Session, requester: Requester) -> ToolContext:
    return ToolContext(
        session=session,
        patient_session=requester.patient_session,
        conversation_id=requester.conversation.id,
    )


def call(session: Session, requester: Requester, name: str, arguments: dict) -> dict:
    return execute_tool(tool_ctx(session, requester), name, json.dumps(arguments))


def propose_move(
    session: Session,
    requester: Requester,
    world: World,
    day: date,
    start: time,
    *,
    appointment_id: int | None = None,
) -> dict:
    """What DeepSeek's ``reschedule_appointment`` call produces."""
    return call(
        session,
        requester,
        "reschedule_appointment",
        {
            "appointment_id": appointment_id if appointment_id is not None else world.original.id,
            "slot_id": world.stable_slot(day, start),
        },
    )


def confirm_move(session: Session, requester: Requester, proposal_id: str) -> dict:
    return call(session, requester, "confirm_appointment_change", {"proposal_id": proposal_id})


def requester_in(session: Session, patient_id: int, session_id: int, conversation_id: int):
    """Rebuild the requester in another session, as a second request would."""
    return Requester(
        patient=session.get(Patient, patient_id),
        patient_session=session.get(PatientSession, session_id),
        conversation=session.get(Conversation, conversation_id),
    )


def active_for(db: SessionFactory, patient_id: int) -> list[int]:
    """Every live booking: booked, in the future, not retired by a move."""
    with open_session(db) as session:
        return [
            view.appointment.id for view in appointments.active_for_patient(session, patient_id)
        ]


def get(db: SessionFactory, row_id: int) -> Appointment | None:
    with open_session(db) as session:
        return session.get(Appointment, row_id)


def slot_is_free(db: SessionFactory, slot_id: int) -> bool:
    return slot_holder(db, slot_id) is None


def slot_holder(db: SessionFactory, slot_id: int) -> int | None:
    with open_session(db) as session:
        return session.scalar(
            select(SlotClaim.appointment_id).where(
                SlotClaim.availability_slot_id == slot_id, SlotClaim.active.is_(True)
            )
        )


def pending_ids(db: SessionFactory, conversation_id: int) -> list[str]:
    with open_session(db) as session:
        return [row.public_id for row in pending_for_conversation(session, conversation_id)]


def appointments_for(db: SessionFactory, patient_id: int) -> list[Appointment]:
    with open_session(db) as session:
        return list(
            session.scalars(select(Appointment).where(Appointment.patient_id == patient_id))
        )


def replacements_for(db: SessionFactory, patient_id: int) -> list[Appointment]:
    return [row for row in appointments_for(db, patient_id) if row.rescheduled_from_id is not None]


# --------------------------------------------------------------------------- #
# The regression scenario
# --------------------------------------------------------------------------- #


def test_successful_reschedule_moves_the_appointment(db, world_session):
    session, world, requester = world_session
    original_id = world.original.id
    original_slot_id = world.slot(ORIGINAL_DAY, time(10, 0)).id
    new_slot_id = world.slot(REPLACEMENT_DAY, time(12, 30)).id

    proposal = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))
    assert proposal["ok"] is True, proposal
    assert proposal["kind"] == "reschedule"
    assert proposal["appointment_unchanged"] is True
    assert proposal["target_appointment_id"] == original_id
    assert proposal["new_starts_at"].startswith("2030-05-03T12:30")

    card = proposal["cards"][0]
    assert card["kind"] == "reschedule"
    assert card["target_appointment_id"] == original_id
    # The card shows the move itself: where it was, and where it would go.
    assert card["items"][0]["original_starts_at"].startswith("2030-05-01T10:00")
    assert card["items"][0]["starts_at"].startswith("2030-05-03T12:30")

    # Selecting a replacement never modifies the original.
    original = get(db, original_id)
    assert original.status == AppointmentStatus.BOOKED.value
    assert original.starts_at == at(ORIGINAL_DAY, time(10, 0))
    assert original.rescheduled_to_id is None
    assert slot_holder(db, original_slot_id) == original_id

    result = confirm_move(session, requester, proposal["proposal_id"])
    assert result["ok"] is True, result
    assert result["confirmed"] is True
    assert result["kind"] == "reschedule"
    assert result["replayed"] is False
    assert result["released_appointment_ids"] == [original_id]
    assert len(result["appointments"]) == 1

    replacement_id = result["appointments"][0]["appointment_id"]
    assert replacement_id != original_id

    original = get(db, original_id)
    replacement = get(db, replacement_id)

    # 1. The original is retired, not deleted, and points forward.
    assert original.status == AppointmentStatus.RESCHEDULED.value
    assert original.rescheduled_to_id == replacement_id
    assert original.rescheduled_at is not None
    assert original.cancelled_at is None  # not confused with a cancellation
    assert original.starts_at == at(ORIGINAL_DAY, time(10, 0))  # history intact
    assert original.duration_minutes == 60

    # 2. The replacement is a real booking with the original's patient and visit.
    assert replacement.status == AppointmentStatus.BOOKED.value
    assert replacement.rescheduled_from_id == original_id
    assert replacement.patient_id == world.patient.id
    assert replacement.provider_id == world.provider.id
    assert replacement.appointment_type_id == world.cleaning.id
    assert replacement.starts_at == at(REPLACEMENT_DAY, time(12, 30))
    assert replacement.ends_at == at(REPLACEMENT_DAY, time(13, 30))
    assert replacement.duration_minutes == 60
    assert replacement.rescheduled_from_starts_at == at(ORIGINAL_DAY, time(10, 0))

    # 3. Exactly one active appointment remains, in every view the app uses.
    assert active_for(db, world.patient.id) == [replacement_id]
    with open_session(db) as fresh:
        upcoming = appointments.upcoming_for_patient(fresh, world.patient.id)
        assert [view.appointment.id for view in upcoming] == [replacement_id]
        listed = appointments.list_for_patient(fresh, world.patient.id)
        assert [view.appointment.id for view in listed if view.is_upcoming] == [replacement_id]
        # The retired original is still in the history, just not upcoming.
        assert {view.appointment.id for view in listed} == {original_id, replacement_id}

    # 4. The original slot is free again; the new one is held by the replacement.
    assert slot_is_free(db, original_slot_id)
    assert slot_holder(db, new_slot_id) == replacement_id
    with open_session(db) as fresh:
        assert (
            is_interval_bookable(
                fresh,
                provider_id=world.provider.id,
                starts_at=at(ORIGINAL_DAY, time(10, 0)),
                duration_minutes=60,
                appointment_type=world.cleaning,
            )
            is not None
        )

    # 5. The proposal is resolved and its card describes a move, not a booking.
    assert proposal["proposal_id"] not in pending_ids(db, requester.conversation.id)
    confirmation = result["cards"][0]
    assert confirmation["type"] == "confirmation"
    assert confirmation["kind"] == "reschedule"
    assert confirmation["title"] == "Appointment moved"
    assert confirmation["moved_from"]["appointment_id"] == original_id
    assert confirmation["moved_from"]["starts_at"].startswith("2030-05-01T10:00")
    assert confirmation["released_appointment_ids"] == [original_id]


def test_staff_dashboard_sees_one_active_visit_with_full_history(db, world_session):
    session, world, requester = world_session
    proposal = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))
    result = confirm_move(session, requester, proposal["proposal_id"])
    replacement_id = result["appointments"][0]["appointment_id"]

    from app.services import staff_scheduling

    with open_session(db) as fresh:
        rows = staff_scheduling.list_appointments(
            fresh,
            staff_scheduling.AppointmentFilters(
                day_from=ORIGINAL_DAY, upcoming_only=True
            ),
        )
        assert [row.id for row in rows] == [replacement_id]

        original = staff_scheduling.get_appointment(fresh, world.original.id)
        replacement = staff_scheduling.get_appointment(fresh, replacement_id)
        original_payload = staff_scheduling.appointment_dict(original)
        replacement_payload = staff_scheduling.appointment_dict(replacement)

    assert original_payload["status"] == "rescheduled"
    assert original_payload["is_upcoming"] is False
    assert original_payload["rescheduled_to_id"] == replacement_id
    assert replacement_payload["is_upcoming"] is True
    assert replacement_payload["rescheduled_from_id"] == world.original.id
    assert replacement_payload["rescheduled_from_starts_at"].startswith("2030-05-01T10:00")


# --------------------------------------------------------------------------- #
# Rejection, expiry, conflict
# --------------------------------------------------------------------------- #


def test_declining_a_reschedule_preserves_the_original(db, world_session):
    session, world, requester = world_session
    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]

    declined = call(session, requester, "decline_booking_proposal", {"proposal_id": proposal_id})
    assert declined["ok"] is True, declined
    assert declined["declined"] is True

    original = get(db, world.original.id)
    assert original.status == AppointmentStatus.BOOKED.value
    assert original.rescheduled_to_id is None
    assert active_for(db, world.patient.id) == [world.original.id]
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, time(12, 30)).id)
    assert slot_holder(db, world.slot(ORIGINAL_DAY, time(10, 0)).id) == world.original.id


def test_expiring_a_reschedule_preserves_the_original(db, world_session):
    session, world, requester = world_session
    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]

    # Retire the proposal outright (as a decline or a supersede would). A hold
    # simply lapsing is *not* this: it frees the interval but keeps the card
    # confirmable, so the patient cannot lose a time they actually chose.
    with open_session(db) as fresh:
        stale = find_by_public_id(fresh, proposal_id)
        expire_proposal(fresh, stale, utcnow())
        stale.status = ProposalStatus.EXPIRED.value
        stale.resolved_at = utcnow()
        fresh.commit()

    original = get(db, world.original.id)
    assert original.status == AppointmentStatus.BOOKED.value
    assert original.rescheduled_to_id is None
    assert len(active_for(db, world.patient.id)) == 1
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, time(12, 30)).id)

    # A stale card cannot move anything afterwards.
    stale = confirm_move(session, requester, proposal_id)
    assert stale["ok"] is False
    assert stale["appointment_unchanged"] is True
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value


def test_confirmation_conflict_leaves_the_original_booked(db, world_session):
    """The held replacement is taken by someone else before the patient confirms."""
    session, world, requester = world_session
    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]
    replacement_slot_id = world.slot(REPLACEMENT_DAY, time(12, 30)).id

    # Another patient's booking lands on the held interval. Dropping our hold
    # first is not cheating: it is the same end state as holding it and losing
    # the race, and it keeps the fixture honest about the claims table.
    for claim in session.scalars(
        select(SlotClaim).where(
            SlotClaim.held_by_proposal_id.is_not(None), SlotClaim.active.is_(True)
        )
    ):
        claim.active = False
    session.flush()
    other = world.book(
        session,
        patient_id=world.other_patient.id,
        day=REPLACEMENT_DAY,
        start=time(12, 30),
        intervals=2,
        appointment_type=world.cleaning,
    )
    session.commit()
    other_id = other.id

    result = confirm_move(session, requester, proposal_id)
    assert result["ok"] is False, result
    assert result["error"] == "slot_taken"
    assert result["appointment_unchanged"] is True
    assert result["kind"] == "reschedule"
    session.commit()

    original = get(db, world.original.id)
    assert original.status == AppointmentStatus.BOOKED.value
    assert original.rescheduled_to_id is None
    assert slot_holder(db, world.slot(ORIGINAL_DAY, time(10, 0)).id) == world.original.id

    # No replacement was ever booked.
    assert replacements_for(db, world.patient.id) == []
    # The other patient's booking is untouched.
    assert get(db, other_id).status == AppointmentStatus.BOOKED.value
    assert slot_holder(db, replacement_slot_id) == other_id
    # The proposal stays pending so the patient can choose again.
    assert pending_ids(db, requester.conversation.id) == [proposal_id]


# --------------------------------------------------------------------------- #
# Repeated confirmation and concurrency
# --------------------------------------------------------------------------- #


def test_repeated_confirmation_replays_without_another_booking(db, world_session):
    session, world, requester = world_session
    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]

    first = confirm_move(session, requester, proposal_id)
    assert first["ok"] is True and first["replayed"] is False
    session.commit()

    for _ in range(3):
        again = confirm_move(session, requester, proposal_id)
        assert again["ok"] is True, again
        assert again["replayed"] is True
        assert again["appointments"] == first["appointments"]
        assert again["released_appointment_ids"] == first["released_appointment_ids"]
        assert again["kind"] == "reschedule"
        assert again["cards"][0]["kind"] == "reschedule"

    # The UI's repeat path goes straight to the service; it must replay too.
    with open_session(db) as fresh:
        request = requester_in(
            fresh, world.patient.id, requester.patient_session.id, requester.conversation.id
        )
        direct = scheduling.confirm(fresh, requester=request, proposal_id=proposal_id)
    assert direct["ok"] is True and direct["replayed"] is True

    rows = appointments_for(db, world.patient.id)
    assert len(rows) == 2  # the original plus exactly one replacement
    assert len(active_for(db, world.patient.id)) == 1


def test_concurrent_confirmations_move_the_visit_once(db, world_session):
    session, world, requester = world_session
    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]

    patient_id = world.patient.id
    session_id = requester.patient_session.id
    conversation_id = requester.conversation.id
    original_id = world.original.id
    outcomes: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            with open_session(db) as fresh:
                # Independent sessions: exactly how two clicks arrive.
                request = requester_in(fresh, patient_id, session_id, conversation_id)
                barrier.wait(timeout=10)
                outcomes.append(
                    scheduling.confirm(fresh, requester=request, proposal_id=proposal_id)
                )
        except BaseException as exc:  # pragma: no cover - surfaced by the asserts
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(outcomes) == 2, outcomes
    assert all(outcome["ok"] for outcome in outcomes), outcomes
    # One confirmation did the work; the other replayed the committed result.
    assert sorted(outcome["replayed"] for outcome in outcomes) == [False, True]

    original = get(db, original_id)
    assert original.status == AppointmentStatus.RESCHEDULED.value
    assert original.rescheduled_to_id is not None
    assert len(appointments_for(db, patient_id)) == 2
    assert active_for(db, patient_id) == [original.rescheduled_to_id]


def test_a_second_replacement_supersedes_the_first_card(db, world_session):
    """Two replacement times chosen in quick succession cannot both stay live."""
    session, world, requester = world_session
    first = propose_move(session, requester, world, REPLACEMENT_DAY, time(10, 0))
    assert first["ok"] is True, first
    second = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))
    assert second["ok"] is True, second

    # The newer card replaces the older one for the same appointment, and the
    # first replacement's hold was released.
    assert pending_ids(db, requester.conversation.id) == [second["proposal_id"]]
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, time(10, 0)).id)

    # Confirming the stale card cannot move the visit.
    stale = confirm_move(session, requester, first["proposal_id"])
    assert stale["ok"] is False
    assert stale["appointment_unchanged"] is True
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value

    confirmed = confirm_move(session, requester, second["proposal_id"])
    assert confirmed["ok"] is True, confirmed

    # Confirming the stale card once more, after the move, changes nothing.
    stale_after = confirm_move(session, requester, first["proposal_id"])
    assert stale_after["ok"] is False
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, time(10, 0)).id)
    assert len(active_for(db, world.patient.id)) == 1


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #


def test_failure_after_releasing_claims_restores_everything(
    db: SessionFactory, world_session, monkeypatch: pytest.MonkeyPatch
):
    """The replacement fails *after* the original's claims were released.

    That is the precise point where a non-atomic implementation would leave the
    patient with no booking at all. The rollback must restore the original row,
    its claims, and leave no replacement behind.
    """
    session, world, requester = world_session
    from app.services import proposals as proposals_service

    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]
    original_id = world.original.id
    original_slot_id = world.slot(ORIGINAL_DAY, time(10, 0)).id
    new_slot_id = world.slot(REPLACEMENT_DAY, time(12, 30)).id

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated failure after claims were released")

    monkeypatch.setattr(proposals_service, "_create_reschedule_replacement", explode)
    with pytest.raises(RuntimeError):
        with open_session(db) as fresh:
            request = requester_in(
                fresh, world.patient.id, requester.patient_session.id, requester.conversation.id
            )
            scheduling.confirm(fresh, requester=request, proposal_id=proposal_id)
    monkeypatch.undo()

    original = get(db, original_id)
    assert original.status == AppointmentStatus.BOOKED.value
    assert original.rescheduled_to_id is None
    assert original.rescheduled_at is None
    assert original.starts_at == at(ORIGINAL_DAY, time(10, 0))
    # Its intervals are active again, so the time is still occupied by it.
    assert slot_holder(db, original_slot_id) == original_id
    # No replacement, and the visit is still the patient's only active booking.
    assert replacements_for(db, world.patient.id) == []
    assert active_for(db, world.patient.id) == [original_id]
    # Nothing half-booked was left on the new time either.
    assert slot_is_free(db, new_slot_id)

    # The proposal is still pending, so simply confirming again works.
    assert pending_ids(db, requester.conversation.id) == [proposal_id]
    retry = confirm_move(session, requester, proposal_id)
    assert retry["ok"] is True, retry
    assert len(active_for(db, world.patient.id)) == 1


def test_conflict_never_reaches_the_release_step(db, world_session):
    """A conflict found during revalidation leaves the original's claims intact."""
    session, world, requester = world_session
    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]

    # The clinic closes the replacement day after the card was shown.
    for row in world.slots[REPLACEMENT_DAY].values():
        row.status = SlotStatus.BLOCKED.value
    session.commit()

    result = confirm_move(session, requester, proposal_id)
    assert result["ok"] is False, result
    assert result["error"] == "slot_taken"
    session.commit()

    original = get(db, world.original.id)
    assert original.status == AppointmentStatus.BOOKED.value
    assert original.rescheduled_to_id is None
    with open_session(db) as fresh:
        claims = list(
            fresh.scalars(
                select(SlotClaim).where(
                    SlotClaim.appointment_id == world.original.id, SlotClaim.active.is_(True)
                )
            )
        )
    assert len(claims) == 2


# --------------------------------------------------------------------------- #
# Routing: a reschedule must never become a booking
# --------------------------------------------------------------------------- #


def test_reschedule_is_its_own_tool():
    assert {
        "reschedule_appointment",
        "create_booking_proposal",
        "confirm_booking_proposal",
        "confirm_appointment_change",
    } <= SCHEDULING_TOOL_NAMES


def test_verified_parent_can_register_and_link_a_new_minor_for_family_booking(db, world_session):
    session, world, requester = world_session

    result = call(
        session,
        requester,
        "register_child_for_verified_parent",
        {
            "full_name": "Noah Whitfield",
            "date_of_birth": "2017-05-14",
            "use_parent_insurance": True,
        },
    )

    assert result["ok"] is True, result
    assert result["registered"] is True
    assert result["linked"] is True
    assert result["first_name"] == "Noah"
    child = session.scalar(select(Patient).where(Patient.full_name == "Noah Whitfield"))
    assert child is not None
    assert child.phone == world.patient.phone
    assert child.insurance_provider == world.patient.insurance_provider
    group = session.scalar(select(FamilyGroup).where(FamilyGroup.id == result["family_group_id"]))
    assert group is not None
    links = list(
        session.scalars(
            select(FamilyMember).where(FamilyMember.family_group_id == group.id)
        )
    )
    assert {(link.patient_id, link.relation_to_primary) for link in links} == {
        (world.patient.id, "self"),
        (child.id, "child"),
    }
    allowed, _ = scheduling.check_family_authorisation(session, requester)
    assert [patient.id for patient in allowed] == [child.id]

def test_parent_cannot_use_child_registration_to_add_an_adult(db, world_session):
    session, _, requester = world_session

    result = call(
        session,
        requester,
        "register_child_for_verified_parent",
        {
            "full_name": "Taylor Whitfield",
            "date_of_birth": "1990-05-14",
            "use_parent_insurance": True,
        },
    )

    assert result["ok"] is False
    assert result["error"] == "adult_family_member"


def test_new_booking_is_blocked_while_a_move_is_pending(db, world_session):
    session, world, requester = world_session
    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]

    result = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, time(14, 0))],
            "appointment_type": "cleaning",
        },
    )
    assert result["ok"] is False
    assert result["error"] == "reschedule_in_progress"
    assert result["proposal_id"] == proposal_id
    assert result["appointment_id"] == world.original.id
    assert result["confirm_with"] == "confirm_appointment_change"
    # The rejected booking attempt held nothing.
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, time(14, 0)).id)


def test_booking_confirmation_refuses_a_move_proposal(db, world_session):
    session, world, requester = world_session
    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]

    result = call(session, requester, "confirm_booking_proposal", {"proposal_id": proposal_id})
    assert result["ok"] is False
    assert result["error"] == "wrong_proposal_kind"
    assert result["kind"] == "reschedule"
    original = get(db, world.original.id)
    assert original.status == AppointmentStatus.BOOKED.value
    assert original.rescheduled_to_id is None


def test_change_confirmation_refuses_a_booking_proposal(db, world_session):
    session, world, requester = world_session
    booking = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, time(14, 0))],
            "appointment_type": "cleaning",
        },
    )
    assert booking["ok"] is True, booking

    result = call(
        session, requester, "confirm_appointment_change", {"proposal_id": booking["proposal_id"]}
    )
    assert result["ok"] is False
    assert result["error"] == "wrong_proposal_kind"
    assert len(active_for(db, world.patient.id)) == 1


def test_reschedule_tool_refuses_an_unbookable_replacement(db, world_session):
    session, world, requester = world_session
    # The patient's own October 27 cleaning occupies that interval.
    result = propose_move(session, requester, world, ORIGINAL_DAY, time(10, 0))
    assert result["ok"] is False
    # The tool rejects the replacement before any proposal exists.
    assert result["error"] == "slot_unavailable"
    assert result["appointment_unchanged"] is True
    assert result["kind"] == "reschedule"
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value


def test_move_keeps_the_visits_own_type_and_duration(db, world_session):
    """A cleaning must not silently become a 30-minute checkup."""
    session, world, requester = world_session
    proposal = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))
    assert proposal["appointment_type"] == "cleaning"
    assert proposal["duration_minutes"] == 60
    result = confirm_move(session, requester, proposal["proposal_id"])
    replacement = get(db, result["appointments"][0]["appointment_id"])
    assert replacement.appointment_type_id == world.cleaning.id
    assert replacement.duration_minutes == 60


# --------------------------------------------------------------------------- #
# Ownership and family protections
# --------------------------------------------------------------------------- #


def test_cannot_reschedule_another_patients_appointment(db, world_session):
    session, world, requester = world_session
    foreign = world.book(
        session,
        patient_id=world.other_patient.id,
        day=REPLACEMENT_DAY,
        start=time(8, 0),
        intervals=2,
        appointment_type=world.cleaning,
    )
    session.commit()
    foreign_id = foreign.id
    foreign_start = foreign.starts_at

    result = propose_move(
        session,
        requester,
        world,
        REPLACEMENT_DAY,
        time(15, 0),
        appointment_id=foreign_id,
    )
    assert result["ok"] is False
    # Deliberately indistinguishable from "does not exist".
    assert result["error"] == "unknown_appointment"
    session.commit()

    untouched = get(db, foreign_id)
    assert untouched.status == AppointmentStatus.BOOKED.value
    assert untouched.starts_at == foreign_start
    assert replacements_for(db, world.other_patient.id) == []


def test_an_already_moved_appointment_cannot_be_moved_again(db, world_session):
    session, world, requester = world_session
    first = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))
    confirm_move(session, requester, first["proposal_id"])
    session.commit()

    again = propose_move(session, requester, world, REPLACEMENT_DAY, time(15, 0))
    assert again["ok"] is False
    assert again["appointment_unchanged"] is True
    assert len(active_for(db, world.patient.id)) == 1


def test_family_booking_path_is_unchanged(db, world_session):
    """An ordinary booking still goes through create -> confirm and books once."""
    session, world, requester = world_session
    booking = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, time(14, 0))],
            "appointment_type": "cleaning",
        },
    )
    assert booking["ok"] is True, booking
    confirmed = call(
        session, requester, "confirm_booking_proposal", {"proposal_id": booking["proposal_id"]}
    )
    assert confirmed["ok"] is True, confirmed
    assert confirmed["released_appointment_ids"] == []
    assert len(confirmed["appointments"]) == 1
    # The original visit is untouched; the new booking is simply added.
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert len(active_for(db, world.patient.id)) == 2


def test_upgraded_database_accepts_the_rescheduled_status(tmp_path: Path):
    """An existing SQLite file gets the widened status CHECK on startup.

    A database created before ``rescheduled`` existed rejects that value outright.
    ``apply_additive_migrations`` must rebuild ``appointments`` (SQLite cannot
    alter a CHECK) while keeping every row, so confirmation can write the status.
    """
    from sqlalchemy import text

    from app.migrations import apply_additive_migrations

    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'upgrade.db'}")
    try:
        # A table whose CHECK is the pre-reschedule definition, with a row in it.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE appointments ("
                    " id INTEGER PRIMARY KEY,"
                    " patient_id INTEGER NOT NULL,"
                    " starts_at DATETIME NOT NULL,"
                    " ends_at DATETIME NOT NULL,"
                    " duration_minutes INTEGER NOT NULL,"
                    " status VARCHAR(16) NOT NULL DEFAULT 'booked'"
                    "  CONSTRAINT ck_appointment_status CHECK"
                    "  (status IN ('booked', 'cancelled', 'completed')))"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO appointments"
                    " (id, patient_id, starts_at, ends_at, duration_minutes, status)"
                    " VALUES (1, 7, '2030-05-01 10:00:00', '2030-05-01 11:00:00', 60, 'booked')"
                )
            )

        with engine.begin() as connection:
            applied = apply_additive_migrations(connection)
            schema = connection.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'appointments'")
            ).scalar_one()

        assert "appointments.status:add 'rescheduled'" in applied
        assert "rescheduled" in schema
        # The rebuild kept the row.
        with engine.begin() as connection:
            row = connection.execute(
                text("SELECT patient_id, status FROM appointments WHERE id = 1")
            ).one()
            assert tuple(row) == (7, "booked")
            connection.execute(
                text("UPDATE appointments SET status = 'rescheduled' WHERE id = 1")
            )
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# Probe: which lock step made the concurrent confirm replay instead of racing
# --------------------------------------------------------------------------- #


def test_concurrent_confirmations_both_hold_the_lock(db, world_session):
    """Two threads must not both commit a booking group for one proposal.

    ``confirm_proposal`` re-reads the proposal's status under ``BEGIN IMMEDIATE``,
    so the second writer sees ``accepted`` and replays. The observable proof is
    that exactly one booking group exists for the proposal afterwards.
    """
    from app.models import BookingGroup

    session, world, requester = world_session
    proposal_id = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))[
        "proposal_id"
    ]
    patient_id = world.patient.id
    session_id = requester.patient_session.id
    conversation_id = requester.conversation.id

    def worker() -> None:
        with open_session(db) as fresh:
            request = requester_in(fresh, patient_id, session_id, conversation_id)
            scheduling.confirm(fresh, requester=request, proposal_id=proposal_id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with open_session(db) as fresh:
        proposal = find_by_public_id(fresh, proposal_id)
        assert proposal.status == "accepted"
        groups = list(
            fresh.scalars(select(BookingGroup).where(BookingGroup.proposal_id == proposal.id))
        )
        assert len(groups) == 1
        # The idempotency record the confirmation wrote is unique to it.
        assert proposal.confirm_key is not None
    assert len(appointments_for(db, patient_id)) == 2


# --------------------------------------------------------------------------- #
# Frontend contract: the cards the React app renders must agree with the API
# --------------------------------------------------------------------------- #


def test_confirmation_card_carries_the_fields_the_ui_reads(db, world_session):
    session, world, requester = world_session
    proposal = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))
    proposal_card = proposal["cards"][0]
    assert proposal_card["actions"] == ["confirm", "change", "cancel"]
    assert proposal_card["items"][0]["patient_first_name"] == "Dana"

    result = confirm_move(session, requester, proposal["proposal_id"])
    card = result["cards"][0]
    # Fields read by ConfirmationCardView / useCardActions in the frontend.
    assert card["kind"] == "reschedule"
    assert card["appointments"][0]["status"] == "booked"
    assert card["appointments"][0]["patient_id"] == world.patient.id
    assert card["moved_from"]["status"] == "rescheduled"
    assert card["replayed"] is False
    assert result["appointment_unchanged"] is False


def test_appointment_list_marks_the_pair_for_deep_linking(db, world_session):
    session, world, requester = world_session
    proposal = propose_move(session, requester, world, REPLACEMENT_DAY, time(12, 30))
    result = confirm_move(session, requester, proposal["proposal_id"])
    replacement_id = result["appointments"][0]["appointment_id"]

    with open_session(db) as fresh:
        payload = [
            view.as_dict() for view in appointments.list_for_patient(fresh, world.patient.id)
        ]

    by_id = {entry["appointment_id"]: entry for entry in payload}
    assert by_id[world.original.id]["rescheduled_to_id"] == replacement_id
    assert by_id[replacement_id]["rescheduled_from_id"] == world.original.id
    upcoming = [entry for entry in payload if entry["is_upcoming"]]
    assert [entry["appointment_id"] for entry in upcoming] == [replacement_id]
