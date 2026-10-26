"""Shared fixtures for backend tests.

``scenario`` builds a throwaway SQLite database holding one clinic, one verified
patient and a two-week grid of open cleaning slots, and points every ``app.db``
helper at it for the duration of the test. It lived inside
``test_search_after_decline.py`` until a second module needed the same world;
keeping one definition means the two cannot drift apart.

Multi-tenancy changed what "a world" means. A clinic row has to exist before any
tenant-owned row can be written, and the session must be *bound* to it, because
that binding is what filters reads and rejects cross-tenant writes in
``app.models.base``. So the fixture creates the tenant first, binds the session
to it, and only then seeds the grid the tests book against.

The business hours are written out rather than imported from
``app.timeutil.LEGACY_CLINIC_SCHEDULING`` on purpose: a fixture that inherits the
legacy grid cannot also prove that a second clinic with a different grid works,
and the lint gate bans that import everywhere a tenant's own config should be
read instead.

``scenario_tenant_b`` builds the second clinic *inside the same database* as
``scenario``. Two databases would make every isolation assertion pass for the
wrong reason -- the other tenant's rows would be unreachable no matter what the
query did -- so the two-tenant tests share one file and one engine.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from app import tenancy
from app.db import bind_tenant, create_db_engine
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
from tests.support import seed_clinic

# 2030-05-06 is a Monday; the grid runs two weeks out so "today" cannot remove it.
BOOKING_DAY = date(2030, 5, 6)
# An explicit, unambiguous date, so tests never depend on the weekday they run.
SEARCH_PHRASE = "2030-05-07"


@pytest.fixture()
def world(tmp_path):
    """The seeded clinic, exposed as an object for tests that need more.

    ``scenario`` hands back a session and a requester, which is enough for a
    turn. Tests that assert on rows the requester cannot reach (an alert, a
    proposal, a second patient) need the identifiers and the clinic context, so
    they take this instead.
    """
    built = build_world(tmp_path, slug="test-clinic")
    yield built
    built.teardown()


@pytest.fixture()
def scenario(world):
    """A verified patient with a two-week grid and its conversation."""
    yield world.session, Requester(
        patient=world.patient,
        patient_session=world.patient_session,
        conversation=world.conversation,
        clinic=world.clinic,
    )


@pytest.fixture()
def scenario_tenant_b(world):
    """A second clinic in the *same* database as ``world``, with its own grid.

    Isolation can only be proved inside one file: two worlds in two databases
    would pass whatever the code did, because a query could never reach the
    other tenant's rows in the first place. So this adds a tenant the same way
    the seeding path does -- through ``tenancy.create_clinic`` -- and gives it a
    grid that is deliberately unlike tenant A's: 20-minute slots, Eastern time,
    Tuesday-Saturday. Any answer that leaks from A is then visible as a
    30-minute Pacific Monday, not as a subtle difference.

    The grid constants are written out here for the same reason the module
    docstring gives: importing the legacy single-clinic constants would make
    both tenants share one grid, which is the bug this fixture exists to catch.
    """
    built = add_tenant(
        world,
        slug="test-clinic-b",
        name="Test Clinic B",
        slot_minutes=20,
        open_time=time(9, 0),
        close_time=time(17, 0),
        open_weekdays=frozenset({1, 2, 3, 4, 5}),
        timezone="America/New_York",
        phone="+16505550199",
        patient_name="Nora Baptiste",
        date_of_birth=date(1985, 1, 9),
        conversation_public_id="conv_tenant_b",
    )
    yield built


class TestWorld:
    """A seeded tenant plus the patching that made its database current.

    Deliberately plain rather than a fixture factory: the attributes are the ones
    a test already had to have, and ``teardown`` is the inverse of what
    ``build_world`` patched, in the same order.

    ``owns_patches`` is what lets a *second* tenant join an existing world.
    ``build_world`` swapped the global engine and session factory, so only the
    call that did the swapping may restore them; a tenant added later closes its
    own session and leaves the database in place for the rest of the test.
    """

    def __init__(self, *, owns_patches: bool = True, **values) -> None:  # noqa: ANN003
        self.owns_patches = owns_patches
        self.__dict__.update(values)

    def teardown(self) -> None:
        from app import db as db_module

        self.session.close()
        if not self.owns_patches:
            return
        db_module._engine = self.previous_engine
        db_module._session_factory = self.previous_factory
        db_module.get_engine = self.previous_get_engine
        db_module.database_path = self.previous_path
        # The tenant snapshot cache is process-global and keyed on clinic id.
        # Every throwaway database here starts at clinic 1, so without this a
        # later test could be served the previous test's hours.
        tenancy.invalidate_cache()
        self.engine.dispose()


def build_world(tmp_path, *, slug: str = "test-clinic", clinic_name: str = "Test Clinic") -> TestWorld:  # noqa: ANN001
    """Create the throwaway database, seed it, and point ``app.db`` at it."""
    from sqlalchemy.orm import sessionmaker

    from app import db as db_module

    path = tmp_path / f"{slug}.db"
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
    tenancy.invalidate_cache()

    # The clinic is created on its own (unbound) session and committed, because
    # creating a tenant is the one write that must not be filtered by a tenant.
    admin = factory()
    try:
        clinic = seed_clinic(admin, slug=slug, name=clinic_name)
    finally:
        admin.close()

    session = bind_tenant(factory(), clinic.clinic_id)
    seeded = _seed_tenant(
        session,
        clinic,
        phone="+16505550123",
        patient_name="Dana Whitfield",
        date_of_birth=date(1990, 4, 12),
        conversation_public_id="conv_decline_test",
    )

    return TestWorld(
        session=session,
        engine=engine,
        factory=factory,
        path=path,
        clinic=clinic,
        previous_engine=previous_engine,
        previous_factory=previous_factory,
        previous_get_engine=previous_get_engine,
        previous_path=previous_path,
        **seeded,
    )


def add_tenant(
    world: TestWorld,
    *,
    slug: str,
    name: str,
    phone: str,
    patient_name: str,
    date_of_birth: date,
    conversation_public_id: str,
    **grid,
) -> TestWorld:  # noqa: ANN003
    """Insert one more clinic into ``world``'s database and seed it the same way.

    Same shape as ``build_world`` minus the engine swapping: the caller already
    owns the patched module globals, so this only opens a session bound to the
    new tenant and hands back the object model a test expects.

    Contact details are arguments rather than shared constants so the two
    tenants in an isolation test do *not* look alike: a lookup that leaks would
    have to return the wrong person's name, not the right one twice.
    """
    admin = world.factory()
    try:
        clinic = seed_clinic(admin, slug=slug, name=name, **grid)
    finally:
        admin.close()

    session = bind_tenant(world.factory(), clinic.clinic_id)
    seeded = _seed_tenant(
        session,
        clinic,
        phone=phone,
        patient_name=patient_name,
        date_of_birth=date_of_birth,
        conversation_public_id=conversation_public_id,
    )
    return TestWorld(
        session=session,
        engine=world.engine,
        factory=world.factory,
        path=world.path,
        clinic=clinic,
        owns_patches=False,
        **seeded,
    )


def _seed_tenant(
    session,  # noqa: ANN001
    clinic,  # noqa: ANN001
    *,
    phone: str,
    patient_name: str,
    date_of_birth: date,
    conversation_public_id: str,
) -> dict:
    """Everything one clinic needs to run a turn: staff, a patient, a grid.

    Kept separate from ``build_world`` so a second tenant is seeded by the same
    code as the first. Two hand-written seedings would drift, and the drift
    would show up as an isolation test that passes because the two worlds differ
    in some unrelated way.
    """
    provider = Provider(
        clinic_id=clinic.clinic_id,
        full_name="Dr. Marion Varga",
        credentials="DDS",
        email="varga@example.test",
        bio=None,
        active=True,
    )
    session.add(provider)
    session.flush()
    session.add(
        ProviderSpecialty(
            clinic_id=clinic.clinic_id, provider_id=provider.id, specialty="General Dentistry"
        )
    )
    session.add(
        AppointmentType(
            clinic_id=clinic.clinic_id,
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
        clinic_id=clinic.clinic_id,
        full_name=patient_name,
        phone=phone,
        date_of_birth=date_of_birth,
        insurance_provider="Self-pay",
    )
    session.add(patient)
    session.flush()
    # The grid follows the *tenant's* hours, not the module constants: a clinic
    # open Tuesday-Saturday must not be handed Monday slots by its own fixture.
    config = clinic.config
    for offset in range(0, 14):
        day = BOOKING_DAY + timedelta(days=offset)
        if day.weekday() not in config.open_weekdays:
            continue
        for starts_at in config.day_slot_starts(day):
            session.add(
                AvailabilitySlot(
                    clinic_id=clinic.clinic_id,
                    provider_id=provider.id,
                    starts_at=starts_at,
                    ends_at=starts_at + config.slot_length,
                    status=SlotStatus.AVAILABLE.value,
                )
            )

    record = patient_sessions.create_patient_session(session)
    patient_session = session.get(PatientSession, record.session_id)
    patient_sessions.mark_verified(
        session,
        patient_session,
        patient,
        method=patient_sessions.VERIFICATION_BY_LOOKUP,
    )
    conversation = Conversation(
        clinic_id=clinic.clinic_id,
        public_id=conversation_public_id,
        patient_session_id=patient_session.id,
        status="active",
        patient_id=patient.id,
        reset_generation=record.reset_generation,
        started_at=utcnow(),
        last_message_at=utcnow(),
    )
    session.add(conversation)
    session.commit()

    return {
        "provider": provider,
        "patient": patient,
        "patient_session": patient_session,
        "conversation": conversation,
        "token": record.token,
    }
