"""Identity tools: who is speaking, and their registration details."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field
from sqlalchemy import select

from app.agent.context import TurnDeps
from app.agent.tools.results import (
    NoArguments,
    ToolArguments,
    require_verified,
    tool_error,
    tool_result,
)
from app.db import require_clinic_id
from app.models import FamilyGroup, FamilyMember, Patient
from app.observability import log_event
from app.services import (
    audit,
    availability_read,
    patient_sessions,
    patients,
    practice,
)
from app.validation import normalize_name, normalize_phone

logger = logging.getLogger(__name__)


@tool(
    "get_session_context",
    args_schema=NoArguments,
    response_format="content_and_artifact",
    description=(
        "Check whether this chat is already verified, which patient it belongs to, and "
        "which registration fields are still missing. Call this first on a new "
        "conversation or when you are unsure what has been collected."
    ),
)
def get_session_context(
    deps: Annotated[TurnDeps, InjectedState("deps")],
) -> tuple[str, dict[str, Any]]:
    patient = deps.verified_patient
    completeness = patients.registration_completeness(patient) if patient else None
    return tool_result(
        {
            "ok": True,
            "verified": patient is not None,
            "patient": patients.patient_profile(deps.clinic, patient) if patient else None,
            "registration_complete": bool(completeness and all(completeness.values())),
            "missing_fields": (
                [name for name, ok in completeness.items() if not ok] if completeness else []
            ),
            "can_show_availability": bool(completeness and all(completeness.values())),
            "clinic_today": availability_read.scheduling_context_summary(
                deps.session, deps.clinic
            )["clinic_today"],
        }
    )


class VerifyPatientArgs(ToolArguments):
    phone: str = Field(description="US phone number exactly as the caller gave it.")
    date_of_birth: str = Field(description="Date of birth, ideally MM/DD/YYYY.")


@tool(
    "verify_existing_patient",
    args_schema=VerifyPatientArgs,
    response_format="content_and_artifact",
    description=(
        "Verify an existing patient using their phone number and date of birth. Only "
        "after this succeeds may you discuss that patient's appointments."
    ),
)
def verify_existing_patient(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    phone: str,
    date_of_birth: str,
) -> tuple[str, dict[str, Any]]:
    record = deps.patient_session
    if patient_sessions.verification_locked(record):
        # Refuse before touching the chart, so a locked-out chat cannot probe at all.
        audit.record(
            deps.session,
            action="identity.verification_locked",
            actor_type="patient_session",
            actor_id=record.id,
            conversation_id=deps.conversation_id,
        )
        return tool_result(
            tool_error("verification_locked", patient_sessions.VERIFICATION_LOCKED_MESSAGE)
        )

    result = patients.verify_patient(
        deps.session,
        deps.clinic,
        phone=phone,
        date_of_birth=date_of_birth,
    )
    if result["verified"]:
        patient = deps.session.get(Patient, result["patient_id"])
        if patient is None:  # pragma: no cover - defensive
            return tool_result(tool_error("unexpected", "I couldn't open that chart."))
        patient_sessions.mark_verified(
            deps.session,
            record,
            patient,
            method=patient_sessions.VERIFICATION_BY_LOOKUP,
        )
        first_name = patient.full_name.split(" ")[0]
        return tool_result(
            {
                "ok": True,
                "verified": True,
                "first_name": first_name,
                "registration_complete": all(
                    patients.registration_completeness(patient).values()
                ),
                "message": (
                    f"Verified {first_name}. You can now discuss their appointments and ask "
                    "about availability."
                ),
            }
        )

    # Only a genuine chart miss counts. A malformed phone or date never reached a
    # chart, and an ambiguous match is our data problem, not an attack by the caller.
    if result.get("reason") == "no_match":
        if patient_sessions.record_failed_verification(deps.session, record):
            audit.record(
                deps.session,
                action="identity.verification_lockout",
                actor_type="patient_session",
                actor_id=record.id,
                conversation_id=deps.conversation_id,
            )
            log_event(
                logger,
                "identity.verification_lockout",
                level=logging.WARNING,
                conversation_id=deps.conversation_id,
                minutes=patient_sessions.VERIFICATION_LOCKOUT.total_seconds() / 60,
            )
            # This miss is the one that closed the window, so say so now rather
            # than inviting another guess with the ordinary "didn't match" reply.
            return tool_result(
                tool_error("verification_locked", patient_sessions.VERIFICATION_LOCKED_MESSAGE)
            )
    return tool_result(
        tool_error(
            result.get("reason") or "no_match",
            result.get("message") or "I couldn't verify that.",
        )
    )


class FindRegistrationDuplicatesArgs(ToolArguments):
    full_name: str = Field(description="Full name to check.")
    phone: str = Field(description="US phone number for the same person.")


@tool(
    "find_registration_duplicates",
    args_schema=FindRegistrationDuplicatesArgs,
    response_format="content_and_artifact",
    description=(
        "Check whether a name/phone pair is already registered, and whether the phone is "
        "shared by family members. Returns counts only, never record details."
    ),
)
def find_registration_duplicates(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    full_name: str,
    phone: str,
) -> tuple[str, dict[str, Any]]:
    normalized_phone = normalize_phone(phone)
    if not normalized_phone.ok or not normalized_phone.e164:
        return tool_result(
            tool_error(
                "invalid_phone",
                normalized_phone.error or "That phone number doesn't look right.",
            )
        )
    name = normalize_name(full_name)
    if not name.ok or not name.value:
        return tool_result(
            tool_error("invalid_name", name.error or "Could you give the full name?")
        )

    summary = patients.find_duplicate_summary(
        deps.session, deps.clinic, normalized_phone.e164, name.value
    )
    hints: list[str] = []
    if summary["same_name"]:
        hints.append("A chart with this exact name and phone already exists.")
    elif summary["likely_household"]:
        hints.append(
            "This phone is already used by other family members, which is normal. "
            "Sharing a phone number does not block registration."
        )
    return tool_result(
        {
            "ok": True,
            "match_count": summary["match_count"],
            "same_name_exists": summary["same_name"],
            "shared_phone_likely_household": summary["likely_household"],
            "hints": hints,
        }
    )


class RegisterPatientArgs(ToolArguments):
    full_name: str = Field(description="Patient's full name, first and last.")
    phone: str = Field(description="US phone number.")
    date_of_birth: str = Field(description="Date of birth, ideally MM/DD/YYYY.")
    insurance_provider: str = Field(
        description=(
            "Insurance plan name (e.g. 'Sunnycoast Dental PPO'), the patient's own "
            "wording for another plan, or 'Self-pay' when they have no insurance."
        )
    )


@tool(
    "register_patient",
    args_schema=RegisterPatientArgs,
    response_format="content_and_artifact",
    description=(
        "Register a new patient. Requires full name, phone, date of birth, and insurance "
        "provider or Self-pay. If validation fails, ask only for the listed fields."
    ),
)
def register_patient(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    full_name: str,
    phone: str,
    date_of_birth: str,
    insurance_provider: str,
) -> tuple[str, dict[str, Any]]:
    if deps.verified_patient is not None:
        return tool_result(
            tool_error(
                "already_registered",
                (
                    "This session is already signed in as a registered patient. Use "
                    "update_patient_details to correct anything instead of registering again."
                ),
            )
        )

    draft = patients.normalize_registration_fields(
        deps.clinic,
        full_name=full_name,
        phone=phone,
        date_of_birth=date_of_birth,
        insurance_provider=insurance_provider,
    )
    if not draft.issues.ok:
        return tool_result(
            tool_error(
                "validation_failed",
                (
                    "Some details need to be clarified. Ask for only the fields listed in "
                    "field_errors."
                ),
                field_errors=draft.issues.errors,
            )
        )

    result = patients.register_patient(
        deps.session,
        deps.clinic,
        full_name=str(draft.values["full_name"]),
        phone=str(draft.values["phone"]),
        date_of_birth=str(draft.values["date_of_birth"]),
        insurance_provider=str(draft.values["insurance_provider"]),
    )
    if not result.get("created"):
        return tool_result(
            tool_error(result.get("reason", "registration_failed"), result.get("message", ""))
        )

    patient = deps.session.get(Patient, result["patient_id"])
    if patient is None:  # pragma: no cover - defensive
        return tool_result(tool_error("unexpected", "Registration failed unexpectedly."))
    patient_sessions.mark_verified(
        deps.session,
        deps.patient_session,
        patient,
        method=patient_sessions.VERIFICATION_BY_REGISTRATION,
    )

    profile = patients.patient_profile(deps.clinic, patient)
    plan = practice.describe_insurance_choice(deps.clinic, patient.insurance_provider)
    return tool_result(
        {
            "ok": True,
            "registered": True,
            "first_name": profile["first_name"],
            "date_of_birth": profile["date_of_birth"],
            "date_format_was_ambiguous": draft.date_ambiguous,
            "registration_complete": profile["registration_complete"],
            "missing_fields": profile["missing_fields"],
            "insurance": {
                "plan": plan["name"],
                "recognized": plan["recognized"],
                "self_pay": plan["self_pay"],
            },
            "message": (
                "Registration saved. Keep the plan name exactly as stored when confirming it. "
                "You can now discuss availability."
            ),
        }
    )


class RegisterChildArgs(ToolArguments):
    """Register a minor for the verified parent and link the household.

    This deliberately does not accept a parent id, phone number, or family id.
    All of those come from the already verified caller, preventing a model from
    attaching a child to someone else's chart.
    """

    full_name: str = Field(description="Child's full first and last name.")
    date_of_birth: str = Field(description="Child's date of birth, ideally MM/DD/YYYY.")
    insurance_provider: str | None = Field(
        default=None,
        description="Child's insurance plan, or omit only when use_parent_insurance is true.",
    )
    use_parent_insurance: bool = Field(
        default=False,
        description="True only after the verified parent says the child uses the same plan.",
    )


@tool(
    "register_child_for_verified_parent",
    args_schema=RegisterChildArgs,
    response_format="content_and_artifact",
    description=(
        "Register a verified parent's minor child, using the parent's phone and optionally "
        "the parent's insurance after they say it is shared. Use when a parent requests a "
        "family appointment and the child is not yet on the account. It links the child so "
        "search_family_availability can make one combined proposal. Never use for another "
        "adult."
    ),
)
def register_child_for_verified_parent(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    full_name: str,
    date_of_birth: str,
    insurance_provider: str | None,
    use_parent_insurance: bool,
) -> tuple[str, dict[str, Any]]:
    """Create or safely link a verified caller's minor child.

    A parent may schedule for their minor children, but this tool never changes
    who is verified in the conversation. The adult remains the requester and
    gets exactly the household membership needed for a single atomic family
    proposal.
    """
    parent, problem = require_verified(deps)
    if problem is not None or parent is None:
        return tool_result(problem or tool_error("not_verified", "Verify the parent first."))

    plan = parent.insurance_provider if use_parent_insurance else insurance_provider
    if not plan:
        return tool_result(
            tool_error(
                "insurance_required",
                "Ask whether the child uses the parent's plan or which plan they use.",
            )
        )

    draft = patients.normalize_registration_fields(
        deps.clinic,
        full_name=full_name,
        # The phone belongs to the already verified parent. Do not reject a
        # legacy chart merely because it predates the current input validator.
        phone=None,
        date_of_birth=date_of_birth,
        insurance_provider=plan,
        required=("full_name", "date_of_birth", "insurance_provider"),
    )
    if not draft.issues.ok:
        return tool_result(
            tool_error(
                "validation_failed",
                "Some child details need to be clarified. Ask only for the fields listed.",
                field_errors=draft.issues.errors,
            )
        )

    child_dob = draft.values["date_of_birth"]
    assert hasattr(child_dob, "year")  # narrowed by normalize_registration_fields
    today = deps.clinic.config.today()
    age = today.year - child_dob.year
    age -= (today.month, today.day) < (child_dob.month, child_dob.day)
    if age >= 18:
        return tool_result(
            tool_error(
                "adult_family_member",
                "I can only add a minor child here. Another adult needs to contact us directly.",
            )
        )

    resolved_name = str(draft.values["full_name"])
    existing = deps.session.scalar(
        select(Patient).where(
            Patient.full_name == resolved_name,
            Patient.phone == parent.phone,
            Patient.date_of_birth == child_dob,
        )
    )
    created = existing is None
    if existing is None:
        registered = patients.register_patient(
            deps.session,
            deps.clinic,
            full_name=resolved_name,
            phone=parent.phone,
            date_of_birth=child_dob.isoformat(),
            insurance_provider=str(draft.values["insurance_provider"]),
        )
        if not registered.get("created"):
            return tool_result(
                tool_error(
                    registered.get("reason", "registration_failed"),
                    registered.get("message", "I couldn't register the child."),
                )
            )
        child = deps.session.get(Patient, registered["patient_id"])
    else:
        child = existing
    if child is None:  # pragma: no cover - defensive after an inserted row
        return tool_result(tool_error("registration_failed", "I couldn't register the child."))

    memberships = list(
        deps.session.scalars(select(FamilyMember).where(FamilyMember.patient_id == parent.id))
    )
    group_ids = {membership.family_group_id for membership in memberships}
    if len(group_ids) > 1:
        return tool_result(
            tool_error(
                "ambiguous_household",
                "I need the front desk to confirm which household to use before adding a child.",
            )
        )
    if memberships:
        family_group_id = memberships[0].family_group_id
    else:
        group = FamilyGroup(
            clinic_id=require_clinic_id(deps.session),
            name=f"{parent.full_name} Household",
            shared_phone=parent.phone,
            notes=None,
        )
        deps.session.add(group)
        deps.session.flush()
        family_group_id = group.id
        deps.session.add(
            FamilyMember(
                clinic_id=require_clinic_id(deps.session),
                family_group_id=family_group_id,
                patient_id=parent.id,
                relation_to_primary="self",
                is_primary_contact=True,
            )
        )

    linked = deps.session.scalar(
        select(FamilyMember).where(
            FamilyMember.family_group_id == family_group_id,
            FamilyMember.patient_id == child.id,
        )
    )
    if linked is None:
        deps.session.add(
            FamilyMember(
                clinic_id=require_clinic_id(deps.session),
                family_group_id=family_group_id,
                patient_id=child.id,
                relation_to_primary="child",
                is_primary_contact=False,
            )
        )
        audit.record(
            deps.session,
            action="family.child_linked",
            actor_type="patient_session",
            entity_type="patient",
            entity_id=child.id,
            details={"parent_id": parent.id, "created": created},
        )
    deps.session.flush()
    return tool_result(
        {
            "ok": True,
            "registered": created,
            "linked": True,
            "first_name": child.full_name.split(" ")[0],
            "family_group_id": family_group_id,
            "message": (
                "The child is registered and linked to the verified parent. Continue the "
                "original family request with search_family_availability for both people. Do "
                "not offer a solo slot."
            ),
        }
    )


class UpdatePatientArgs(ToolArguments):
    full_name: str | None = Field(default=None, description="Corrected full name.")
    phone: str | None = Field(default=None, description="Corrected phone number.")
    date_of_birth: str | None = Field(default=None, description="Corrected date of birth.")
    insurance_provider: str | None = Field(
        default=None, description="Corrected insurance or 'Self-pay'."
    )


@tool(
    "update_patient_details",
    args_schema=UpdatePatientArgs,
    response_format="content_and_artifact",
    description=(
        "Correct one or more details on the verified patient's chart. Use this for "
        "corrections instead of restarting registration."
    ),
)
def update_patient_details(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    full_name: str | None,
    phone: str | None,
    date_of_birth: str | None,
    insurance_provider: str | None,
) -> tuple[str, dict[str, Any]]:
    patient, problem = require_verified(deps)
    if problem is not None or patient is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))

    if not any([full_name, phone, date_of_birth, insurance_provider]):
        return tool_result(
            tool_error("no_changes", "Tell me which detail you'd like to correct.")
        )

    result = patients.update_patient(
        deps.session,
        deps.clinic,
        patient,
        full_name=full_name,
        phone=phone,
        date_of_birth=date_of_birth,
        insurance_provider=insurance_provider,
    )
    if not result.get("updated"):
        return tool_result(
            tool_error(
                "validation_failed",
                (
                    "Some details need to be clarified. Only the listed field is wrong; "
                    "everything else is saved."
                ),
                field_errors=result.get("field_errors", {}),
            )
        )
    profile = patients.patient_profile(deps.clinic, patient)
    return tool_result(
        {
            "ok": True,
            "changed_fields": result["changed_fields"],
            "patient": profile,
            "registration_complete": profile["registration_complete"],
        }
    )


TOOLS = (
    get_session_context,
    verify_existing_patient,
    find_registration_duplicates,
    register_patient,
    register_child_for_verified_parent,
    update_patient_details,
)


__all__ = [
    "TOOLS",
    "FindRegistrationDuplicatesArgs",
    "RegisterChildArgs",
    "RegisterPatientArgs",
    "UpdatePatientArgs",
    "VerifyPatientArgs",
]
