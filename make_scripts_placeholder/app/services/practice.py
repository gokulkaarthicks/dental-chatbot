"""Clinic knowledge lookups backed by configured data.

Policy answers come from ``app.clinic.PRACTICE_POLICIES``. The assistant is
instructed to relay ``answer`` verbatim, which is what stops it from inventing
prices, coverage, or availability.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clinic import (
    ACCEPTED_INSURANCE_PLANS,
    CLINIC,
    PRACTICE_POLICIES,
    SELF_PAY_LABEL,
)
from app.models import AppointmentType, Provider
from app.validation import normalize_insurance


def _normalize_topic(topic: str | None) -> str:
    return (topic or "").strip().lower().replace(" ", "_").replace("-", "_")


def list_topics() -> list[dict[str, str]]:
    return [{"topic": policy.topic, "question": policy.question} for policy in PRACTICE_POLICIES]


def lookup_policy(topic: str | None) -> dict | None:
    """Exact topic lookup; returns None for anything not configured."""
    cleaned = _normalize_topic(topic)
    for policy in PRACTICE_POLICIES:
        if policy.topic == cleaned:
            return {
                "topic": policy.topic,
                "question": policy.question,
                "answer": policy.answer,
                "source": "clinic_policy",
            }
    return None


def front_desk_contact() -> dict:
    return {
        "phone": CLINIC.phone,
        "email": CLINIC.email,
        "hours": "Mon-Sat, 8:00 AM - 6:00 PM Pacific",
        "timezone": CLINIC.timezone,
    }


def insurance_overview() -> dict:
    return {
        "accepted_plans": [
            {"name": plan.name, "note": plan.note} for plan in ACCEPTED_INSURANCE_PLANS
        ],
        "self_pay_label": SELF_PAY_LABEL,
        "note": (
            "Out-of-network plans may still be used, but coverage is never guaranteed by "
            "the assistant; the front desk verifies benefits."
        ),
    }


def describe_insurance_choice(value: str | None) -> dict:
    """Turn a stored provider string into something safe to say out loud."""
    normalized = normalize_insurance(value)
    if not normalized.value:
        return {"name": None, "recognized": False, "in_network": False, "self_pay": False}
    name = normalized.value
    accepted = {plan.name for plan in ACCEPTED_INSURANCE_PLANS}
    return {
        "name": name,
        "recognized": name in accepted or name == SELF_PAY_LABEL,
        "in_network": name in accepted,
        "self_pay": name == SELF_PAY_LABEL,
    }


def practice_summary(session: Session) -> dict:
    """Public clinic facts. Safe to hand to the model verbatim."""
    providers = session.scalars(
        select(Provider).where(Provider.active.is_(True)).order_by(Provider.id)
    ).all()
    types = session.scalars(select(AppointmentType).order_by(AppointmentType.sort_order)).all()
    return {
        "clinic": CLINIC.as_public_dict(),
        "providers": [
            {
                "name": provider.full_name,
                "credentials": provider.credentials,
                "specialties": provider.specialty_names,
            }
            for provider in providers
        ],
        "appointment_types": [
            {
                "code": item.code,
                "name": item.name,
                "duration_minutes": item.duration_minutes,
                "is_emergency": item.is_emergency,
            }
            for item in types
        ],
        "insurance": insurance_overview(),
        "contact": front_desk_contact(),
        "policy_topics": list_topics(),
    }
