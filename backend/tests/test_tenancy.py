"""Tenant isolation: one database, two clinics, no path between them.

The migration's whole promise is that a second clinic can be added without the
first one noticing, and that no read path can be talked into crossing over. That
promise is made of five separate mechanisms, so this module tests each one where
it actually lives:

* **Resolution** -- ``X-Clinic`` picks a tenant, and nothing else does. A typo'd
  slug must be a 404 rather than a quiet fall back to the default clinic, because
  falling back would show a patient the wrong practice's hours.
* **Pinning** -- once a credential exists it *is* the tenant. A token from clinic
  A must be refused under a clinic-B header rather than served B's data.
* **The ORM net** -- a ``select()`` a developer forgot to filter still returns one
  clinic's rows. This is the mechanism that scales, so it is tested on purpose
  with a query that has no ``.where()`` at all, run clinic A first (the order that
  exposed the loader-criteria cache bug).
* **The insert guard** -- a row stamped for the wrong clinic, or for no clinic,
  raises instead of being written.
* **Tenant-owned knowledge** -- hours, carriers and policy text differ per clinic,
  so an answer that came from a module constant would be visible here.

The two worlds live in *one* database (``scenario`` plus ``scenario_tenant_b``).
Two files would make every assertion pass for the wrong reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tenancy
from app.api.clinic import get_clinic, list_clinics
from app.api.deps import (
    CLINIC_MISMATCH_DETAIL,
    PATIENT_CLINIC_MISMATCH_DETAIL,
    get_clinic_context,
    get_patient_session,
    get_staff_session,
)
from app.models import (
    AlertStatus,
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AuditRecord,
    AvailabilitySlot,
    BookingProposal,
    ClinicInsurancePlan,
    ClinicPolicy,
    Conversation,
    IdempotencyRecord,
    Patient,
    PendingRequest,
    ProposalStatus,
    Provider,
    RequestStatus,
    StaffAlert,
    StaffRole,
    StaffUser,
    TenantMismatch,
    utcnow,
)
from app.services import audit, generations, patients, practice, staff_auth
from app.services import staff_handoff_board as board
from app.services.availability_read import find_availability
from app.services.proposals import find_by_public_id
from app.tenancy import ClinicContext
from app.validation import normalize_insurance

# Aliased because a bare ``TestWorld`` import is collected by pytest as a test
# class (it starts with "Test" and has a constructor).
from tests.conftest import TestWorld as World

# --------------------------------------------------------------------------- #
# The two-clinic world
# --------------------------------------------------------------------------- #


@dataclass
class TwoClinics:
    """Both tenants of one database, each with a fact of every kind.

    ``alpha`` is the default clinic (the one a header-less request resolves to),
    ``beta`` is the deliberately unlike one: 20-minute slots, Eastern time,
    Tuesday-Saturday. Any value that crosses over is therefore obvious -- a
    leaked answer reads as a 30-minute Pacific Monday.
    """

    alpha_session: Session
    beta_session: Session
    alpha: ClinicContext
    beta: ClinicContext
    alpha_patient: Patient
    beta_patient: Patient
    alpha_token: str
    beta_token: str
    alpha_staff_token: str
    beta_staff_token: str
    alpha_alert_id: int
    beta_alert_id: int
    alpha_proposal_id: str
    beta_proposal_id: str


def _populate(session: Session, clinic: ClinicContext, *, label: str) -> dict[str, object]:
    """Give one clinic a config row, a staff login, and one row of each kind.

    Written as one function called twice so the two tenants cannot drift: an
    isolation test that compares two differently built worlds proves nothing.
    """
    session.add_all(
        [
            ClinicInsurancePlan(
                clinic_id=clinic.clinic_id, name=f"{label} Health", accepted=True
            ),
            ClinicPolicy(
                clinic_id=clinic.clinic_id,
                topic="hours",
                question="When are you open?",
                answer=f"{label} sees patients {clinic.config.booking_days_phrase}, "
                f"{clinic.config.open_time:%H:%M}-{clinic.config.close_time:%H:%M}.",
                keywords=["hours", "open"],
            ),
        ]
    )
    session.flush()
    user = StaffUser(
        clinic_id=clinic.clinic_id,
        email=f"{label.lower()}@example.test",
        username=label.lower(),
        display_name=f"{label} Front Desk",
        role=StaffRole.RECEPTIONIST.value,
        password_hash="not-used-by-these-tests",
        active=True,
    )
    session.add(user)
    session.flush()
    issued = staff_auth.create_session(session, user=user)
    alert = StaffAlert(
        clinic_id=clinic.clinic_id,
        kind="emergency",
        severity="urgent",
        message=f"{label} emergency",
        status=AlertStatus.OPEN.value,
    )
    request = PendingRequest(
        clinic_id=clinic.clinic_id,
        request_type="callback",
        status=RequestStatus.PENDING.value,
        details=f"{label} callback",
    )
    session.add_all([alert, request])
    session.flush()
    # Every read below is already tenant-filtered, so these scalars can only find
    # rows belonging to ``clinic`` -- which is itself part of what is being tested.
    patient = session.scalar(select(Patient))
    provider = session.scalar(select(Provider))
    appointment_type = session.scalar(select(AppointmentType))
    conversation = session.scalar(select(Conversation))
    assert patient and provider and appointment_type and conversation
    visit = Appointment(
        clinic_id=clinic.clinic_id,
        patient_id=patient.id,
        provider_id=provider.id,
        appointment_type_id=appointment_type.id,
        starts_at=datetime(2030, 5, 7, 9, 0),
        ends_at=datetime(2030, 5, 7, 10, 0),
        duration_minutes=60,
        status=AppointmentStatus.BOOKED.value,
    )
    proposal = BookingProposal(
        clinic_id=clinic.clinic_id,
        public_id=f"prop_{label.lower()}",
        conversation_id=conversation.id,
        status=ProposalStatus.PENDING.value,
        reset_generation=conversation.reset_generation,
    )
    session.add_all([visit, proposal])
    session.commit()
    return {
        "staff_token": issued.token,
        "alert_id": alert.id,
        "proposal_id": proposal.public_id,
    }


@pytest.fixture()
def two_clinics(scenario, scenario_tenant_b, world) -> TwoClinics:
    """Both tenants, each populated, with a default clinic and a fresh snapshot.

    ``world`` is requested as well as ``scenario`` on purpose: pytest hands both
    the same instance, and the raw session token lives on it
    (``world.token``/``scenario_tenant_b.token``). ``Requester`` carries the
    hashed row, which cannot be presented as a credential.
    """
    alpha_session, requester = scenario
    beta = scenario_tenant_b

    # The header-less fallback resolves ``is_default``. Set it rather than relying
    # on "clinic 1 happens to be first", so the test pins the flag and not the
    # insertion order.
    alpha_row = tenancy.load_clinic_row(alpha_session, requester.clinic.clinic_id)
    assert alpha_row is not None
    alpha_row.is_default = True
    alpha_session.commit()

    alpha_facts = _populate(alpha_session, requester.clinic, label="Alpha")
    beta_facts = _populate(beta.session, beta.clinic, label="Beta")

    # Adding config rows does not touch ``clinics.updated_at``, so the snapshots
    # taken before them are stale. Invalidate, then rebuild both from the rows.
    tenancy.invalidate_cache()
    beta_row = tenancy.load_clinic_row(beta.session, beta.clinic.clinic_id)
    assert beta_row is not None
    alpha = tenancy.context_from(alpha_session, alpha_row)
    beta_context = tenancy.context_from(beta.session, beta_row)

    return TwoClinics(
        alpha_session=alpha_session,
        beta_session=beta.session,
        alpha=alpha,
        beta=beta_context,
        alpha_patient=requester.patient,
        beta_patient=beta.patient,
        alpha_token=world.token,
        beta_token=beta.token,
        alpha_staff_token=str(alpha_facts["staff_token"]),
        beta_staff_token=str(beta_facts["staff_token"]),
        alpha_alert_id=int(alpha_facts["alert_id"]),
        beta_alert_id=int(beta_facts["alert_id"]),
        alpha_proposal_id=str(alpha_facts["proposal_id"]),
        beta_proposal_id=str(beta_facts["proposal_id"]),
    )


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def _resolve(world: World, header: str | None = None) -> ClinicContext:
    """Run the request dependency on its own session, as a request would.

    ``get_clinic_context`` binds the session it is given, and a session may not be
    rebound to a second clinic, so each resolution gets a fresh one -- exactly the
    lifetime a request has.
    """
    db = world.factory()
    try:
        return get_clinic_context(db, header)
    finally:
        db.close()


def test_the_header_accepts_a_slug_and_a_numeric_id(world):
    """Both forms name the same tenant, so bookmarks and debugging URLs work."""
    by_slug = _resolve(world, "test-clinic")
    by_id = _resolve(world, str(world.clinic.clinic_id))
    assert by_slug.clinic_id == by_id.clinic_id == world.clinic.clinic_id
    assert by_slug.slug == "test-clinic"


def test_a_missing_header_resolves_the_default_clinic(two_clinics, world):
    """Today's client sends no header, and must keep getting today's clinic."""
    assert _resolve(world, None).clinic_id == two_clinics.alpha.clinic_id


def test_an_unknown_header_is_a_404_not_a_fallback(world):
    """A typo must not quietly show another practice's address and hours."""
    with pytest.raises(HTTPException) as caught:
        _resolve(world, "dental-that-does-not-exist")
    assert caught.value.status_code == 404


def test_an_inactive_clinic_is_a_503(two_clinics, world):
    """A closed tenant fails loudly instead of resolving to somebody else."""
    row = tenancy.load_clinic_row(two_clinics.beta_session, two_clinics.beta.clinic_id)
    assert row is not None
    row.active = False
    two_clinics.beta_session.commit()
    try:
        with pytest.raises(HTTPException) as caught:
            _resolve(world, two_clinics.beta.slug)
        assert caught.value.status_code == 503
    finally:
        row.active = True
        two_clinics.beta_session.commit()


def test_the_directory_lists_every_active_tenant(two_clinics, world):
    """``GET /api/clinics`` is how a client discovers the slugs to send."""
    db = world.factory()
    try:
        directory = list_clinics(db)
    finally:
        db.close()
    assert {entry.slug for entry in directory} == {two_clinics.alpha.slug, two_clinics.beta.slug}
    defaults = [entry.slug for entry in directory if entry.is_default]
    assert defaults == [two_clinics.alpha.slug]


# --------------------------------------------------------------------------- #
# Credentials outrank the header
# --------------------------------------------------------------------------- #


class _ReadOnlyRequest:
    """The staff dependency reads ``.method`` to decide whether CSRF applies."""

    method = "GET"


def test_a_patient_session_is_refused_under_another_clinics_header(two_clinics, world):
    """The token names the clinic; the header does not get to overrule it."""
    db = world.factory()
    try:
        with pytest.raises(HTTPException) as caught:
            get_patient_session(
                db, two_clinics.beta, x_patient_session=two_clinics.alpha_token
            )
        assert caught.value.status_code == 403
        assert caught.value.detail == PATIENT_CLINIC_MISMATCH_DETAIL
        # ... and its own clinic still works.
        record = get_patient_session(
            db, two_clinics.beta, x_patient_session=two_clinics.beta_token
        )
        assert record.clinic_id == two_clinics.beta.clinic_id
    finally:
        db.close()


def test_a_staff_session_is_refused_under_another_clinics_header(two_clinics, world):
    """A cookie from clinic A is the wrong dashboard at clinic B, not a 401."""
    db = world.factory()
    try:
        with pytest.raises(HTTPException) as caught:
            get_staff_session(
                db,
                _ReadOnlyRequest(),
                two_clinics.beta,
                mb_staff_session=two_clinics.alpha_staff_token,
            )
        assert caught.value.status_code == 403
        assert caught.value.detail == CLINIC_MISMATCH_DETAIL
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The ORM net
# --------------------------------------------------------------------------- #


def test_a_select_with_no_where_still_returns_one_clinic(two_clinics):
    """The point of the loader criteria: filtering that nobody has to remember.

    Clinic A is queried *first* on purpose. The value has to reach SQLAlchemy as a
    closure rather than a default argument, or the first statement compiled pins
    every later read to the first tenant and clinic B's rows read as missing.
    """
    alpha_ids = {row.id for row in two_clinics.alpha_session.scalars(select(Patient))}
    beta_ids = {row.id for row in two_clinics.beta_session.scalars(select(Patient))}

    assert alpha_ids == {two_clinics.alpha_patient.id}
    assert beta_ids == {two_clinics.beta_patient.id}
    assert alpha_ids.isdisjoint(beta_ids)


def test_the_opt_out_flag_is_what_shows_every_tenant(two_clinics):
    """Unscoped reads exist, but only where the caller asked for them explicitly."""
    every = {
        row.clinic_id
        for row in two_clinics.beta_session.scalars(
            select(Patient).execution_options(tenant_scoped=False)
        )
    }
    assert every == {two_clinics.alpha.clinic_id, two_clinics.beta.clinic_id}


def test_a_slot_from_another_clinic_does_not_resolve(two_clinics):
    """``stable_id`` is globally unique, so ids are the shortest path across.

    The lookup has to be tenant-filtered for the foreign key to mean anything: a
    provider id is global, and a slot id from clinic A must read as absent rather
    than as a row this clinic could hold.
    """
    alpha_slot_id = two_clinics.alpha_session.scalar(select(AvailabilitySlot.id).limit(1))
    assert alpha_slot_id is not None
    assert two_clinics.beta_session.get(AvailabilitySlot, alpha_slot_id) is None
    assert (
        two_clinics.beta_session.scalar(
            select(AvailabilitySlot).where(AvailabilitySlot.id == alpha_slot_id)
        )
        is None
    )


def test_appointments_do_not_leak_between_clinics(two_clinics):
    """``clinic_id`` is denormalised onto ``appointments`` for exactly this read."""
    alpha_ids = {row.id for row in two_clinics.alpha_session.scalars(select(Appointment))}
    beta_ids = {row.id for row in two_clinics.beta_session.scalars(select(Appointment))}
    assert len(alpha_ids) == 1
    assert beta_ids and alpha_ids.isdisjoint(beta_ids)


def test_a_proposal_from_another_clinic_is_not_found(two_clinics):
    """``find_by_public_id`` has no clinic argument; the net is the only guard."""
    assert (
        find_by_public_id(two_clinics.beta_session, two_clinics.alpha_proposal_id) is None
    )
    found = find_by_public_id(two_clinics.alpha_session, two_clinics.alpha_proposal_id)
    assert found is not None
    assert found.clinic_id == two_clinics.alpha.clinic_id


def test_the_staff_board_shows_only_its_own_clinic(two_clinics):
    """Alerts and pending requests are the two places a cross-tenant read would
    be operationally dangerous: it would page the wrong front desk."""
    alpha_alerts = board.list_alerts(two_clinics.alpha_session, board.AlertFilters())
    beta_alerts = board.list_alerts(two_clinics.beta_session, board.AlertFilters())
    assert {row.id for row in alpha_alerts} == {two_clinics.alpha_alert_id}
    assert {row.id for row in beta_alerts} == {two_clinics.beta_alert_id}

    alpha_requests = board.list_requests(two_clinics.alpha_session, board.RequestFilters())
    beta_requests = board.list_requests(two_clinics.beta_session, board.RequestFilters())
    assert len(alpha_requests) == 1
    assert len(beta_requests) == 1
    assert alpha_requests[0].id != beta_requests[0].id


def test_audit_rows_belong_to_the_transaction_that_wrote_them(two_clinics):
    """``audit.record`` takes the tenant from the session, never from a caller."""
    entry = audit.record(
        two_clinics.beta_session,
        action="tenancy.test",
        entity_type="patient",
        entity_id=two_clinics.beta_patient.id,
    )
    two_clinics.beta_session.commit()

    assert two_clinics.alpha_session.scalar(
        select(AuditRecord).where(AuditRecord.id == entry.id)
    ) is None
    assert entry.clinic_id == two_clinics.beta.clinic_id


def test_idempotency_keys_are_namespaced_by_clinic(two_clinics):
    """One dashboard driving two clinics may send the same key to both."""
    now = utcnow()
    for session, clinic in (
        (two_clinics.alpha_session, two_clinics.alpha),
        (two_clinics.beta_session, two_clinics.beta),
    ):
        session.add(
            IdempotencyRecord(
                clinic_id=clinic.clinic_id,
                key="retry-1",
                scope="book",
                reset_generation="generation",
                created_at=now,
            )
        )
        # The composite unique is what makes this succeed twice instead of raising
        # the way a global ``(scope, key)`` unique would.
        session.commit()

    beta_rows = list(two_clinics.beta_session.scalars(select(IdempotencyRecord)))
    assert [row.clinic_id for row in beta_rows] == [two_clinics.beta.clinic_id]


# --------------------------------------------------------------------------- #
# The insert guard
# --------------------------------------------------------------------------- #


def test_writing_a_row_for_another_clinic_raises(two_clinics):
    """A forgotten filter on a write is the failure that leaks data permanently."""
    stray = Patient(
        clinic_id=two_clinics.alpha.clinic_id,
        full_name="Wrong Clinic",
        phone="+16505550111",
        date_of_birth=date(1990, 1, 1),
    )
    two_clinics.beta_session.add(stray)
    with pytest.raises(TenantMismatch):
        two_clinics.beta_session.flush()
    two_clinics.beta_session.rollback()


def test_writing_a_row_with_no_clinic_raises(two_clinics):
    """An unset tenant is a bug, not a default."""
    stray = Patient(full_name="No Clinic", phone="+16505550112", date_of_birth=date(1990, 1, 1))
    two_clinics.beta_session.add(stray)
    with pytest.raises(TenantMismatch):
        two_clinics.beta_session.flush()
    two_clinics.beta_session.rollback()


def test_deleting_a_row_for_another_clinic_raises(two_clinics):
    """A delete leaks as permanently as a write: the row is gone either way.

    ``before_flush`` used to inspect only inserts and updates. The patient is
    loaded through alpha's session, then handed to beta's session to delete, so
    the row genuinely belongs to another tenant by the time the flush runs.
    """
    alpha_patient = two_clinics.alpha_patient
    # Detach from alpha's identity map, then hand the same row to beta: an ORM
    # object can only belong to one session, and the delete is what must be
    # rejected, not the reassignment.
    two_clinics.alpha_session.expunge(alpha_patient)
    two_clinics.beta_session.delete(alpha_patient)
    with pytest.raises(TenantMismatch):
        two_clinics.beta_session.flush()
    two_clinics.beta_session.rollback()


def test_deleting_its_own_row_is_allowed(two_clinics):
    """The guard must not fire on the ordinary case it has to permit.

    Without this, a stricter check could pass its own cross-tenant test while
    breaking every legitimate delete in the app. A throwaway row is used rather
    than the fixture's patient: deleting that would cascade into the visit that
    points at it, which is unrelated to what this asserts.
    """
    stray = Patient(
        clinic_id=two_clinics.beta.clinic_id,
        full_name="Own Clinic",
        phone="+16505550113",
        date_of_birth=date(1990, 1, 1),
    )
    two_clinics.beta_session.add(stray)
    two_clinics.beta_session.commit()
    patient_id = stray.id

    two_clinics.beta_session.delete(stray)
    two_clinics.beta_session.commit()
    assert two_clinics.beta_session.get(Patient, patient_id) is None


# --------------------------------------------------------------------------- #
# Tenant-owned knowledge
# --------------------------------------------------------------------------- #


def test_a_phone_and_date_of_birth_lookup_never_crosses_clinics(two_clinics):
    """Identity lookup is the one answer a patient would notice going wrong."""
    inside = patients.verify_patient(
        two_clinics.alpha_session,
        two_clinics.alpha,
        phone=two_clinics.alpha_patient.phone,
        date_of_birth=two_clinics.alpha_patient.date_of_birth.isoformat(),
    )
    assert inside["verified"] is True

    outside = patients.verify_patient(
        two_clinics.beta_session,
        two_clinics.beta,
        phone=two_clinics.alpha_patient.phone,
        date_of_birth=two_clinics.alpha_patient.date_of_birth.isoformat(),
    )
    assert outside["verified"] is False
    assert outside["reason"] == "no_match"


def test_hours_and_policy_answers_come_from_the_tenant(two_clinics):
    """The same topic must answer differently, or the constant never left."""
    alpha_hours = practice.lookup_policy(two_clinics.alpha, "hours")
    beta_hours = practice.lookup_policy(two_clinics.beta, "hours")
    assert alpha_hours is not None and beta_hours is not None
    assert alpha_hours["answer"] != beta_hours["answer"]
    assert alpha_hours["clinic"] == "test-clinic"
    assert beta_hours["clinic"] == "test-clinic-b"
    # A topic the other clinic never configured is a miss, not a near match.
    assert practice.lookup_policy(two_clinics.beta, "cancellation") is None


def test_insurance_normalisation_uses_the_tenants_own_carriers(two_clinics):
    """Accepting A's carrier at B would promise coverage B does not have.

    The input is deliberately not the canonical spelling: at its own clinic it is
    recognised and stored under the practice's name, while at the other clinic
    the same words fall through to "what the patient said". Equal values on both
    sides would mean the accepted list was never consulted.
    """
    alpha_plan = next(plan.name for plan in two_clinics.alpha.insurance_plans if plan.accepted)
    # Lower case, and phrased the way a patient would: not the stored spelling.
    spoken = alpha_plan.lower()

    at_home = normalize_insurance(spoken, accepted_plans=two_clinics.alpha.accepted_plan_names)
    elsewhere = normalize_insurance(
        spoken, accepted_plans=two_clinics.beta.accepted_plan_names
    )

    assert at_home.ok and at_home.value == alpha_plan
    assert elsewhere.ok and elsewhere.value == spoken
    assert elsewhere.value != alpha_plan
    # Each clinic still recognises its own.
    beta_plan = next(plan.name for plan in two_clinics.beta.insurance_plans if plan.accepted)
    assert normalize_insurance(
        beta_plan.lower(), accepted_plans=two_clinics.beta.accepted_plan_names
    ).value == beta_plan


def test_the_clinic_payload_differs_per_tenant(two_clinics):
    """What ``/api/clinic`` renders has to be the row, not the template."""
    alpha_payload = get_clinic(two_clinics.alpha).model_dump()
    beta_payload = get_clinic(two_clinics.beta).model_dump()

    assert alpha_payload["slot_minutes"] == 30
    assert beta_payload["slot_minutes"] == 20
    assert alpha_payload["business_hours"]["Mon"]["open"] == "08:00"
    assert beta_payload["business_hours"]["Mon"]["open"] is None
    assert beta_payload["business_hours"]["Tue"]["open"] == "09:00"
    assert beta_payload["timezone"] == "America/New_York"


def test_the_second_clinic_keeps_its_own_grid_and_closed_days(two_clinics):
    """A 20-minute Tue-Sat clinic must not be handed a 30-minute Pacific Monday."""
    alpha_offsets = [
        start.minute for start in two_clinics.alpha.config.day_slot_starts(date(2030, 5, 6))
    ]
    beta_offsets = [
        start.minute for start in two_clinics.beta.config.day_slot_starts(date(2030, 5, 6))
    ]
    assert alpha_offsets[:3] == [0, 30, 0]
    assert beta_offsets[:3] == [0, 20, 40]

    today = two_clinics.beta.config.today()
    next_monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    assert next_monday.weekday() == 0
    result = find_availability(
        two_clinics.beta_session,
        two_clinics.beta,
        start_date=next_monday.isoformat(),
        days=1,
        appointment_type="cleaning",
    )
    # Closed, so no slots -- not "the default grid's Monday", and not a fallback.
    assert result["available"] is False


def test_the_reset_generation_is_per_clinic(two_clinics):
    """One clinic resetting must not invalidate the other clinic's browser state."""
    before_alpha = generations.ensure_generation(two_clinics.alpha_session)
    before_beta = generations.ensure_generation(two_clinics.beta_session)
    assert before_alpha != before_beta

    rotated = generations.rotate_generation(two_clinics.alpha_session)
    two_clinics.alpha_session.commit()

    assert rotated != before_alpha
    assert generations.get_generation(two_clinics.alpha_session) == rotated
    assert generations.get_generation(two_clinics.beta_session) == before_beta


def test_a_patient_with_a_visit_cannot_be_deleted(two_clinics):
    """The schema refuses this deletion, and the ORM must refuse it the same way.

    ``appointments.patient_id`` is ``NOT NULL`` with ``ondelete="RESTRICT"``, so
    the database's answer is "no". Before ``passive_deletes`` was set on
    ``Patient.appointments`` the ORM instead tried to null the FK and died on
    ``NOT NULL constraint failed`` -- an IntegrityError crash rather than the
    intended refusal. Either way the row must survive, which is the part worth
    pinning: a rollback that silently ate the patient would be far worse than
    the crash.
    """
    from sqlalchemy.exc import IntegrityError

    beta = two_clinics.beta_session
    patient = two_clinics.beta_patient
    patient_id = patient.id
    assert beta.scalar(select(Appointment).where(Appointment.patient_id == patient_id))

    beta.delete(patient)
    with pytest.raises(IntegrityError) as refusal:
        beta.flush()
    # Assert the cause, not just the class. Both the broken and fixed paths
    # raise IntegrityError, and only the message tells them apart: nulling the
    # FK trips NOT NULL, whereas honouring RESTRICT trips the foreign key. A
    # test that only checked the type passed against the bug this fixes.
    message = str(refusal.value.orig)
    assert "FOREIGN KEY constraint failed" in message, message
    assert "NOT NULL" not in message, message
    beta.rollback()

    still_there = beta.get(Patient, patient_id)
    assert still_there is not None
    assert beta.scalar(select(Appointment).where(Appointment.patient_id == patient_id))
