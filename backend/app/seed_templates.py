"""Seed-time clinic templates for the demo tenants. Fictional data only.

This module is the *only* place a clinic's facts are written as Python literals,
and it is read exclusively by the seeding path (``app.services.seed`` and the CLI)
when creating or refreshing tenant rows. Nothing at request time imports it:
runtime answers come from the clinic's database rows via
:class:`app.tenancy.ClinicContext`. That separation is what keeps ``/api/clinic``,
the system prompt and the tools from drifting apart, which is the property the
original module-level constants gave by construction.

Tenant 1 preserves the exact values the single-clinic build used, including the
original ``seed_key`` fragments and provider roster, so existing demos, tests and
screenshots keep working. Tenant 2 is deliberately *different in kind*, not just
in text: a 20-minute grid, Eastern time, and Tuesday-Saturday hours. If the second
clinic were only a rename, a leaked cross-tenant booking or a wrong timezone
answer would still look correct.
"""

from __future__ import annotations

from datetime import time

from app.clinic import ClinicConfig, InsurancePlan, PracticePolicy, hours_map_from
from app.timeutil import LEGACY_CLINIC_SCHEDULING, SchedulingConfig

# --------------------------------------------------------------------------- #
# Tenant 1: Oral-B Dental Care (the original demo clinic)
# --------------------------------------------------------------------------- #

DEFAULT_CLINIC = ClinicConfig(
    name="Oral-B Dental Care",
    legal_note="Fictional clinic created for a product demonstration. Not a real practice.",
    address_line1="1717 Brannan Street, Suite 400",
    address_line2="Second floor, entrance on the courtyard side",
    city="San Francisco",
    state="CA",
    postal_code="94103",
    phone="+1 (415) 555-0182",
    email="hello@oralbdental.example",
    timezone=LEGACY_CLINIC_SCHEDULING.timezone_name,
    slot_minutes=LEGACY_CLINIC_SCHEDULING.slot_minutes,
    business_hours=hours_map_from(
        LEGACY_CLINIC_SCHEDULING.open_time,
        LEGACY_CLINIC_SCHEDULING.close_time,
        LEGACY_CLINIC_SCHEDULING.open_weekdays,
    ),
    closed_days=("Sun",),
    parking_note="Free parking in the rear lot; visitor spaces are on level P1.",
    timezone_note="All times are Pacific Time.",
)

DEFAULT_SLUG = "oral-b-dental-care"
DEFAULT_SELF_PAY_LABEL = "Self-pay"
DEFAULT_INSURANCE_NOTE = (
    "Out-of-network plans may still be used, but coverage is never guaranteed by "
    "the assistant; the front desk verifies benefits."
)

# Accepted plans. Everything else is answered as "check with the front desk".
DEFAULT_ACCEPTED_INSURANCE_PLANS: tuple[InsurancePlan, ...] = (
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

# Practice policies. These are the ONLY source for policy/pricing answers.
#
# Prices are deliberately expressed as ranges and prefixed so the assistant
# relays them verbatim instead of inventing a quote. Coverage language avoids
# promising any specific benefit amount.
DEFAULT_PRACTICE_POLICIES: tuple[PracticePolicy, ...] = (
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
        topic="membership_plan",
        question="Do you offer a membership or financing plan without insurance?",
        answer=(
            "Yes. Our in-house membership is $29 a month and includes two cleanings and two "
            "checkups a year plus 15% off other treatment. We also offer third-party financing "
            "for larger plans, subject to approval. The front desk confirms the current terms "
            "before you enroll."
        ),
        keywords=("membership", "financing", "payment plan", "no insurance", "monthly"),
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

# --------------------------------------------------------------------------- #
# Tenant 2: Bayview Dental Studio
#
# Different in kind, not just in text: a 20-minute grid, Eastern time, and
# Tuesday-Saturday hours. Its first-of-month Monday is therefore closed while
# tenant 1's is open, which is the cleanest observable proof that the two clinics
# are answering from their own configuration.
# --------------------------------------------------------------------------- #

BAYVIEW_SCHEDULING = SchedulingConfig(
    timezone_name="America/New_York",
    slot_minutes=20,
    open_time=time(9, 0),
    close_time=time(17, 0),
    open_weekdays=frozenset({1, 2, 3, 4, 5}),
    closed_day_labels=("Sun", "Mon"),
    seed_horizon_days=45,
)

BAYVIEW_CLINIC = ClinicConfig(
    name="Bayview Dental Studio",
    legal_note="Fictional clinic created for a product demonstration. Not a real practice.",
    address_line1="88 Furman Street, Suite 210",
    address_line2="Waterfront concourse, entrance by the ferry landing",
    city="Brooklyn",
    state="NY",
    postal_code="11231",
    phone="+1 (718) 555-0143",
    email="hello@bayviewstudio.example",
    timezone=BAYVIEW_SCHEDULING.timezone_name,
    slot_minutes=BAYVIEW_SCHEDULING.slot_minutes,
    business_hours=hours_map_from(
        BAYVIEW_SCHEDULING.open_time,
        BAYVIEW_SCHEDULING.close_time,
        BAYVIEW_SCHEDULING.open_weekdays,
    ),
    closed_days=("Sun", "Mon"),
    parking_note="Metered street parking; the Studio garage on Slate Street is an easy walk.",
    timezone_note="All times are Eastern Time.",
)

BAYVIEW_SLUG = "bayview-dental-studio"
BAYVIEW_SELF_PAY_LABEL = "Self-pay"
BAYVIEW_INSURANCE_NOTE = (
    "Out-of-network plans may still be used, but coverage is never guaranteed by "
    "the assistant; the front desk verifies benefits."
)

BAYVIEW_INSURANCE_PLANS: tuple[InsurancePlan, ...] = (
    InsurancePlan(
        name="Empire Dental Choice",
        accepted=True,
        note="In-network PPO. Preventive visits carry no copay under most plans.",
    ),
    InsurancePlan(
        name="Liberty Smile Care",
        accepted=True,
        note="In-network. Requires a referral from a participating general dentist.",
    ),
)

BAYVIEW_PRACTICE_POLICIES: tuple[PracticePolicy, ...] = (
    PracticePolicy(
        topic="new_patient_exam",
        question="Do you accept new patients, and what does a first visit include?",
        answer=(
            "Yes, we take new patients aged 12 and up. A first visit is an exam with digital "
            "X-rays and an oral cancer screening, about 45 minutes."
        ),
        keywords=("new patient", "first visit", "exam"),
    ),
    PracticePolicy(
        topic="cleaning_cost",
        question="How much does a cleaning cost?",
        answer=(
            "Without insurance a routine cleaning is quoted at $140-$185 depending on the "
            "hygienist's assessment. Insurance often covers part or all of it, and the front "
            "desk verifies benefits before treatment."
        ),
        keywords=("cost", "price", "cleaning", "how much"),
    ),
    PracticePolicy(
        topic="exam_cost",
        question="How much does a general checkup cost?",
        answer=(
            "A checkup with digital X-rays is quoted at $110-$150 without insurance. Coverage "
            "varies by plan, so we confirm your benefits first."
        ),
        keywords=("checkup", "check up", "cost", "price", "exam price"),
    ),
    PracticePolicy(
        topic="emergency_visit",
        question="What about an emergency visit?",
        answer=(
            "We keep two same-day emergency openings each day we are open, for pain or a broken "
            "tooth. An emergency visit starts at $175 and further treatment is quoted before it "
            "begins. If this is a life-threatening emergency, call 911."
        ),
        keywords=("emergency", "pain", "toothache", "broken tooth", "urgent"),
    ),
    PracticePolicy(
        topic="insurance_accepted",
        question="Which dental insurance plans do you accept?",
        answer=(
            "We are in-network with Empire Dental Choice and Liberty Smile Care. For any other "
            "plan we can still see you and submit a claim, but out-of-network coverage depends "
            "on your plan. We cannot promise coverage amounts."
        ),
        keywords=("insurance", "ppo", "in network", "covered", "claim"),
    ),
    PracticePolicy(
        topic="self_pay",
        question="Can I pay without insurance?",
        answer=(
            "Yes. Self-pay patients are quoted the flat fee and may split it across two visits."
        ),
        keywords=("self pay", "no insurance", "cash", "out of pocket"),
    ),
    PracticePolicy(
        topic="membership_plan",
        question="Do you offer a membership or financing plan without insurance?",
        answer=(
            "Yes. The Studio Plan is $34 a month and includes two cleanings, two checkups and "
            "one X-ray set a year, plus 12% off other treatment. Third-party financing is "
            "available for larger plans, subject to approval."
        ),
        keywords=("membership", "financing", "payment plan", "no insurance", "monthly"),
    ),
    PracticePolicy(
        topic="payment_methods",
        question="What payment methods do you take?",
        answer="We accept card, Apple Pay, Google Pay, and HSA or FSA cards.",
        keywords=("pay", "card", "payment", "hsa", "fsa", "apple pay"),
    ),
    PracticePolicy(
        topic="hours",
        question="When are you open?",
        answer=(
            "We are open Tuesday through Saturday, 9:00 AM to 5:00 PM Eastern, and closed on "
            "Sunday and Monday."
        ),
        keywords=("hours", "open", "close", "sunday", "monday", "weekend"),
    ),
    PracticePolicy(
        topic="location",
        question="Where are you located?",
        answer=(
            "We are at 88 Furman Street, Suite 210, Brooklyn, NY 11231, on the waterfront "
            "concourse with the entrance by the ferry landing. Metered street parking is "
            "available and the Slate Street garage is a short walk."
        ),
        keywords=("address", "location", "where", "parking", "directions"),
    ),
    PracticePolicy(
        topic="contact",
        question="How do I reach the front desk?",
        answer=(
            "Call the front desk at (718) 555-0143 or email hello@bayviewstudio.example. We "
            "answer during business hours."
        ),
        keywords=("phone", "call", "email", "contact", "front desk"),
    ),
    PracticePolicy(
        topic="providers",
        question="Who are the dentists?",
        answer=(
            "Dr. Priya Raman handles general and cosmetic dentistry, and Dr. Elias Duarte "
            "covers restorative work and emergency care."
        ),
        keywords=("dentist", "who", "doctor", "provider"),
    ),
    PracticePolicy(
        topic="cancellation",
        question="What is your cancellation policy?",
        answer=(
            "Please give at least 24 hours' notice to change or cancel. Two late cancellations "
            "in a year may require a deposit for future visits."
        ),
        keywords=("cancel", "cancellation", "miss", "no show", "reschedule"),
    ),
    PracticePolicy(
        topic="records_privacy",
        question="Will my records be shared?",
        answer=(
            "We only discuss a patient's records with that patient, a legal guardian, or "
            "someone named on the chart."
        ),
        keywords=("privacy", "records", "hipaa", "share"),
    ),
)


__all__ = [
    "BAYVIEW_CLINIC",
    "BAYVIEW_INSURANCE_NOTE",
    "BAYVIEW_INSURANCE_PLANS",
    "BAYVIEW_PRACTICE_POLICIES",
    "BAYVIEW_SCHEDULING",
    "BAYVIEW_SELF_PAY_LABEL",
    "BAYVIEW_SLUG",
    "DEFAULT_ACCEPTED_INSURANCE_PLANS",
    "DEFAULT_CLINIC",
    "DEFAULT_INSURANCE_NOTE",
    "DEFAULT_PRACTICE_POLICIES",
    "DEFAULT_SELF_PAY_LABEL",
    "DEFAULT_SLUG",
]
