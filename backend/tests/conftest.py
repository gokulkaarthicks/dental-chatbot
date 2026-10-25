"""Shared fixtures for backend tests.

``scenario`` builds a throwaway SQLite database holding one verified patient and
a two-week grid of open cleaning slots, and points every ``app.db`` helper at it
for the duration of the test. It lived inside ``test_search_after_decline.py``
until a second module needed the same world; keeping one definition means the
two cannot drift apart.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.db import create_db_engine
from app.models import (
    AppointmentType,
    AvailabilitySlot,
    Base,
    Conversation,
    Patient,
    PatientSession,
    Provider,
    ProviderSpecialty,
    SlotStatus,
    utcnow,
)
from app.services import patient_sessions
from app.services.scheduling import Requester
from app.timeutil import CLOSE_TIME, OPEN_TIME, SLOT_MINUTES

# 2030-05-06 is a Monday; the grid runs two weeks out so "today" cannot remove it.
BOOKING_DAY = date(2030, 5, 6)
# An explicit, unambiguous date, so tests never depend on the weekday they run.
SEARCH_PHRASE = "2030-05-07"


@pytest.fixture()
def scenario(tmp_path):
    """A verified patient with a two-week grid and its conversation."""
    from sqlalchemy.orm import sessionmaker

    from app import db as db_module

    path = tmp_path / "decline-test.db"
    engine = create_db_engine(f"sqlite+pysqlite:///{path}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    previous_engine = db_module._engine
    previous_factory = db_module._session_factory
    previous_get_engine = db_module.get_engine
    previous_path = db_module.database_path
    db_module._engine = engine
    db_module._session_factory = factory
    # ``create_db_engine`` still resolves the project path internally, so point
    # every path helper at the throwaway file for the duration of the test.
    db_module.get_engine = lambda: engine
    db_module.database_path = lambda: path
    Base.metadata.create_all(bind=engine)
    session = factory()
    try:
        provider = Provider(
            full_name="Dr. Marion Varga",
            credentials="DDS",
            email="varga@example.test",
            bio=None,
            active=True,
        )
        session.add(provider)
        session.flush()
        session.add(ProviderSpecialty(provider_id=provider.id, specialty="General Dentistry"))
        session.add(
            AppointmentType(
                code="cleaning",
                name="Cleaning",
                description=None,
                duration_minutes=60,
                is_emergency=False,
                required_specialty="General Dentistry",
                sort_order=1,
            )
        )
        patient = Patient(
            full_name="Dana Whitfield",
            phone="+15550101234",
            date_of_birth=date(1990, 4, 12),
            insurance_provider="Self-pay",
        )
        session.add(patient)
        session.flush()
        for offset in range(0, 14):
            day = BOOKING_DAY + timedelta(days=offset)
            if day.weekday() >= 6:
                continue
            cursor = datetime.combine(day, OPEN_TIME)
            end = datetime.combine(day, CLOSE_TIME)
            while cursor < end:
                session.add(
                    AvailabilitySlot(
                        provider_id=provider.id,
                        starts_at=cursor,
                        ends_at=cursor + timedelta(minutes=SLOT_MINUTES),
                        status=SlotStatus.AVAILABLE.value,
                    )
                )
                cursor += timedelta(minutes=SLOT_MINUTES)

        record = patient_sessions.create_patient_session(session)
        patient_session = session.get(PatientSession, record.session_id)
        patient_sessions.mark_verified(
            session,
            patient_session,
            patient,
            method=patient_sessions.VERIFICATION_BY_LOOKUP,
        )
        conversation = Conversation(
            public_id="conv_decline_test",
            patient_session_id=patient_session.id,
            status="active",
            patient_id=patient.id,
            reset_generation=record.reset_generation,
            started_at=utcnow(),
            last_message_at=utcnow(),
        )
        session.add(conversation)
        session.commit()
        yield session, Requester(
            patient=patient, patient_session=patient_session, conversation=conversation
        )
    finally:
        session.close()
        db_module._engine = previous_engine
        db_module._session_factory = previous_factory
        db_module.get_engine = previous_get_engine
        db_module.database_path = previous_path
        engine.dispose()
