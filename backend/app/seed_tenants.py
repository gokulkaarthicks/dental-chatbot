"""The demo tenants: one row plan per clinic, plus its fixtures.

Seeding used to mean "insert the one clinic's constants and its fixture tables".
With tenancy it means "make this clinic exist, then give it *its own* world":
its own providers, charts, staff login, transcripts, holds, and a slot grid
measured on its own slot length and opening days.

Two plans ship here.

``oral-b-dental-care`` (tenant 1) carries the values the single-clinic build
used, so the existing demo, README, tests and screenshots keep working. Its
fixtures are the ones in :mod:`app.seed_fixtures`.

``bayview-dental-studio`` (tenant 2) is deliberately different in kind rather
than in text: a 20-minute grid, ``America/New_York``, Tuesday-Saturday hours,
two providers, and different carriers. If tenant 2 were only a rename, a leaked
booking or a wrong-timezone answer would still *look* right; with a different
grid and calendar, a leak is visible as a time that does not exist there.

This module is seed-time only. Runtime code must never import it -- the tenant's
rows, read through :class:`app.tenancy.ClinicContext`, are the only source of
clinic facts at request time. That boundary is enforced by the
``flake8-tidy-imports`` banned-api rules in ``pyproject.toml``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.clinic import ClinicConfig, InsurancePlan, PracticePolicy
from app.seed_fixtures import (
    ALERTS,
    APPOINTMENT_TYPES,
    CONVERSATIONS,
    FAMILIES,
    GREETING_TEMPLATE,
    PAST_APPOINTMENTS,
    PATIENTS,
    PENDING_REQUESTS,
    PROPOSALS,
    PROVIDERS,
    STAFF,
    UPCOMING_APPOINTMENTS,
    AlertFixture,
    AppointmentFixture,
    AppointmentTypeFixture,
    ConversationFixture,
    FamilyFixture,
    PatientFixture,
    PendingRequestFixture,
    ProposalFixture,
    ProposalItemFixture,
    ProviderFixture,
    SeedPlan,
    StaffFixture,
)
from app.seed_templates import (
    BAYVIEW_CLINIC,
    BAYVIEW_INSURANCE_NOTE,
    BAYVIEW_INSURANCE_PLANS,
    BAYVIEW_PRACTICE_POLICIES,
    BAYVIEW_SCHEDULING,
    BAYVIEW_SELF_PAY_LABEL,
    BAYVIEW_SLUG,
    DEFAULT_ACCEPTED_INSURANCE_PLANS,
    DEFAULT_CLINIC,
    DEFAULT_INSURANCE_NOTE,
    DEFAULT_PRACTICE_POLICIES,
    DEFAULT_SELF_PAY_LABEL,
    DEFAULT_SLUG,
)
from app.timeutil import LEGACY_CLINIC_SCHEDULING, SchedulingConfig

# --------------------------------------------------------------------------- #
# Tenant 2 fixtures
#
# Every ``key`` here is written *without* its prefix; ``with_key_prefix`` applies
# ``bayview:`` below so the composite ``(clinic_id, seed_key)`` uniqueness is
# satisfied without duplicating the fixture text of tenant 1.
# --------------------------------------------------------------------------- #

BAYVIEW_PROVIDERS: tuple[ProviderFixture, ...] = (
    ProviderFixture(
        key="dr-raman",
        full_name="Dr. Priya Raman",
        credentials="DDS",
        email="priya.raman@bayviewstudio.example",
        specialties=("General Dentistry", "Cosmetic Dentistry"),
        bio="Leads the studio's general and cosmetic work; known for unhurried visits.",
        preferred_slot_index=3,
    ),
    ProviderFixture(
        key="dr-duarte",
        full_name="Dr. Elias Duarte",
        credentials="DMD",
        email="elias.duarte@bayviewstudio.example",
        specialties=("General Dentistry", "Emergency Care"),
        bio="Restorative cases and the clinic's same-day emergency openings.",
        preferred_slot_index=9,
    ),
)

BAYVIEW_FAMILIES: tuple[FamilyFixture, ...] = (
    FamilyFixture(
        key="family-arenas",
        name="Arenas Household",
        shared_phone="+1 (347) 555-0118",
        notes="Two adults; Saturday is the only day both can come in.",
    ),
)

BAYVIEW_PATIENTS: tuple[PatientFixture, ...] = (
    PatientFixture(
        key="camila-arenas",
        full_name="Camila Arenas",
        phone="+1 (347) 555-0118",
        dob="1988-01-22",
        insurance_provider="Empire Dental Choice",
        insurance_member_id="EDC-88-20451",
        family_group_key="family-arenas",
        relationship="spouse",
        is_primary_contact=True,
        notes="Prefers the first appointment of the day.",
    ),
    PatientFixture(
        key="jordan-arenas",
        full_name="Jordan Arenas",
        phone="+1 (347) 555-0118",
        dob="1986-09-14",
        insurance_provider="Empire Dental Choice",
        insurance_member_id="EDC-88-20452",
        family_group_key="family-arenas",
        relationship="spouse",
        is_primary_contact=False,
    ),
    PatientFixture(
        key="nadia-baptiste",
        full_name="Nadia Baptiste",
        phone="+1 (347) 555-0164",
        dob="1975-06-03",
        insurance_provider="Liberty Smile Care",
        insurance_member_id="LS-339120",
        family_group_key=None,
        relationship="self",
        is_primary_contact=True,
        notes="Needs a referral on file before a hygienist visit.",
    ),
    PatientFixture(
        key="owen-fitzgerald",
        full_name="Owen Fitzgerald",
        phone="+1 (917) 555-0129",
        dob="1998-11-30",
        insurance_provider=None,
        insurance_member_id=None,
        family_group_key=None,
        relationship="self",
        is_primary_contact=True,
    ),
    PatientFixture(
        key="solomon-vey",
        full_name="Solomon Vey",
        phone="+1 (917) 555-0177",
        dob="1961-02-17",
        insurance_provider="Empire Dental Choice",
        insurance_member_id="EDC-88-77310",
        family_group_key=None,
        relationship="self",
        is_primary_contact=True,
        notes="Implant maintenance; sees Dr. Duarte.",
    ),
)

# A 20-minute grid means the durations do not have to be multiples of 30. A
# 40-minute cleaning is two 20-minute intervals plus one, i.e. three slots --
# which is precisely the arithmetic a shared constant would have got wrong.
BAYVIEW_APPOINTMENT_TYPES: tuple[AppointmentTypeFixture, ...] = (
    AppointmentTypeFixture(
        code="cleaning",
        name="Hygiene Appointment",
        description="Scaling, polishing, and a hygienist check.",
        duration_minutes=40,
        required_specialty="General Dentistry",
    ),
    AppointmentTypeFixture(
        code="checkup",
        name="Routine Checkup",
        description="Short exam with X-rays when they are due.",
        duration_minutes=20,
        required_specialty="General Dentistry",
    ),
    AppointmentTypeFixture(
        code="emergency",
        name="Emergency Visit",
        description="Same-day attention for pain or a broken tooth.",
        duration_minutes=20,
        required_specialty="Emergency Care",
        is_emergency=True,
    ),
)

# Anchored on a Monday, so offsets 1..5 land Tuesday..Saturday -- the days this
# studio is actually open. Offset 0 (Monday) would seed nothing bookable.
BAYVIEW_UPCOMING: tuple[AppointmentFixture, ...] = (
    AppointmentFixture(
        key="appt-camila-cleaning",
        patient_key="camila-arenas",
        provider_key="dr-raman",
        type_code="cleaning",
        day_offset=1,
        slot_index=0,
        reason="Six-month hygiene visit.",
    ),
    AppointmentFixture(
        key="appt-jordan-checkup",
        patient_key="jordan-arenas",
        provider_key="dr-raman",
        type_code="checkup",
        day_offset=1,
        slot_index=3,
        reason="Annual exam.",
    ),
    AppointmentFixture(
        key="appt-nadia-cleaning",
        patient_key="nadia-baptiste",
        provider_key="dr-raman",
        type_code="cleaning",
        day_offset=3,
        slot_index=6,
        reason="Hygiene, referral on file.",
    ),
    AppointmentFixture(
        key="appt-owen-checkup",
        patient_key="owen-fitzgerald",
        provider_key="dr-duarte",
        type_code="checkup",
        day_offset=4,
        slot_index=12,
        reason="New-patient exam.",
    ),
    AppointmentFixture(
        key="appt-solomon-emergency",
        patient_key="solomon-vey",
        provider_key="dr-duarte",
        type_code="emergency",
        day_offset=5,
        slot_index=15,
        reason="Implant crown sensitivity.",
    ),
)

BAYVIEW_PAST: tuple[AppointmentFixture, ...] = (
    AppointmentFixture(
        key="appt-past-camila-checkup",
        patient_key="camila-arenas",
        provider_key="dr-raman",
        type_code="checkup",
        day_offset=-20,
        slot_index=4,
        reason="Exam before the hygiene plan.",
        status="completed",
    ),
    AppointmentFixture(
        key="appt-past-nadia-emergency",
        patient_key="nadia-baptiste",
        provider_key="dr-duarte",
        type_code="emergency",
        day_offset=-11,
        slot_index=10,
        reason="Cracked filling, lower left.",
        status="completed",
    ),
    AppointmentFixture(
        key="appt-past-solomon-cleaning",
        patient_key="solomon-vey",
        provider_key="dr-raman",
        type_code="cleaning",
        day_offset=-27,
        slot_index=8,
        reason="Implant maintenance.",
        status="completed",
    ),
)

BAYVIEW_STAFF: tuple[StaffFixture, ...] = (
    StaffFixture(
        key="staff-bayview",
        email="frontdesk@bayviewstudio.example",
        display_name="Bayview Front Desk",
        role="receptionist",
        username="bayview",
        demo_password="bayview",
    ),
)

BAYVIEW_ALERTS: tuple[AlertFixture, ...] = (
    AlertFixture(
        key="alert-referral-nadia",
        patient_key="nadia-baptiste",
        kind="insurance_verification",
        severity="normal",
        message="Liberty Smile Care referral has not arrived; hygienist visit is on hold.",
        status="open",
    ),
    AlertFixture(
        key="alert-owen-new-patient",
        patient_key="owen-fitzgerald",
        kind="assistance_request",
        severity="high",
        message="Self-pay new patient asked for a written estimate before the visit.",
        status="open",
    ),
)

BAYVIEW_PENDING_REQUESTS: tuple[PendingRequestFixture, ...] = (
    PendingRequestFixture(
        key="request-callback-solomon",
        patient_key="solomon-vey",
        request_type="callback",
        status="pending",
        details="Wants to discuss the implant crown before Friday.",
    ),
    PendingRequestFixture(
        key="request-reschedule-jordan",
        patient_key="jordan-arenas",
        request_type="reschedule",
        status="in_progress",
        details="Tuesday morning no longer works; asked for a later weekday.",
    ),
)

BAYVIEW_CONVERSATIONS: tuple[ConversationFixture, ...] = (
    ConversationFixture(
        key="conversation-arenas-saturday",
        caller_phone="+1 (347) 555-0118",
        patient_key="camila-arenas",
        status="active",
        day_offset=-2,
        messages=(
            ("assistant", GREETING_TEMPLATE, 0),
            ("patient", "Can you get my husband and me in on the same Saturday?", 1),
            ("assistant", "I can look for one Saturday that has a time for each of you.", 2),
        ),
    ),
    ConversationFixture(
        key="conversation-owen-selfpay",
        caller_phone="+1 (917) 555-0129",
        patient_key="owen-fitzgerald",
        status="closed",
        day_offset=-6,
        messages=(
            ("assistant", GREETING_TEMPLATE, 0),
            ("patient", "I don't have insurance. Is that a problem?", 1),
            ("assistant", "Not at all. Self-pay is fine and we quote the fee up front.", 2),
        ),
    ),
)

BAYVIEW_PROPOSALS: tuple[ProposalFixture, ...] = (
    ProposalFixture(
        key="proposal-arenas-saturday",
        conversation_key="conversation-arenas-saturday",
        status="pending",
        summary="Saturday hygiene for Camila and a checkup for Jordan.",
        items=(
            ProposalItemFixture("camila-arenas", "dr-raman", "cleaning", 5, 3),
            ProposalItemFixture("jordan-arenas", "dr-raman", "checkup", 5, 6),
        ),
    ),
)

BAYVIEW_FIXTURES = SeedPlan(
    providers=BAYVIEW_PROVIDERS,
    patients=BAYVIEW_PATIENTS,
    families=BAYVIEW_FAMILIES,
    appointment_types=BAYVIEW_APPOINTMENT_TYPES,
    upcoming=BAYVIEW_UPCOMING,
    past=BAYVIEW_PAST,
    staff=BAYVIEW_STAFF,
    alerts=BAYVIEW_ALERTS,
    pending_requests=BAYVIEW_PENDING_REQUESTS,
    conversations=BAYVIEW_CONVERSATIONS,
    proposals=BAYVIEW_PROPOSALS,
)


# --------------------------------------------------------------------------- #
# Key namespacing
# --------------------------------------------------------------------------- #

# Fixture keys are looked up across tables (a patient names its family group, an
# appointment names a patient and a provider), so a prefix is only safe if it is
# applied to every key *and* every reference to it. Listing the fields here, rather
# than prefixing whatever string happens to contain a colon, is what makes that
# true: a new fixture field holding a key has to be added to the table or the
# seed breaks visibly instead of silently pointing at another tenant's row.
_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "ProviderFixture": ("key",),
    "FamilyFixture": ("key",),
    "PatientFixture": ("key", "family_group_key"),
    "AppointmentFixture": ("key", "patient_key", "provider_key"),
    "StaffFixture": ("key",),
    "AlertFixture": ("key", "patient_key"),
    "PendingRequestFixture": ("key", "patient_key"),
    "ConversationFixture": ("key", "patient_key", "caller_phone"),
    "ProposalFixture": ("key", "conversation_key"),
    "ProposalItemFixture": ("patient_key", "provider_key"),
}


def _prefixed(value: str | None, prefix: str) -> str | None:
    if not prefix or value is None or value == "":
        # An empty prefix means "keep the literal keys": tenant 1's rows predate
        # tenancy and an upgraded database already holds them unprefixed.
        return value
    return f"{prefix}:{value}"


def _rekey(fixture: Any, prefix: str) -> Any:
    fields = _KEY_FIELDS.get(type(fixture).__name__)
    if fields is None:
        return fixture
    changes: dict[str, object] = {}
    for name in fields:
        if name == "caller_phone":
            # A phone is an identity lookup, not a key, so it must *not* be
            # namespaced: it has to stay a phone number the chart could hold.
            continue
        changes[name] = _prefixed(getattr(fixture, name), prefix)
    return replace(fixture, **changes)


def _rekey_proposal(fixture: Any, prefix: str) -> Any:
    """Namespace a proposal and the keys its line items point at.

    ``items`` is the one fixture field that carries keys inside a nested value,
    so ``replace`` on the proposal alone cannot reach them. The appointment
    *type* stays a bare code: codes are the tool's vocabulary, not fixture keys.
    """
    return replace(
        _rekey(fixture, prefix),
        items=tuple(_rekey(item, prefix) for item in fixture.items),
    )


def with_key_prefix(plan: SeedPlan, prefix: str, *, clinic_name: str) -> SeedPlan:
    """Namespace a fixture plan and fill in its clinic name.

    Appointment-type *codes* stay unprefixed on purpose: they are part of the
    tool contract (``appointment_type="cleaning"``), so both clinics answer to the
    same words while the ``(clinic_id, code)`` uniqueness keeps the rows apart.
    """
    return SeedPlan(
        providers=tuple(_rekey(item, prefix) for item in plan.providers),
        patients=tuple(_rekey(item, prefix) for item in plan.patients),
        families=tuple(_rekey(item, prefix) for item in plan.families),
        appointment_types=plan.appointment_types,
        upcoming=tuple(_rekey(item, prefix) for item in plan.upcoming),
        past=tuple(_rekey(item, prefix) for item in plan.past),
        staff=tuple(_rekey(item, prefix) for item in plan.staff),
        alerts=tuple(_rekey(item, prefix) for item in plan.alerts),
        pending_requests=tuple(_rekey(item, prefix) for item in plan.pending_requests),
        conversations=tuple(
            replace(
                _rekey(item, prefix),
                messages=tuple(
                    (role, content.replace("{clinic_name}", clinic_name), offset)
                    for role, content, offset in item.messages
                ),
            )
            for item in plan.conversations
        ),
        proposals=tuple(_rekey_proposal(item, prefix) for item in plan.proposals),
    )


# --------------------------------------------------------------------------- #
# Tenant plans
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TenantSeedPlan:
    """Everything needed to create one clinic row and give it a demo world."""

    slug: str
    name: str
    is_default: bool
    clinic: ClinicConfig
    scheduling: SchedulingConfig
    self_pay_label: str
    insurance_note: str
    insurance_plans: tuple[InsurancePlan, ...]
    policies: tuple[PracticePolicy, ...]
    fixtures: SeedPlan


# Tenant 1 keeps the *unprefixed* fixture keys on purpose. It is the clinic the
# pre-tenancy demo wrote, so an upgraded database already holds ``staff-root``,
# ``dr-bell`` and friends; naming them ``oralb:staff-root`` here would make
# ``make seed`` insert a second copy of every fixture and fail on the unique
# username. The composite ``(clinic_id, seed_key)`` uniqueness is what keeps
# both tenants' keys apart -- they never share a namespace row.
ORAL_B_PLAN = TenantSeedPlan(
    slug=DEFAULT_SLUG,
    name=DEFAULT_CLINIC.name,
    is_default=True,
    clinic=DEFAULT_CLINIC,
    scheduling=LEGACY_CLINIC_SCHEDULING,
    self_pay_label=DEFAULT_SELF_PAY_LABEL,
    insurance_note=DEFAULT_INSURANCE_NOTE,
    insurance_plans=DEFAULT_ACCEPTED_INSURANCE_PLANS,
    policies=DEFAULT_PRACTICE_POLICIES,
    fixtures=with_key_prefix(
        SeedPlan(
            providers=PROVIDERS,
            patients=PATIENTS,
            families=FAMILIES,
            appointment_types=APPOINTMENT_TYPES,
            upcoming=UPCOMING_APPOINTMENTS,
            past=PAST_APPOINTMENTS,
            staff=STAFF,
            alerts=ALERTS,
            pending_requests=PENDING_REQUESTS,
            conversations=CONVERSATIONS,
            proposals=PROPOSALS,
        ),
        "",
        clinic_name=DEFAULT_CLINIC.name,
    ),
)

BAYVIEW_PLAN = TenantSeedPlan(
    slug=BAYVIEW_SLUG,
    name=BAYVIEW_CLINIC.name,
    is_default=False,
    clinic=BAYVIEW_CLINIC,
    scheduling=BAYVIEW_SCHEDULING,
    self_pay_label=BAYVIEW_SELF_PAY_LABEL,
    insurance_note=BAYVIEW_INSURANCE_NOTE,
    insurance_plans=BAYVIEW_INSURANCE_PLANS,
    policies=BAYVIEW_PRACTICE_POLICIES,
    # The literal keys in ``BAYVIEW_*`` fixtures are written unprefixed, so the
    # ``bayview`` prefix has to be applied here or tenant 1's ``oralb:`` rows and
    # tenant 2's would collide on nothing at all.
    fixtures=with_key_prefix(
        BAYVIEW_FIXTURES, "bayview", clinic_name=BAYVIEW_CLINIC.name
    ),
)

TENANT_PLANS: tuple[TenantSeedPlan, ...] = (ORAL_B_PLAN, BAYVIEW_PLAN)


def plan_for_slug(slug: str) -> TenantSeedPlan | None:
    cleaned = (slug or "").strip().lower()
    for plan in TENANT_PLANS:
        if plan.slug == cleaned:
            return plan
    return None


def plans_for_slugs(slugs: list[str] | None) -> tuple[TenantSeedPlan, ...]:
    """The named tenants, or every shipped plan when nothing was named."""
    if not slugs:
        return TENANT_PLANS
    selected: list[TenantSeedPlan] = []
    for slug in slugs:
        plan = plan_for_slug(slug)
        if plan is not None:
            selected.append(plan)
    return tuple(selected) or TENANT_PLANS


__all__ = [
    "BAYVIEW_APPOINTMENT_TYPES",
    "BAYVIEW_FIXTURES",
    "BAYVIEW_PLAN",
    "ORAL_B_PLAN",
    "TENANT_PLANS",
    "TenantSeedPlan",
    "plan_for_slug",
    "plans_for_slugs",
    "with_key_prefix",
]
