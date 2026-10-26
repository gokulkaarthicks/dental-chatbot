"""Reproducible demo fixtures.

All dates are generated relative to the clinic-local date at seed time, so the
demo always has past and upcoming appointments no matter when it is opened.
Within a day, slots are assigned by a fixed index so repeated seeds produce the
same layout for the same anchor date.

Fixture counts (per seed):
  * 3 providers, 8 patients (2 family groups), 3 appointment types
  * 60 days of 30-minute availability per provider (Mon-Sat, 08:00-18:00)
  * 6 confirmed upcoming appointments + 3 past completed visits
  * staff users, queued alerts/requests, pending proposals, audit + idempotency rows
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.timeutil import SchedulingConfig

FIXTURE_SET = "clinic-demo"
FIXTURE_VERSION = 1

# The opening line stored in the demo transcripts. Kept as a template rather than
# an f-string because the clinic name is a *tenant* fact: substituting one
# practice's name into another's history is exactly the leak the tenancy work is
# meant to make impossible. ``app.seed_tenants`` fills it in per clinic.
GREETING_TEMPLATE = "Thanks for calling {clinic_name}. How can I help?"


@dataclass(frozen=True)
class ProviderFixture:
    key: str
    full_name: str
    credentials: str
    email: str
    specialties: tuple[str, ...]
    bio: str
    # 0..19 index into the 08:00-18:00 grid; used for fixture appointments.
    preferred_slot_index: int


@dataclass(frozen=True)
class PatientFixture:
    key: str
    full_name: str
    phone: str
    dob: str
    insurance_provider: str | None
    insurance_member_id: str | None
    family_group_key: str | None
    relationship: str
    is_primary_contact: bool
    notes: str | None = None


@dataclass(frozen=True)
class FamilyFixture:
    key: str
    name: str
    shared_phone: str
    notes: str


@dataclass(frozen=True)
class AppointmentTypeFixture:
    code: str
    name: str
    description: str
    duration_minutes: int
    required_specialty: str | None
    is_emergency: bool = False


@dataclass(frozen=True)
class AppointmentFixture:
    key: str
    patient_key: str
    provider_key: str
    type_code: str
    day_offset: int  # relative to the clinic-local seed anchor date
    slot_index: int
    reason: str
    status: str = "booked"  # "booked" (upcoming) or "completed" (past)


@dataclass(frozen=True)
class StaffFixture:
    key: str
    email: str
    display_name: str
    role: str
    # Phase 4: dashboard login. Optional so contact-only accounts can exist.
    username: str | None = None
    demo_password: str = ""


@dataclass(frozen=True)
class AlertFixture:
    key: str
    patient_key: str | None
    kind: str
    severity: str
    message: str
    status: str


@dataclass(frozen=True)
class PendingRequestFixture:
    key: str
    patient_key: str
    request_type: str
    status: str
    details: str


@dataclass(frozen=True)
class ConversationFixture:
    key: str
    caller_phone: str
    patient_key: str
    status: str
    day_offset: int
    messages: tuple[tuple[str, str, int], ...]  # (role, content, minute_offset)


@dataclass(frozen=True)
class ProposalItemFixture:
    """One line of a proposal, naming the rows it points at.

    A dataclass rather than a tuple because the seed namespaces fixture keys per
    tenant: the keys have to be addressable fields so the prefixing table can
    rewrite them along with every other reference.
    """

    patient_key: str
    provider_key: str
    type_code: str
    day_offset: int
    slot_index: int


@dataclass(frozen=True)
class ProposalFixture:
    key: str
    conversation_key: str
    status: str
    summary: str
    items: tuple[ProposalItemFixture, ...]


PROVIDERS: tuple[ProviderFixture, ...] = (
    ProviderFixture(
        key="dr-bell",
        full_name="Dr. Naomi Bell",
        credentials="DDS",
        email="naomi.bell@oralbdental.example",
        specialties=("General Dentistry", "Family Dentistry"),
        bio="Founding dentist. Gentle with anxious patients and children.",
        preferred_slot_index=2,
    ),
    ProviderFixture(
        key="dr-osei",
        full_name="Dr. Kwame Osei",
        credentials="DDS, MS",
        email="kwame.osei@oralbdental.example",
        specialties=("Endodontics", "Emergency Care"),
        bio="Root canals and same-day dental emergencies.",
        preferred_slot_index=6,
    ),
    ProviderFixture(
        key="dr-varga",
        full_name="Dr. Ilona Varga",
        credentials="DMD",
        email="ilona.varga@oralbdental.example",
        specialties=("Pediatric Dentistry", "Preventive Care"),
        bio="Pediatric specialist; sees most of the clinic's younger patients.",
        preferred_slot_index=10,
    ),
)

FAMILIES: tuple[FamilyFixture, ...] = (
    FamilyFixture(
        key="family-whitfield",
        name="Whitfield Household",
        shared_phone="+1 (650) 555-0101",
        notes="Two children; prefers Saturday morning appointments.",
    ),
    FamilyFixture(
        key="family-castellanos",
        name="Castellanos Household",
        shared_phone="+1 (650) 555-0184",
        notes="Shared for insurance paperwork; calls usually come from Ana.",
    ),
)

PATIENTS: tuple[PatientFixture, ...] = (
    PatientFixture(
        key="marcus-whitfield",
        full_name="Marcus Whitfield",
        phone="+1 (650) 555-0101",
        dob="1984-03-12",
        insurance_provider="Sunnycoast Dental PPO",
        insurance_member_id="SC-4471902",
        family_group_key="family-whitfield",
        relationship="spouse",
        is_primary_contact=True,
        notes="Prefers morning appointments.",
    ),
    PatientFixture(
        key="priya-whitfield",
        full_name="Priya Whitfield",
        phone="+1 (650) 555-0101",
        dob="1986-07-30",
        insurance_provider="Sunnycoast Dental PPO",
        insurance_member_id="SC-4471903",
        family_group_key="family-whitfield",
        relationship="spouse",
        is_primary_contact=False,
    ),
    PatientFixture(
        key="ellie-whitfield",
        full_name="Ellie Whitfield",
        phone="+1 (650) 555-0101",
        dob="2016-11-05",
        insurance_provider="Sunnycoast Dental PPO",
        insurance_member_id="SC-4471904",
        family_group_key="family-whitfield",
        relationship="child",
        is_primary_contact=False,
        notes="Sensitive gag reflex; bring the flavored fluoride.",
    ),
    PatientFixture(
        key="noah-whitfield",
        full_name="Noah Whitfield",
        phone="+1 (650) 555-0101",
        dob="2019-02-21",
        insurance_provider="Sunnycoast Dental PPO",
        insurance_member_id="SC-4471905",
        family_group_key="family-whitfield",
        relationship="child",
        is_primary_contact=False,
    ),
    PatientFixture(
        key="ana-castellanos",
        full_name="Ana Castellanos",
        phone="+1 (650) 555-0184",
        dob="1979-09-18",
        insurance_provider="Blue Harbor Dental",
        insurance_member_id="BH-8820311",
        family_group_key="family-castellanos",
        relationship="self",
        is_primary_contact=True,
    ),
    PatientFixture(
        key="diego-castellanos",
        full_name="Diego Castellanos",
        phone="+1 (650) 555-0184",
        dob="2010-05-02",
        insurance_provider="Blue Harbor Dental",
        insurance_member_id="BH-8820312",
        family_group_key="family-castellanos",
        relationship="child",
        is_primary_contact=False,
    ),
    PatientFixture(
        key="ruth-okonkwo",
        full_name="Ruth Okonkwo",
        phone="+1 (415) 555-0166",
        dob="1957-01-09",
        insurance_provider="MediCal Dental",
        insurance_member_id="MC-3390147",
        family_group_key=None,
        relationship="",
        is_primary_contact=False,
        notes="Prefers a paper copy of instructions.",
    ),
    PatientFixture(
        key="theo-lindqvist",
        full_name="Theo Lindqvist",
        phone="+1 (650) 555-0137",
        dob="1994-12-01",
        insurance_provider=None,
        insurance_member_id=None,
        family_group_key=None,
        relationship="",
        is_primary_contact=False,
        notes="Self-pay; asks for cost estimates before treatment.",
    ),
)

APPOINTMENT_TYPES: tuple[AppointmentTypeFixture, ...] = (
    AppointmentTypeFixture(
        code="cleaning",
        name="Dental Cleaning",
        description="Routine hygiene visit with polishing and fluoride.",
        duration_minutes=60,
        required_specialty="General Dentistry",
    ),
    AppointmentTypeFixture(
        code="checkup",
        name="General Checkup",
        description="Exam, X-rays if needed, and treatment planning.",
        duration_minutes=30,
        required_specialty="General Dentistry",
    ),
    AppointmentTypeFixture(
        code="emergency",
        name="Emergency Visit",
        description="Same-day relief for pain, trauma, or a broken tooth.",
        duration_minutes=30,
        required_specialty="Emergency Care",
        is_emergency=True,
    ),
)

# Upcoming, confirmed. Offsets land on open weekdays once anchored to a Monday.
UPCOMING_APPOINTMENTS: tuple[AppointmentFixture, ...] = (
    AppointmentFixture(
        key="appt-marcus-cleaning",
        patient_key="marcus-whitfield",
        provider_key="dr-bell",
        type_code="cleaning",
        day_offset=1,
        slot_index=2,
        reason="Six-month recall cleaning.",
    ),
    AppointmentFixture(
        key="appt-priya-checkup",
        patient_key="priya-whitfield",
        provider_key="dr-bell",
        type_code="checkup",
        day_offset=1,
        slot_index=6,
        reason="Sensitivity on upper left molars.",
    ),
    AppointmentFixture(
        key="appt-ellie-checkup",
        patient_key="ellie-whitfield",
        provider_key="dr-varga",
        type_code="checkup",
        day_offset=5,
        slot_index=4,
        reason="First visit after sealants.",
    ),
    AppointmentFixture(
        key="appt-ana-emergency",
        patient_key="ana-castellanos",
        provider_key="dr-osei",
        type_code="emergency",
        day_offset=2,
        slot_index=10,
        reason="Chipped front tooth; needs bonding.",
    ),
    AppointmentFixture(
        key="appt-diego-cleaning",
        patient_key="diego-castellanos",
        provider_key="dr-varga",
        type_code="cleaning",
        day_offset=8,
        slot_index=8,
        reason="Routine cleaning.",
    ),
    AppointmentFixture(
        key="appt-ruth-checkup",
        patient_key="ruth-okonkwo",
        provider_key="dr-osei",
        type_code="checkup",
        day_offset=11,
        slot_index=12,
        reason="Review medication-related dry mouth.",
    ),
)

# Past visits (status completed) so history views have content.
PAST_APPOINTMENTS: tuple[AppointmentFixture, ...] = (
    AppointmentFixture(
        key="appt-past-marcus-cleaning",
        patient_key="marcus-whitfield",
        provider_key="dr-bell",
        type_code="cleaning",
        day_offset=-21,
        slot_index=2,
        reason="Routine cleaning.",
        status="completed",
    ),
    AppointmentFixture(
        key="appt-past-theo-emergency",
        patient_key="theo-lindqvist",
        provider_key="dr-osei",
        type_code="emergency",
        day_offset=-9,
        slot_index=14,
        reason="Pain in lower right molar.",
        status="completed",
    ),
    AppointmentFixture(
        key="appt-past-noah-checkup",
        patient_key="noah-whitfield",
        provider_key="dr-varga",
        type_code="checkup",
        day_offset=-30,
        slot_index=4,
        reason="Growth check.",
        status="completed",
    ),
)

STAFF: tuple[StaffFixture, ...] = (
    # The documented demo login for the staff dashboard. Demonstration only:
    # the credentials are intentionally trivial and stated in the README.
    StaffFixture(
        key="staff-root",
        email="root@oralbdental.example",
        display_name="Demo Administrator",
        role="clinic_manager",
        username="root",
        demo_password="root",
    ),
)

ALERTS: tuple[AlertFixture, ...] = (
    AlertFixture(
        key="alert-walkin-pain",
        patient_key="theo-lindqvist",
        kind="urgent_callback",
        severity="high",
        message="Caller reported severe pain and may walk in this afternoon.",
        status="open",
    ),
    AlertFixture(
        key="alert-insurance-mismatch",
        patient_key="ana-castellanos",
        kind="insurance_verification",
        severity="normal",
        message="Blue Harbor plan number does not match the card on file.",
        status="acknowledged",
    ),
)

PENDING_REQUESTS: tuple[PendingRequestFixture, ...] = (
    PendingRequestFixture(
        key="request-callback-marcus",
        patient_key="marcus-whitfield",
        request_type="callback",
        status="pending",
        details="Would like a call about adding a first visit for Noah.",
    ),
    PendingRequestFixture(
        key="request-billing-ruth",
        patient_key="ruth-okonkwo",
        request_type="billing",
        status="in_progress",
        details="Questions about coverage for a second cleaning this year.",
    ),
)

CONVERSATIONS: tuple[ConversationFixture, ...] = (
    ConversationFixture(
        key="conversation-whitfield-family",
        caller_phone="+1 (650) 555-0101",
        patient_key="priya-whitfield",
        status="active",
        day_offset=-1,
        messages=(
            ("assistant", GREETING_TEMPLATE, 0),
            ("patient", "Hi, I'd like to book cleanings for the whole family.", 1),
            ("assistant", "Happy to help. Which names should I include?", 2),
        ),
    ),
    ConversationFixture(
        key="conversation-theo-pain",
        caller_phone="+1 (650) 555-0137",
        patient_key="theo-lindqvist",
        status="closed",
        day_offset=-9,
        messages=(
            ("assistant", GREETING_TEMPLATE, 0),
            ("patient", "I have a bad toothache on the bottom right.", 1),
            ("assistant", "Let's get you seen today. I have an opening at 3:00 PM.", 2),
        ),
    ),
)

PROPOSALS: tuple[ProposalFixture, ...] = (
    ProposalFixture(
        key="proposal-whitfield-family",
        conversation_key="conversation-whitfield-family",
        status="pending",
        summary="Back-to-back Saturday cleanings for Marcus, Priya, and Ellie.",
        items=(
            ProposalItemFixture("marcus-whitfield", "dr-bell", "cleaning", 5, 2),
            ProposalItemFixture("priya-whitfield", "dr-bell", "cleaning", 5, 4),
            ProposalItemFixture("ellie-whitfield", "dr-varga", "cleaning", 5, 6),
        ),
    ),
)


def anchor_date(config: SchedulingConfig, today: date | None = None) -> date:
    """Start of the next Monday at or after the clinic's own today.

    Anchoring the horizon on a Monday guarantees every ``day_offset`` used by
    the fixtures falls on the same weekday regardless of when the demo is seeded.
    ``config`` is required because "today" is a claim about a timezone: seeding
    tenant B on a server set to Pacific would otherwise anchor a day early.
    """
    current = today or config.today()
    return current + timedelta(days=(7 - current.weekday()) % 7)


def appointment_bounds(
    fixture: AppointmentFixture,
    anchor: date,
    config: SchedulingConfig,
    types: tuple[AppointmentTypeFixture, ...] | None = None,
) -> tuple[datetime, datetime]:
    """Absolute start/end for a fixture appointment given a seed anchor date.

    The ``slot_index`` is counted on *this* clinic's grid, so the same fixture
    index means 09:00 + 20 minutes at a 20-minute practice and 08:00 + 30 minutes
    at a 30-minute one.
    """
    durations = {
        item.code: item.duration_minutes for item in (types or APPOINTMENT_TYPES)
    }
    day = anchor + timedelta(days=fixture.day_offset)
    starts_at = datetime.combine(day, config.open_time) + config.slot_length * fixture.slot_index
    return starts_at, starts_at + timedelta(minutes=durations[fixture.type_code])


@dataclass(frozen=True)
class SeedSummary:
    providers_created: int = 0
    patients_created: int = 0
    families_created: int = 0
    appointment_types_created: int = 0
    slots_created: int = 0
    appointments_created: int = 0
    staff_created: int = 0
    alerts_created: int = 0
    pending_requests_created: int = 0
    conversations_created: int = 0
    messages_created: int = 0
    proposals_created: int = 0
    proposal_items_created: int = 0
    claim_conflicts: int = 0
    skipped_existing: int = 0
    anchor_date: date | None = None
    horizon_days: int = 0

    def as_dict(self) -> dict:
        return {
            "providers_created": self.providers_created,
            "patients_created": self.patients_created,
            "families_created": self.families_created,
            "appointment_types_created": self.appointment_types_created,
            "slots_created": self.slots_created,
            "appointments_created": self.appointments_created,
            "staff_created": self.staff_created,
            "alerts_created": self.alerts_created,
            "pending_requests_created": self.pending_requests_created,
            "conversations_created": self.conversations_created,
            "messages_created": self.messages_created,
            "proposals_created": self.proposals_created,
            "proposal_items_created": self.proposal_items_created,
            "claim_conflicts": self.claim_conflicts,
            "skipped_existing": self.skipped_existing,
            "anchor_date": self.anchor_date.isoformat() if self.anchor_date else None,
            "horizon_days": self.horizon_days,
        }


def fixture_counts(plan: SeedPlan | None = None) -> dict[str, int]:
    """Static counts of a tenant's fixture definitions, safe to expose publicly.

    With no plan, tenant 1's tables are counted -- which is what the single-clinic
    build meant. A second clinic has its own roster, so ``/api/demo/status`` passes
    the resolved tenant's plan instead.
    """
    table = plan if plan is not None else SEED_PLAN
    return {
        "providers": len(table.providers),
        "patients": len(table.patients),
        "family_groups": len(table.families),
        "appointment_types": len(table.appointment_types),
        "seeded_appointments": len(table.upcoming) + len(table.past),
        "staff_users": len(table.staff),
        "alerts": len(table.alerts),
        "pending_requests": len(table.pending_requests),
        "conversations": len(table.conversations),
        "proposals": len(table.proposals),
    }


@dataclass(frozen=True)
class SeedPlan:
    """Bundles the fixture tables. Constructed once via ``SEED_PLAN`` below.

    Fields intentionally have no defaults: a default of ``PROVIDERS`` in the
    class body is captured at class-creation time, when the module-level names
    do not exist yet.
    """

    providers: tuple[ProviderFixture, ...]
    patients: tuple[PatientFixture, ...]
    families: tuple[FamilyFixture, ...]
    appointment_types: tuple[AppointmentTypeFixture, ...]
    upcoming: tuple[AppointmentFixture, ...]
    past: tuple[AppointmentFixture, ...]
    staff: tuple[StaffFixture, ...]
    alerts: tuple[AlertFixture, ...]
    pending_requests: tuple[PendingRequestFixture, ...]
    conversations: tuple[ConversationFixture, ...]
    proposals: tuple[ProposalFixture, ...]


SEED_PLAN = SeedPlan(
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
)
