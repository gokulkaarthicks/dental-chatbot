"""Tool definitions exposed to the model.

Every tool declares a Pydantic argument model. Arguments arrive as a JSON string
from the provider and are validated before any service is called, so a
hallucinated field or a bad date becomes a structured, recoverable tool result
instead of an exception.

Registration state is *never* taken from model arguments. Tools that need to
know who is speaking read ``ToolContext``, which the conversation runner
populates from the authenticated session only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clinic import PRACTICE_POLICIES
from app.models import FamilyGroup, FamilyMember, Patient, PatientSession
from app.services import audit, availability, patient_sessions, patients, practice
from app.timeutil import clinic_today
from app.validation import normalize_name, normalize_phone

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 6000


# --------------------------------------------------------------------------- #
# Context and argument models
# --------------------------------------------------------------------------- #


@dataclass
class ToolContext:
    """Request-scoped state. Built by the runner from the session, not the model."""

    session: Session
    patient_session: PatientSession
    conversation_id: int

    @property
    def verified_patient(self) -> Patient | None:
        return patient_sessions.verified_patient(self.session, self.patient_session)


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArguments(ToolArguments):
    """For tools that take no input."""


class VerifyPatientArgs(ToolArguments):
    phone: str = Field(description="US phone number exactly as the caller gave it.")
    date_of_birth: str = Field(description="Date of birth, ideally MM/DD/YYYY.")


class FindRegistrationDuplicatesArgs(ToolArguments):
    full_name: str = Field(description="Full name to check.")
    phone: str = Field(description="US phone number for the same person.")


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


class UpdatePatientArgs(ToolArguments):
    full_name: str | None = Field(default=None, description="Corrected full name.")
    phone: str | None = Field(default=None, description="Corrected phone number.")
    date_of_birth: str | None = Field(default=None, description="Corrected date of birth.")
    insurance_provider: str | None = Field(
        default=None, description="Corrected insurance or 'Self-pay'."
    )


class PracticeInfoArgs(ToolArguments):
    topic: str | None = Field(
        default=None,
        description=(
            "Policy topic such as 'hours', 'location', 'insurance_accepted', "
            "'cleaning_cost'. Omit to list available topics."
        ),
    )


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": code, "message": message}
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #


def _get_verified_patient(ctx: ToolContext) -> tuple[Patient | None, dict[str, Any] | None]:
    patient = ctx.verified_patient
    if patient is None:
        return None, _error(
            "not_verified",
            (
                "This session isn't verified. Ask for phone and date of birth to verify an "
                "existing patient, or register them first."
            ),
        )
    return patient, None


def _tool_get_session_context(ctx: ToolContext, _: NoArguments) -> dict[str, Any]:
    """Who we're talking to, and what is still missing before availability."""
    patient = ctx.verified_patient
    completeness = patients.registration_completeness(patient) if patient else None
    return {
        "ok": True,
        "verified": patient is not None,
        "patient": patients.patient_profile(patient) if patient else None,
        "registration_complete": bool(completeness and all(completeness.values())),
        "missing_fields": (
            [name for name, ok in completeness.items() if not ok] if completeness else []
        ),
        "can_show_availability": bool(completeness and all(completeness.values())),
        "clinic_today": availability.scheduling_context_summary(ctx.session)["clinic_today"],
    }


def _tool_find_registration_duplicates(
    ctx: ToolContext, args: FindRegistrationDuplicatesArgs
) -> dict[str, Any]:
    phone = normalize_phone(args.phone)
    if not phone.ok or not phone.e164:
        return _error("invalid_phone", phone.error or "That phone number doesn't look right.")
    name = normalize_name(args.full_name)
    if not name.ok or not name.value:
        return _error("invalid_name", name.error or "Could you give the full name?")

    summary = patients.find_duplicate_summary(ctx.session, phone.e164, name.value)
    hints: list[str] = []
    if summary["same_name"]:
        hints.append("A chart with this exact name and phone already exists.")
    elif summary["likely_household"]:
        hints.append(
            "This phone is already used by other family members, which is normal. "
            "Sharing a phone number does not block registration."
        )
    return {
        "ok": True,
        "match_count": summary["match_count"],
        "same_name_exists": summary["same_name"],
        "shared_phone_likely_household": summary["likely_household"],
        "hints": hints,
    }


def _tool_verify_patient(ctx: ToolContext, args: VerifyPatientArgs) -> dict[str, Any]:
    result = patients.verify_patient(
        ctx.session, phone=args.phone, date_of_birth=args.date_of_birth
    )
    if result["verified"]:
        patient = ctx.session.get(Patient, result["patient_id"])
        if patient is None:  # pragma: no cover - defensive
            return _error("unexpected", "I couldn't open that chart.")
        patient_sessions.mark_verified(
            ctx.session,
            ctx.patient_session,
            patient,
            method=patient_sessions.VERIFICATION_BY_LOOKUP,
        )
        return {
            "ok": True,
            "verified": True,
            "first_name": patient.full_name.split(" ")[0],
            "registration_complete": all(patients.registration_completeness(patient).values()),
            "message": (
                f"Verified {patient.full_name.split(' ')[0]}. You can now discuss their "
                "appointments and ask about availability."
            ),
        }
    return _error(
        result.get("reason") or "no_match", result.get("message") or "I couldn't verify that."
    )


def _tool_register_patient(ctx: ToolContext, args: RegisterPatientArgs) -> dict[str, Any]:
    existing = ctx.verified_patient
    if existing is not None:
        return _error(
            "already_registered",
            (
                "This session is already signed in as a registered patient. Use "
                "update_patient_details to correct anything instead of registering again."
            ),
        )

    draft = patients.normalize_registration_fields(
        full_name=args.full_name,
        phone=args.phone,
        date_of_birth=args.date_of_birth,
        insurance_provider=args.insurance_provider,
    )
    if not draft.issues.ok:
        return _error(
            "validation_failed",
            "Some details need to be clarified. Ask for only the fields listed in field_errors.",
            field_errors=draft.issues.errors,
        )

    result = patients.register_patient(
        ctx.session,
        full_name=str(draft.values["full_name"]),
        phone=str(draft.values["phone"]),
        date_of_birth=str(draft.values["date_of_birth"]),
        insurance_provider=str(draft.values["insurance_provider"]),
    )
    if not result.get("created"):
        return _error(result.get("reason", "registration_failed"), result.get("message", ""))

    patient = ctx.session.get(Patient, result["patient_id"])
    if patient is None:  # pragma: no cover - defensive
        return _error("unexpected", "Registration failed unexpectedly.")
    patient_sessions.mark_verified(
        ctx.session,
        ctx.patient_session,
        patient,
        method=patient_sessions.VERIFICATION_BY_REGISTRATION,
    )

    profile = patients.patient_profile(patient)
    plan = practice.describe_insurance_choice(patient.insurance_provider)
    return {
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


def _tool_register_child(ctx: ToolContext, args: RegisterChildArgs) -> dict[str, Any]:
    """Create or safely link a verified caller's minor child.

    A parent may schedule for their minor children, but this tool never changes
    who is verified in the conversation.  The adult remains the requester and
    gets exactly the household membership needed for a single atomic family
    proposal.
    """
    parent, problem = _get_verified_patient(ctx)
    if problem is not None or parent is None:
        return problem or _error("not_verified", "Verify the parent before adding a child.")

    insurance_provider = (
        parent.insurance_provider if args.use_parent_insurance else args.insurance_provider
    )
    if not insurance_provider:
        return _error(
            "insurance_required",
            "Ask whether the child uses the parent's plan or which plan they use.",
        )

    draft = patients.normalize_registration_fields(
        full_name=args.full_name,
        # The phone belongs to the already verified parent.  Do not reject a
        # legacy chart merely because it predates the current input validator.
        phone=None,
        date_of_birth=args.date_of_birth,
        insurance_provider=insurance_provider,
        required=("full_name", "date_of_birth", "insurance_provider"),
    )
    if not draft.issues.ok:
        return _error(
            "validation_failed",
            "Some child details need to be clarified. Ask only for the fields listed.",
            field_errors=draft.issues.errors,
        )

    child_dob = draft.values["date_of_birth"]
    assert hasattr(child_dob, "year")  # narrowed by normalize_registration_fields
    today = clinic_today()
    age = today.year - child_dob.year
    age -= (today.month, today.day) < (child_dob.month, child_dob.day)
    if age >= 18:
        return _error(
            "adult_family_member",
            "I can only add a minor child here. Another adult needs to contact us directly.",
        )

    full_name = str(draft.values["full_name"])
    existing = ctx.session.scalar(
        select(Patient).where(
            Patient.full_name == full_name,
            Patient.phone == parent.phone,
            Patient.date_of_birth == child_dob,
        )
    )
    created = existing is None
    if existing is None:
        registered = patients.register_patient(
            ctx.session,
            full_name=full_name,
            phone=parent.phone,
            date_of_birth=child_dob.isoformat(),
            insurance_provider=str(draft.values["insurance_provider"]),
        )
        if not registered.get("created"):
            return _error(
                registered.get("reason", "registration_failed"),
                registered.get("message", "I couldn't register the child."),
            )
        child = ctx.session.get(Patient, registered["patient_id"])
    else:
        child = existing
    if child is None:  # pragma: no cover - defensive after an inserted row
        return _error("registration_failed", "I couldn't register the child.")

    memberships = list(
        ctx.session.scalars(select(FamilyMember).where(FamilyMember.patient_id == parent.id))
    )
    group_ids = {membership.family_group_id for membership in memberships}
    if len(group_ids) > 1:
        return _error(
            "ambiguous_household",
            "I need the front desk to confirm which household to use before adding a child.",
        )
    if memberships:
        family_group_id = memberships[0].family_group_id
    else:
        group = FamilyGroup(
            name=f"{parent.full_name} Household",
            shared_phone=parent.phone,
            notes=None,
        )
        ctx.session.add(group)
        ctx.session.flush()
        family_group_id = group.id
        ctx.session.add(
            FamilyMember(
                family_group_id=family_group_id,
                patient_id=parent.id,
                relation_to_primary="self",
                is_primary_contact=True,
            )
        )

    linked = ctx.session.scalar(
        select(FamilyMember).where(
            FamilyMember.family_group_id == family_group_id,
            FamilyMember.patient_id == child.id,
        )
    )
    if linked is None:
        ctx.session.add(
            FamilyMember(
                family_group_id=family_group_id,
                patient_id=child.id,
                relation_to_primary="child",
                is_primary_contact=False,
            )
        )
        audit.record(
            ctx.session,
            action="family.child_linked",
            actor_type="patient_session",
            entity_type="patient",
            entity_id=child.id,
            details={"parent_id": parent.id, "created": created},
        )
    ctx.session.flush()
    return {
        "ok": True,
        "registered": created,
        "linked": True,
        "first_name": child.full_name.split(" ")[0],
        "family_group_id": family_group_id,
        "message": (
            "The child is registered and linked to the verified parent. Continue the original "
            "family request with search_family_availability for both people. Do not offer a "
            "solo slot."
        ),
    }


def _tool_update_patient(ctx: ToolContext, args: UpdatePatientArgs) -> dict[str, Any]:
    patient, problem = _get_verified_patient(ctx)
    if problem:
        return problem
    assert patient is not None

    if not any([args.full_name, args.phone, args.date_of_birth, args.insurance_provider]):
        return _error("no_changes", "Tell me which detail you'd like to correct.")

    result = patients.update_patient(
        ctx.session,
        patient,
        full_name=args.full_name,
        phone=args.phone,
        date_of_birth=args.date_of_birth,
        insurance_provider=args.insurance_provider,
    )
    if not result.get("updated"):
        return _error(
            "validation_failed",
            (
                "Some details need to be clarified. Only the listed field is wrong; "
                "everything else is saved."
            ),
            field_errors=result.get("field_errors", {}),
        )
    profile = patients.patient_profile(patient)
    return {
        "ok": True,
        "changed_fields": result["changed_fields"],
        "patient": profile,
        "registration_complete": profile["registration_complete"],
    }


def _tool_get_practice_info(ctx: ToolContext, args: PracticeInfoArgs) -> dict[str, Any]:
    del ctx
    if not args.topic:
        return {
            "ok": True,
            "topics": practice.list_topics(),
            "contact": practice.front_desk_contact(),
            "insurance": practice.insurance_overview(),
        }
    policy = practice.lookup_policy(args.topic)
    if policy is None:
        return _error(
            "unknown_topic",
            "That policy detail isn't in our knowledge base. Offer the front desk instead.",
            topics=[item.topic for item in PRACTICE_POLICIES],
        )
    return {"ok": True, **policy, "contact": practice.front_desk_contact()}


def _tool_get_practice_summary(ctx: ToolContext, _: NoArguments) -> dict[str, Any]:
    return {"ok": True, **practice.practice_summary(ctx.session)}


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[ToolArguments]
    handler: Callable[..., dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_session_context",
        description=(
            "Check whether this chat is already verified, which patient it belongs to, and "
            "which registration fields are still missing. Call this first on a new "
            "conversation or when you are unsure what has been collected."
        ),
        args_model=NoArguments,
        handler=_tool_get_session_context,
    ),
    ToolSpec(
        name="verify_existing_patient",
        description=(
            "Verify an existing patient using their phone number and date of birth. Only "
            "after this succeeds may you discuss that patient's appointments."
        ),
        args_model=VerifyPatientArgs,
        handler=_tool_verify_patient,
    ),
    ToolSpec(
        name="find_registration_duplicates",
        description=(
            "Check whether a name/phone pair is already registered, and whether the phone is "
            "shared by family members. Returns counts only, never record details."
        ),
        args_model=FindRegistrationDuplicatesArgs,
        handler=_tool_find_registration_duplicates,
    ),
    ToolSpec(
        name="register_patient",
        description=(
            "Register a new patient. Requires full name, phone, date of birth, and insurance "
            "provider or Self-pay. If validation fails, ask only for the listed fields."
        ),
        args_model=RegisterPatientArgs,
        handler=_tool_register_patient,
    ),
    ToolSpec(
        name="register_child_for_verified_parent",
        description=(
            "Register a verified parent's minor child, using the parent's phone and optionally "
            "the parent's insurance after they say it is shared. Use when a parent requests a "
            "family appointment and the child is not yet on the account. It links the child so "
            "search_family_availability can make one combined proposal. Never use for another "
            "adult."
        ),
        args_model=RegisterChildArgs,
        handler=_tool_register_child,
    ),
    ToolSpec(
        name="update_patient_details",
        description=(
            "Correct one or more details on the verified patient's chart. Use this for "
            "corrections instead of restarting registration."
        ),
        args_model=UpdatePatientArgs,
        handler=_tool_update_patient,
    ),
    ToolSpec(
        name="get_practice_info",
        description=(
            "Look up an exact clinic policy: hours, location, contact, insurance accepted, "
            "prices, cancellation, pediatric care. Call without a topic to list topics. Always "
            "relay the returned answer; never invent prices or coverage."
        ),
        args_model=PracticeInfoArgs,
        handler=_tool_get_practice_info,
    ),
    ToolSpec(
        name="get_practice_summary",
        description=(
            "Get clinic address, hours, dentists, appointment types and insurance at once."
        ),
        args_model=NoArguments,
        handler=_tool_get_practice_summary,
    ),
)

def _all_specs() -> tuple[ToolSpec, ...]:
    """Phase 2 specs plus Phase 3 scheduling specs.

    Imported lazily because ``scheduling_tools`` imports names from this module;
    a top-level import would be circular.
    """
    from app.agent.scheduling_tools import SCHEDULING_TOOL_SPECS

    existing = {spec.name for spec in TOOL_SPECS}
    return (*TOOL_SPECS, *(spec for spec in SCHEDULING_TOOL_SPECS if spec.name not in existing))


# Keep the base registry importable by ``scheduling_tools``.  Resolving the
# combined registry here used to import that module while it was itself still
# importing this one, which made focused tests and some process start orders
# fail with a partially-initialised-module error.
ALL_TOOL_SPECS: tuple[ToolSpec, ...] = TOOL_SPECS


def _tool_by_name(name: str) -> ToolSpec | None:
    return next((spec for spec in _all_specs() if spec.name == name), None)


def openai_tool_schemas() -> list[dict[str, Any]]:
    return [spec.schema() for spec in _all_specs()]


def _truncate(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, default=str)
    if len(encoded) <= MAX_TOOL_RESULT_CHARS:
        return payload
    return {
        "ok": payload.get("ok", True),
        "truncated": True,
        "note": "Result trimmed for size. Ask a narrower question if you need more.",
    }


def execute_tool(ctx: ToolContext, name: str, raw_arguments: str) -> dict[str, Any]:
    """Validate and run one tool call. Never raises: failures become tool results."""
    spec = _tool_by_name(name)
    if spec is None:
        logger.warning("Model requested unknown tool %s", name)
        return _error("unknown_tool", f"I don't have a tool called {name}.")

    try:
        arguments = json.loads(raw_arguments) if raw_arguments and raw_arguments.strip() else {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except (ValueError, TypeError):
        return _error("bad_arguments", "I couldn't read those details. Could you restate them?")

    try:
        parsed = spec.args_model.model_validate(arguments)
    except ValidationError as exc:
        return _error(
            "invalid_arguments",
            "Those details didn't validate. Ask the patient again for the relevant field.",
            field_errors={
                str(error["loc"][0]): error["msg"] for error in exc.errors() if error.get("loc")
            },
        )

    try:
        # Every tool takes the request context plus its validated arguments, so
        # the call shape is uniform and the model can never reach a handler that
        # lacks session state.
        result = spec.handler(ctx, parsed)
    except Exception:  # noqa: BLE001 - the loop must survive any tool bug
        logger.exception("Tool %s raised an unexpected error", name)
        return _error("tool_failed", "Something went wrong on my end with that step.")

    if spec.name in {"verify_existing_patient", "register_patient"}:
        ctx.session.flush()
    return _truncate(result)
