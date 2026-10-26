"""Clinic knowledge lookups, answered from the resolved tenant.

Policy answers come from the clinic's own ``clinic_policies`` rows, reached through
``ClinicContext``. The assistant is instructed to relay ``answer`` verbatim, which
is what stops it from inventing prices, coverage, or availability.

Every function here takes the tenant explicitly rather than reading a module
constant. Under multi-tenancy that is the difference between "the practice's
policy" and "whatever clinic happened to be configured when this module was
imported": hours, prices, carriers and phone numbers are all per-clinic facts, and
a call that forgot to say which clinic would answer with the wrong practice's
number and sound entirely plausible doing it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppointmentType, Provider
from app.tenancy import ClinicContext
from app.validation import normalize_insurance


def list_topics(clinic: ClinicContext) -> list[dict[str, str]]:
    return [{"topic": policy.topic, "question": policy.question} for policy in clinic.policies]


def lookup_policy(clinic: ClinicContext, topic: str | None) -> dict | None:
    """Exact topic lookup within one tenant; None for anything it has not configured.

    Returning ``None`` rather than a near match is deliberate: the assistant is
    told to say it does not know when a lookup misses, so a fuzzy cross-clinic
    match would put tenant A's cancellation fee in tenant B's mouth.
    """
    policy = clinic.policy(topic)
    if policy is None:
        return None
    return {
        "topic": policy.topic,
        "question": policy.question,
        "answer": policy.answer,
        "source": "clinic_policy",
        "clinic": clinic.slug,
    }


def front_desk_contact(clinic: ClinicContext) -> dict:
    """The contact block, with hours assembled from the tenant's own grid."""
    return {
        "phone": clinic.phone,
        "email": clinic.email,
        "hours": clinic.front_desk_hours_phrase(),
        "timezone": clinic.config.timezone_name,
    }


def insurance_overview(clinic: ClinicContext) -> dict:
    return {
        "accepted_plans": [
            {"name": plan.name, "note": plan.note} for plan in clinic.accepted_plans
        ],
        "self_pay_label": clinic.self_pay_label,
        "note": clinic.insurance_note
        or (
            "Out-of-network plans may still be used, but coverage is never guaranteed by "
            "the assistant; the front desk verifies benefits."
        ),
    }


def describe_insurance_choice(clinic: ClinicContext, value: str | None) -> dict:
    """Turn a stored provider string into something safe to say out loud."""
    normalized = normalize_insurance(
        value,
        accepted_plans=sorted(clinic.accepted_plan_names),
        self_pay_label=clinic.self_pay_label,
    )
    if not normalized.value:
        return {"name": None, "recognized": False, "in_network": False, "self_pay": False}
    name = normalized.value
    accepted = clinic.accepted_plan_names
    self_pay = clinic.self_pay_label
    return {
        "name": name,
        "recognized": name in accepted or name == self_pay,
        "in_network": name in accepted,
        "self_pay": name == self_pay,
    }


def practice_summary(session: Session, clinic: ClinicContext) -> dict:
    """Public clinic facts plus this tenant's roster. Safe to hand to the model verbatim.

    ``providers`` and ``appointment_types`` are entity selects, so the session's
    tenant binding filters them; the summary cannot mix one clinic's dentists with
    another's hours.
    """
    providers = session.scalars(
        select(Provider).where(Provider.active.is_(True)).order_by(Provider.id)
    ).all()
    types = session.scalars(select(AppointmentType).order_by(AppointmentType.sort_order)).all()
    return {
        "clinic": clinic.as_public_dict(),
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
        "insurance": insurance_overview(clinic),
        "contact": front_desk_contact(clinic),
        "policy_topics": list_topics(clinic),
    }


__all__ = [
    "describe_insurance_choice",
    "front_desk_contact",
    "insurance_overview",
    "list_topics",
    "lookup_policy",
    "practice_summary",
]
