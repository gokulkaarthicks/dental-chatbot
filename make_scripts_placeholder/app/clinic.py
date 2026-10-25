"""Static, fictional clinic configuration.

Everything here is invented for demonstration. ``/api/clinic`` serves this
object so the frontend can render hours and contact details without hardcoding
them. Do not replace with real clinic data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.timeutil import (
    CLINIC_TIMEZONE_NAME,
    CLOSE_TIME,
    OPEN_TIME,
    OPEN_WEEKDAYS,
    SLOT_MINUTES,
)

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class InsurancePlan:
    name: str
    accepted: bool
    note: str


@dataclass(frozen=True)
class PracticePolicy:
    """A single answerable question, stored in the database at seed time.

    ``topic`` is the stable lookup key the agent passes to ``get_practice_info``.
    ``answer`` must be self-contained: the assistant relays it rather than
    paraphrasing freely, so it can never invent a price or a coverage promise.
    """

    topic: str
    question: str
    answer: str
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClinicConfig:
    # The one place the practice is named. The patient chat header, the staff
    # dashboard, the system prompt, and the seeded demo threads all read it from
    # here (via ``/api/clinic`` on the client), so the two interfaces cannot
    # disagree about who they work for.
    name: str
    legal_note: str
    address_line1: str
    address_line2: str
    city: str
    state: str
    postal_code: str
    phone: str
    email: str
    timezone: str
    slot_minutes: int
    business_hours: dict[str, dict[str, str | None]]
    closed_days: tuple[str, ...]
    parking_note: str
    timezone_note: str = ""
    fictional: bool = True
    extra: dict[str, str] = field(default_factory=dict)

    def as_public_dict(self) -> dict:
        return {
            "name": self.name,
            "legal_note": self.legal_note,
            "address": {
                "line1": self.address_line1,
                "line2": self.address_line2,
                "city": self.city,
                "state": self.state,
                "postal_code": self.postal_code,
            },
            "phone": self.phone,
            "email": self.email,
            "timezone": self.timezone,
            "timezone_note": self.timezone_note,
            "slot_minutes": self.slot_minutes,
            "business_hours": self.business_hours,
            "closed_days": list(self.closed_days),
            "parking_note": self.parking_note,
            "fictional": self.fictional,
        }


def _build_hours() -> dict[str, dict[str, str | None]]:
    open_label = OPEN_TIME.strftime("%H:%M")
    close_label = CLOSE_TIME.strftime("%H:%M")
    hours: dict[str, dict[str, str | None]] = {}
    for index, label in enumerate(WEEKDAY_LABELS):
        is_open = index in OPEN_WEEKDAYS
        hours[label] = {
            "open": open_label if is_open else None,
            "close": close_label if is_open else None,
        }
    return hours


CLINIC = ClinicConfig(
    name="Oral-B Dental Care",
    legal_note="Fictional clinic created for a product demonstration. Not a real practice.",
    address_line1="1717 Brannan Street, Suite 400",
    address_line2="Second floor, entrance on the courtyard side",
    city="San Francisco",
    state="CA",
    postal_code="94103",
    phone="+1 (415) 555-0182",
    email="hello@oralbdental.example",
    timezone=CLINIC_TIMEZONE_NAME,
    slot_minutes=SLOT_MINUTES,
    business_hours=_build_hours(),
    closed_days=("Sun",),
    parking_note="Free parking in the rear lot; visitor spaces are on level P1.",
    timezone_note="All times are Pacific Time.",
)

# Accepted plans. Everything else is answered as "check with the front desk".
ACCEPTED_INSURANCE_PLANS: tuple[InsurancePlan, ...] = (
    InsurancePlan(
        name="Sunnycoast Dental PPO",
        accepted=True,
        note="In-network PPO. Cleanings and checkups are covered per plan year.",
    ),
    InsurancePlan(
        name="Blue Harbor Dental",
        accepted=True,
        note="In-network PPO. Verify member ID at registration.",
    ),
    InsurancePlan(
        name="MediCal Dental",
        accepted=True,
        note="Accepted for children and adults; treatment limits apply per plan.",
    ),
)

SELF_PAY_LABEL = "Self-pay"

# Practice policies. These are the ONLY source for policy/pricing answers.
#
# Prices are deliberately expressed as ranges and prefixed so the assistant
# relays them verbatim instead of inventing a quote. Coverage language avoids
# promising any specific benefit amount.
PRACTICE_POLICIES: tuple[PracticePolicy, ...] = (
    PracticePolicy(
        topic="new_patient_exam",
        question="Do you accept new patients, and what does a first visit include?",
        answer=(
            "We are accepting new patients of all ages. A first visit is a comprehensive exam "
            "with X-rays and a cleaning when it is due. Plan for about 60 minutes."
        ),
        keywords=("new patient", "first visit", "exam"),
    ),
    PracticePolicy(
        topic="cleaning_cost",
        question="How much does a cleaning cost?",
        answer=(
            "Without insurance, a routine cleaning is quoted at $120-$160 depending on the "
            "hygienist's assessment. If you have insurance, your plan may cover part or all of "
            "it; the front desk verifies benefits before treatment."
        ),
        keywords=("cost", "price", "cleaning", "how much"),
    ),
    PracticePolicy(
        topic="exam_cost",
        question="How much does a general checkup cost?",
        answer=(
            "A general checkup with X-rays is quoted at $95-$140 without insurance. "
            "Insurance coverage varies by plan, so we confirm your benefits first."
        ),
        keywords=("checkup", "check up", "cost", "price", "exam price"),
    ),
    PracticePolicy(
        topic="emergency_visit",
        question="What about an emergency visit?",
        answer=(
            "We hold same-day emergency openings each business day for pain or a broken tooth. "
            "An emergency visit starts at $150 and further treatment is quoted before it begins. "
            "If this is a life-threatening emergency, call 911."
        ),
        keywords=("emergency", "pain", "toothache", "broken tooth", "urgent"),
    ),
    PracticePolicy(
        topic="insurance_accepted",
        question="Which dental insurance plans do you accept?",
        answer=(
            "We are in-network with Sunnycoast Dental PPO, Blue Harbor Dental, and MediCal "
            "Dental. For any other plan we can still see you and submit a claim, but out-of-"
            "network coverage depends on your plan. We cannot promise coverage amounts."
        ),
        keywords=("insurance", "ppo", "in network", "covered", "claim"),
    ),
    PracticePolicy(
        topic="self_pay",
        question="Can I pay without insurance?",
        answer=(
            "Yes. Self-pay patients receive the quoted fees and can split payment across two "
            "visits when the treatment plan allows."
        ),
        keywords=("self pay", "no insurance", "cash", "out of pocket"),
    ),
    PracticePolicy(
        topic="payment_methods",
        question="What payment methods do you take?",
        answer=(
            "We accept card, Apple Pay, and HSA or FSA cards. Cash and checks are not accepted."
        ),
        keywords=("pay", "card", "payment", "hsa", "fsa", "apple pay"),
    ),
    PracticePolicy(
        topic="hours",
        question="When are you open?",
        answer=(
            "We are open Monday through Saturday, 8:00 AM to 6:00 PM Pacific, and closed on "
            "Sunday."
        ),
        keywords=("hours", "open", "close", "sunday", "weekend", "saturday"),
    ),
    PracticePolicy(
        topic="location",
        question="Where are you located?",
        answer=(
            "We are at 1717 Brannan Street, Suite 400, San Francisco, CA 94103, on the second "
            "floor with the entrance on the courtyard side. Free parking is in the rear lot; "
            "visitor spaces are on level P1."
        ),
        keywords=("address", "location", "where", "parking", "directions"),
    ),
    PracticePolicy(
        topic="contact",
        question="How do I reach the front desk?",
        answer=(
            "Call the front desk at (415) 555-0182 or email "
            "hello@oralbdental.example. We answer during business hours."
        ),
        keywords=("phone", "call", "email", "contact", "front desk"),
    ),
    PracticePolicy(
        topic="pediatric",
        question="Do you see children?",
        answer=(
            "Yes. Dr. Ilona Varga is our pediatric dentist and sees most patients under 13. "
            "Young children can be booked back-to-back with a parent."
        ),
        keywords=("kids", "children", "child", "pediatric", "my son", "my daughter"),
    ),
    PracticePolicy(
        topic="providers",
        question="Who are the dentists?",
        answer=(
            "Dr. Naomi Bell handles general and family dentistry, Dr. Kwame Osei covers "
            "endodontics and emergency care, and Dr. Ilona Varga is our pediatric specialist."
        ),
        keywords=("dentist", "who", "doctor", "provider"),
    ),
    PracticePolicy(
        topic="cancellation",
        question="What is your cancellation policy?",
        answer=(
            "Please give at least 24 hours' notice to change or cancel. Repeated late "
            "cancellations may require a deposit for future visits."
        ),
        keywords=("cancel", "cancellation", "miss", "no show", "reschedule"),
    ),
    PracticePolicy(
        topic="records_privacy",
        question="Will my records be shared?",
        answer=(
            "We only discuss a patient's records with that patient, a parent or guardian, or "
            "someone explicitly listed on the chart."
        ),
        keywords=("privacy", "records", "hipaa", "share"),
    ),
)
