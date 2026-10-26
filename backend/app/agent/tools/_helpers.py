"""Helpers shared by more than one tool domain.

Nothing here is a tool: these resolve a requester, a target appointment, or a
pending proposal for the ``@tool`` functions that live in ``domains/``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.agent.context import TurnDeps
from app.agent.tools.results import require_verified, tool_error
from app.models import AppointmentType, FamilyMember, Patient
from app.services import appointments, patients, scheduling, staff_handoff
from app.services.availability_engine import resolve_appointment_type
from app.services.proposals import find_by_public_id, proposal_intent
from app.services.scheduling import FamilyMemberInput, Requester

if TYPE_CHECKING:
    # Annotation-only: the argument models belong to the domain that owns the
    # tool, so importing them at runtime would point the arrow the wrong way.
    from app.agent.tools.domains.appointments import AppointmentTargetArgs
    from app.agent.tools.domains.family import FamilyMemberArgs


def _requester(deps: TurnDeps) -> tuple[Requester | None, dict[str, Any] | None]:
    patient, problem = require_verified(deps)
    if problem is not None or patient is None:
        return None, problem or tool_error("not_verified", "This session isn't verified.")
    requester = deps.requester()
    if requester is None:  # pragma: no cover - defensive
        return None, tool_error("unknown_conversation", "I lost track of this conversation.")
    return requester, None


def _registration_gate(deps: TurnDeps, patient: Patient) -> dict[str, Any] | None:
    missing = [
        name
        for name, ok in patients.registration_completeness(patient).items()
        if not ok
    ]
    if missing:
        return tool_error(
            "registration_incomplete",
            "Registration needs completing before scheduling. Ask for: "
            + ", ".join(missing)
            + ".",
            missing_fields=missing,
        )
    return None


def _resolve_target(
    deps: TurnDeps, requester: Requester, args: AppointmentTargetArgs
) -> tuple[appointments.AppointmentView | None, dict[str, Any] | None]:
    """The existing visit a cancellation is about, proved to be this patient's."""
    try:
        return (
            appointments.resolve_reschedule_target(
                deps.session,
                deps.clinic,
                patient_id=requester.patient.id,
                appointment_id=args.appointment_id,
                which=args.which,
            ),
            None,
        )
    except appointments.RescheduleTargetError as exc:
        extra = dict(exc.extra)
        if exc.code == "multiple_appointments" and extra.get("appointments"):
            extra["cards"] = [
                scheduling.appointment_cards(
                    appointments.upcoming_for_patient(
                        deps.session, deps.clinic, requester.patient.id
                    )
                )
            ]
        return None, tool_error(exc.code, exc.message, **extra)


def _pending_proposal_summary(deps: TurnDeps, requester: Requester) -> list[dict[str, Any]]:
    """Still-pending proposals, so the model confirms the one the patient means."""
    from app.services.proposals import pending_for_conversation

    summaries: list[dict[str, Any]] = []
    for proposal in pending_for_conversation(deps.session, requester.conversation_id):
        first = min(proposal.items, key=lambda item: item.order_index, default=None)
        summaries.append(
            {
                "proposal_id": proposal.public_id,
                "kind": proposal.kind,
                "intent": proposal_intent(proposal),
                "status": proposal.status,
                "appointment_id": proposal.target_appointment_id,
                # The held replacement time: what the patient clicked, and what
                # create_booking_proposal must refuse to re-hold.
                "slot_id": first.stable_slot_id if first is not None else None,
                "starts_at": first.proposed_starts_at.isoformat(timespec="minutes")
                if first is not None and first.proposed_starts_at
                else None,
                "original_starts_at": first.original_starts_at.isoformat(timespec="minutes")
                if first is not None and first.original_starts_at
                else None,
                "confirm_with": (
                    "confirm_appointment_change"
                    if proposal.kind in {"reschedule", "cancellation"}
                    else "confirm_booking_proposal"
                ),
            }
        )
    return summaries


def _pending_reschedule_summary(deps: TurnDeps, requester: Requester) -> dict[str, Any] | None:
    """The pending move, if any: which visit, which replacement, how to finish."""
    for entry in _pending_proposal_summary(deps, requester):
        if entry["kind"] == "reschedule":
            return entry
    return None


def _load_proposal(deps: TurnDeps, public_id: str | None):
    if not public_id:
        return None
    return find_by_public_id(deps.session, public_id)


def _household_summary(
    deps: TurnDeps, requester: Requester, allowed: list[Patient]
) -> list[dict[str, Any]]:
    """Who may be booked together, derived from the household link only.

    Family booking is allowed for every linked household member, including a
    spouse. Their appointment history is still not disclosed without their own
    verification: this list answers "who can I book", not "what do they have".
    """
    rows = deps.session.scalars(
        select(FamilyMember).where(FamilyMember.patient_id == requester.patient.id)
    ).all()
    allowed_ids = {patient.id for patient in allowed}
    by_group: dict[int, list[dict[str, Any]]] = {}
    for membership in rows:
        same_group = deps.session.scalars(
            select(FamilyMember).where(FamilyMember.family_group_id == membership.family_group_id)
        ).all()
        members: list[dict[str, Any]] = []
        for sibling in same_group:
            other = deps.session.get(Patient, sibling.patient_id)
            if other is None:
                continue
            members.append(
                {
                    "first_name": patients.first_name(other.full_name),
                    "is_you": other.id == requester.patient.id,
                    "bookable_here": other.id == requester.patient.id
                    or other.id in allowed_ids,
                }
            )
        by_group[membership.family_group_id] = members
    return [
        {"family_group_id": group_id, "members": members}
        for group_id, members in by_group.items()
    ]


def _family_member_patients(
    deps: TurnDeps, requester: Requester, members: list[FamilyMemberArgs]
) -> tuple[list[FamilyMemberInput] | None, dict[str, Any] | None]:
    allowed, _ = scheduling.check_family_authorisation(deps.session, requester)
    by_first_name: dict[str, Patient] = {
        patient.full_name.split(" ")[0].lower(): patient for patient in allowed
    }
    by_first_name.setdefault(requester.patient.full_name.split(" ")[0].lower(), requester.patient)

    resolved: list[FamilyMemberInput] = []
    for member in members:
        key = member.patient_first_name.strip().lower()
        if key in {
            "both",
            "btoh",  # common one-word typo in a short clarification reply
            "both kids",
            "both children",
            "the kids",
            "all children",
        }:
            # "Both" is an answer to which child, not a request to discard the
            # verified parent or choose only the first matching child.
            for patient in allowed:
                if patient.id not in {item.patient.id for item in resolved}:
                    resolved.append(
                        FamilyMemberInput(
                            patient=patient,
                            appointment_type_code=member.appointment_type,
                            provider_name=member.provider_name,
                        )
                    )
            continue
        patient = by_first_name.get(key)
        if patient is None:
            return None, tool_error(
                "unknown_family_member",
                (
                    f"I don't have a {member.patient_first_name} linked to this account. "
                    "I can only book for you and the other people on your household account."
                ),
                available_first_names=sorted(by_first_name),
            )
        resolved.append(
            FamilyMemberInput(
                patient=patient,
                appointment_type_code=member.appointment_type,
                provider_name=member.provider_name,
            )
        )
    return resolved, None


def _appointment_type(
    deps: TurnDeps, code: str | None
) -> tuple[AppointmentType | None, dict[str, Any] | None]:
    appointment_type, _ = resolve_appointment_type(deps.session, deps.clinic, code)
    if appointment_type is None:
        return None, tool_error(
            "unknown_appointment_type",
            "I can look up cleanings, checkups, or emergency visits.",
        )
    return appointment_type, None


def _create_key(deps: TurnDeps, slot_ids: Sequence[str]) -> str:
    """The idempotency key for a hold, derived by the server and not the model.

    ``create_proposal`` replays any earlier proposal carrying the same key,
    whatever became of it, so a key the model invented could bring a declined
    hold back as if it were still waiting. Scoping the key to this turn keeps a
    retry inside the turn from holding twice and still lets a later request make
    a new one.
    """
    digest = hashlib.sha1("|".join(sorted(slot_ids)).encode()).hexdigest()[:12]
    return f"turn:{deps.turn_key}:slots:{digest}"


def _confirm_key(deps: TurnDeps, proposal_id: str) -> str:
    """The idempotency key for a confirmation, derived the same way.

    A second confirm of one proposal inside one turn replays the first result
    instead of reaching the booking transaction twice.
    """
    return f"turn:{deps.turn_key}:confirm:{proposal_id}"


def _handoff_card_for(deps: TurnDeps) -> list[dict[str, Any]]:
    entries = staff_handoff.request_status(deps.session, conversation_id=deps.conversation_id)
    return [scheduling.handoff_cards(entries)] if entries else []
