"""Phase 3 tools: availability, proposals, appointments, family, emergencies.

Split from ``tools.py`` so registration/identity tools stay easy to audit, and so
the scheduling surface can grow without making that file unwieldy.

Every handler follows the same discipline:

1. resolve the patient from ``ToolContext`` (the verified session), never from
   model arguments;
2. call a deterministic service;
3. return a dict whose ``cards`` describe exactly what is now persisted.

The model never receives a way to write an appointment directly. The only paths
that create or modify bookings are ``confirm_booking_proposal`` and
``confirm_appointment_change``, both of which run SQLite transactions after an
explicit confirmation.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field
from sqlalchemy import select

from app.agent.tools import (
    NoArguments,
    ToolArguments,
    ToolContext,
    ToolSpec,
    _error,
    _get_verified_patient,
)
from app.models import AppointmentType, FamilyMember, Patient, RequestType
from app.services import (
    appointments,
    booking_policy,
    emergency,
    patients,
    scheduling,
    staff_handoff,
)
from app.services.availability_engine import resolve_appointment_type
from app.services.proposals import (
    find_by_public_id,
    proposal_intent,
)
from app.services.scheduling import FamilyMemberInput, Requester

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Argument models
# --------------------------------------------------------------------------- #


class SearchAvailabilityArgs(ToolArguments):
    appointment_type: str = Field(
        default="checkup",
        description="'cleaning', 'checkup', or 'emergency'.",
    )
    date_phrases: list[str] = Field(
        default_factory=list,
        description=(
            "Dates exactly as the patient said them, one entry each: 'tomorrow', "
            "'next tuesday', 'this coming friday', 'early next month', '2026-11-12'. "
            "Do not calculate calendar dates yourself."
        ),
    )
    start_date: str | None = Field(
        default=None,
        description="Only for an unambiguous ISO date (YYYY-MM-DD) the patient gave explicitly.",
    )
    time_windows: list[str] = Field(
        default_factory=list,
        description="e.g. 'morning only', 'after 3 PM', 'before lunch', 'late afternoon'.",
    )
    weekdays: list[str] = Field(
        default_factory=list, description="Only these weekdays, e.g. ['saturday']."
    )
    exclude_weekdays: list[str] = Field(
        default_factory=list,
        description="Weekdays to avoid, e.g. ['wednesday'] for 'any day except Wednesday'.",
    )
    range_days: int | None = Field(
        default=None, ge=1, le=120, description="How many days to scan when no date is named."
    )
    provider_name: str | None = Field(
        default=None, description="Requested dentist, e.g. 'Varga' or 'Dr. Lin'."
    )
    group_first_name: str | None = Field(
        default=None,
        description=(
            "Set ONLY when the patient asked for a household booking and named a child on the "
            "account ('my son Leo and me'). The result is then one card per complete same-day "
            "plan, ranked by how close the visits are, with a time for each person, because one "
            "time cannot book two people. Leave it empty for a solo booking."
        ),
    )
    additional_visit: bool = Field(
        default=False,
        description=(
            "True only when the patient explicitly asked for a separate ADDITIONAL visit "
            "('also', 'another one', 'a second visit'). A second visit for someone who already "
            "has one in this booking is otherwise refused."
        ),
    )


class SlotIdsArgs(ToolArguments):
    slot_ids: list[str] = Field(
        min_length=1,
        max_length=6,
        description="Stable slot ids returned by search_availability, not human descriptions.",
    )
    intent: str | None = Field(
        default=None,
        description=(
            "'reschedule' when these ids are replacement times for an existing appointment; "
            "omit (or 'new_booking') for an additional visit."
        ),
    )
    appointment_id: int | None = Field(
        default=None,
        description=(
            "With intent='reschedule': the appointment_id being moved, from "
            "get_patient_appointments. Omit only when exactly one move is pending."
        ),
    )


class CreateProposalArgs(SlotIdsArgs):
    appointment_type: str = Field(default="checkup", description="Visit type for these slots.")
    additional_visit: bool = Field(
        default=False,
        description=(
            "True only when the patient explicitly asked for an ADDITIONAL visit (also, "
            "another, a second one) while a move is waiting. Never set it for a move."
        ),
    )
    additional_visit_reason: str | None = Field(
        default=None,
        description="The patient's own words asking for the extra visit.",
    )
    patient_first_name: str | None = Field(
        default=None,
        description="Only for a different household member already authorised in this session.",
    )
    summary: str | None = Field(default=None, description="One-line reason shown on the card.")
    create_key: str | None = Field(
        default=None, description="Stable key for this proposal; reuse it to retry safely."
    )


class ConfirmSlotSelectionArgs(ToolArguments):
    """A clicked replacement slot: the card itself carries the context."""

    slot_id: str = Field(description="slot_id from the clicked card.")
    intent: str | None = Field(
        default=None,
        description="The card's intent field when present, e.g. 'reschedule'.",
    )
    appointment_id: int | None = Field(
        default=None,
        description="The card's target_appointment_id when present.",
    )
    conversation_id: str | None = Field(
        default=None, description="Only for the UI: which thread the click belongs to."
    )
    appointment_type: str | None = Field(
        default=None,
        description="The card's appointment_type_code; omit to keep the visit's current type.",
    )


class ProposalIdArgs(ToolArguments):
    proposal_id: str = Field(description="Proposal id from a proposal card, e.g. 'prop_...'.")
    confirm_key: str | None = Field(
        default=None, description="Stable key so a repeated confirmation does not book twice."
    )


class DeclineProposalArgs(ToolArguments):
    proposal_id: str = Field(description="Proposal id to release.")
    reason: str | None = Field(default=None, description="Why, if the patient said.")


class ListAppointmentsArgs(ToolArguments):
    include_past: bool = Field(default=False, description="Also include past visits.")


class AppointmentTargetArgs(ToolArguments):
    appointment_id: int | None = Field(
        default=None,
        description=(
            "Numeric id from get_patient_appointments. Always pass it when the listing gave "
            "one; omit only when exactly one upcoming visit exists."
        ),
    )
    which: str | None = Field(
        default=None,
        description=(
            "Natural selector when the id is unknown: 'next', a date '2026-11-03', or a weekday."
        ),
    )


class RescheduleArgs(AppointmentTargetArgs):
    slot_id: str | None = Field(
        default=None,
        description=(
            "Stable id of the replacement time. When a move is already pending, searching "
            "again returns slot cards whose intent is 'reschedule'; any of those ids works."
        ),
    )
    appointment_type: str | None = Field(
        default=None,
        description=(
            "Visit type of the new time. Omit to keep the appointment's current type, which "
            "also keeps its duration."
        ),
    )
    date_phrases: list[str] = Field(
        default_factory=list,
        description=(
            "Only when the patient named a replacement time without a card id: their date "
            "words exactly ('october 29', 'thursday'). The visit's own type and duration are "
            "kept, and the matching slots are returned as replacement cards."
        ),
    )
    time_windows: list[str] = Field(
        default_factory=list,
        description="With date_phrases: 'afternoon', 'after 3 PM', etc.",
    )


class CancelArgs(AppointmentTargetArgs):
    reason: str | None = Field(default=None, description="Patient's stated reason.")


class FamilyMemberArgs(ToolArguments):
    patient_first_name: str = Field(description="First name of the family member.")
    appointment_type: str = Field(
        default="checkup", description="'cleaning', 'checkup', 'emergency'."
    )
    provider_name: str | None = Field(
        default=None, description="Preferred dentist for this person."
    )


class FamilyAvailabilityArgs(ToolArguments):
    members: list[FamilyMemberArgs] = Field(
        min_length=1,
        max_length=4,
        description=(
            "Runtime family intent: include the verified patient and every requested child, "
            "with each person's appointment type."
        ),
    )
    date_phrases: list[str] = Field(default_factory=list)
    start_date: str | None = None
    time_windows: list[str] = Field(default_factory=list)
    range_days: int | None = Field(default=None, ge=1, le=120)
    back_to_back: bool = Field(
        default=False,
        description="Legacy compatibility flag; family options may include short gaps.",
    )


class FamilyOptionArgs(ToolArguments):
    members: list[FamilyMemberArgs] = Field(min_length=1, max_length=4)
    option_index: int = Field(ge=0, le=2, description="Which family option (0-based) to hold.")
    option_identity: str | None = Field(
        default=None,
        description=(
            "The clicked family card's option_identity, passed back exactly. Use it instead of "
            "option_index when the patient clicked a card."
        ),
    )
    date_phrases: list[str] = Field(default_factory=list)
    start_date: str | None = None
    time_windows: list[str] = Field(default_factory=list)
    range_days: int | None = Field(default=None, ge=1, le=120)
    back_to_back: bool = False
    summary: str | None = None


class BookingContextArgs(ToolArguments):
    include_past: bool = Field(
        default=False,
        description="Also list visits already completed or cancelled.",
    )


class EmergencyArgs(ToolArguments):
    description: str | None = Field(
        default=None,
        description="What the patient described, in their own words (short).",
    )
    symptoms: list[str] = Field(
        default_factory=list, description="Symptom labels if already clear."
    )
    wants_urgent_visit: bool = Field(
        default=True, description="Whether they asked to be seen urgently."
    )
    is_update: bool = Field(
        default=False, description="True when this is a follow-up on an existing report."
    )
    caller_name: str | None = Field(
        default=None,
        description=(
            "Name the caller gives for a call back. Required for a non-life-threatening "
            "report when no patient is verified yet; omit once the session already has it."
        ),
    )
    caller_phone: str | None = Field(
        default=None,
        description=(
            "US phone number the caller gives for a call back. Required with caller_name "
            "for an unverified caller; omit once the session already has it."
        ),
    )


class StaffRequestArgs(ToolArguments):
    topic: str = Field(
        description=(
            "What the patient needs help with: 'insurance', 'billing', 'financing', "
            "'membership', 'clinical_question', or 'callback'."
        )
    )
    details: str = Field(description="What the patient asked, in their words.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _requester(ctx: ToolContext) -> tuple[Requester | None, dict[str, Any] | None]:
    patient, problem = _get_verified_patient(ctx)
    if problem is not None or patient is None:
        return None, problem or _error("not_verified", "This session isn't verified.")
    from app.models import Conversation

    conversation = ctx.session.get(Conversation, ctx.conversation_id)
    if conversation is None:  # pragma: no cover - defensive
        return None, _error("unknown_conversation", "I lost track of this conversation.")
    return (
        Requester(
            patient=patient,
            patient_session=ctx.patient_session,
            conversation=conversation,
        ),
        None,
    )


def _appointment_type(
    ctx: ToolContext, code: str | None
) -> tuple[AppointmentType | None, dict[str, Any] | None]:
    appointment_type, _ = resolve_appointment_type(ctx.session, code)
    if appointment_type is None:
        return None, _error(
            "unknown_appointment_type",
            "I can look up cleanings, checkups, or emergency visits.",
        )
    return appointment_type, None


def _registration_gate(ctx: ToolContext, patient: Patient) -> dict[str, Any] | None:
    from app.services import patients as patients_service

    missing = [
        name
        for name, ok in patients_service.registration_completeness(patient).items()
        if not ok
    ]
    if missing:
        return _error(
            "registration_incomplete",
            "Registration needs completing before scheduling. Ask for: "
            + ", ".join(missing)
            + ".",
            missing_fields=missing,
        )
    return None


def _family_member_patients(
    ctx: ToolContext, requester: Requester, members: list[FamilyMemberArgs]
) -> tuple[list[FamilyMemberInput] | None, dict[str, Any] | None]:
    allowed, _ = scheduling.check_family_authorisation(ctx.session, requester)
    by_first_name: dict[str, Patient] = {
        patient.full_name.split(" ")[0].lower(): patient for patient in allowed
    }
    by_first_name.setdefault(requester.patient.full_name.split(" ")[0].lower(), requester.patient)

    resolved: list[FamilyMemberInput] = []
    for member in members:
        key = member.patient_first_name.strip().lower()
        patient = by_first_name.get(key)
        if patient is None:
            return None, _error(
                "unknown_family_member",
                (
                    f"I don't have a {member.patient_first_name} linked to this account. "
                    "I can only book for you and the children on your file."
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


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #


def _tool_search_availability(ctx: ToolContext, args: SearchAvailabilityArgs) -> dict[str, Any]:
    patient, problem = _get_verified_patient(ctx)
    if problem is not None or patient is None:
        return problem or _error("not_verified", "This session isn't verified.")
    gate = _registration_gate(ctx, patient)
    if gate is not None:
        return gate

    requester, requester_problem = _requester(ctx)
    if requester_problem is not None or requester is None:
        return requester_problem or _error("not_verified", "This session isn't verified.")

    # When a move is already pending, the times being searched for are
    # replacement times: the cards must say so, so a click continues the move
    # instead of starting a second booking. The visit's own type is kept.
    target_id = scheduling.pending_target(ctx.session, requester)
    if target_id is not None:
        return scheduling.find_replacement_availability(
            ctx.session,
            requester=requester,
            target_id=target_id,
            date_phrases=args.date_phrases,
            start_date=args.start_date,
            time_windows=args.time_windows,
            weekdays=args.weekdays,
            exclude_weekdays=args.exclude_weekdays,
            range_days=args.range_days,
            provider_name=args.provider_name,
        )

    answer = scheduling.find_availability(
        ctx.session,
        patient=patient,
        appointment_type_code=args.appointment_type,
        date_phrases=args.date_phrases,
        start_date=args.start_date,
        time_windows=args.time_windows,
        weekdays=args.weekdays,
        exclude_weekdays=args.exclude_weekdays,
        range_days=args.range_days,
        provider_name=args.provider_name,
        requester=requester,
        group_first_name=args.group_first_name,
        requests_additional=args.additional_visit,
    )
    payload = dict(answer.payload)
    payload.setdefault("patient_first_name", patients.first_name(patient.full_name))
    payload.setdefault("intent", "new_booking")
    return payload


def _tool_create_proposal(ctx: ToolContext, args: CreateProposalArgs) -> dict[str, Any]:
    """Hold slots for a *new* appointment. Never silently a reschedule.

    A reschedule must go through ``reschedule_appointment``: a booking proposal
    has no original to retire, so confirming it would leave the patient with two
    active appointments. The routing is decided from *stored state and explicit
    arguments*, never from wording:

    * the model says ``intent="reschedule"`` / passes an ``appointment_id``: the
      call is a move, and is routed to the move path (never a booking);
    * a reschedule is already pending for this conversation: the call is refused
      with ``reschedule_in_progress`` pointing at that move, so it can neither
      replace the pending proposal nor hold a second one on top of it;
    * the only way past a pending move is an explicit additional-visit flag
      *and* a description, which books an ADDITIONAL visit and touches nothing
      about the move or the existing appointment.
    """
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")

    if args.intent == "reschedule" or args.appointment_id is not None:
        if not args.slot_ids:
            return _error(
                "missing_slot",
                "Which replacement time should I hold for the move?",
                kind="reschedule",
                intent="reschedule",
                appointment_unchanged=True,
            )
        if len(args.slot_ids) > 1:
            return _error(
                "too_many_slots",
                "A move replaces one appointment at a time. Which of those times do you want?",
                kind="reschedule",
                intent="reschedule",
                appointment_unchanged=True,
            )
        result = scheduling.replacement_for_slot(
            ctx.session,
            requester=requester,
            target_id=args.appointment_id,
            slot_id=args.slot_ids[0],
            appointment_type_code=args.appointment_type,
        )
        if result.get("ok"):
            ctx.session.commit()
        return result

    appointment_type, type_problem = _appointment_type(ctx, args.appointment_type)
    if type_problem is not None or appointment_type is None:
        return type_problem or _error("unknown_type", "Unknown visit type.")

    pending_move = _pending_reschedule_summary(ctx, requester)
    if pending_move is not None:
        holds_the_move = pending_move.get("slot_id") in set(args.slot_ids)
        if not (
            args.additional_visit
            and args.additional_visit_reason
            and args.additional_visit_reason.strip()
        ):
            return _error(
                "reschedule_in_progress",
                (
                    "You're partway through moving an appointment to the time you picked. Finish "
                    "that move, or tell me you'd rather book a separate visit instead."
                ),
                kind="reschedule",
                intent="reschedule",
                appointment_unchanged=True,
                proposal_id=pending_move["proposal_id"],
                appointment_id=pending_move["appointment_id"],
                confirm_with="confirm_appointment_change",
                next_step=(
                    "The patient picked a replacement time for an existing visit. Ask them to "
                    "confirm it with confirm_appointment_change. Only when they explicitly ask "
                    "for another visit as well, retry with additional_visit=true and their "
                    "reason; that books an additional visit and never changes the current one "
                    "or the pending move."
                ),
                same_slot_as_pending=holds_the_move,
            )

    target_id = requester.patient.id
    first_name = patients.first_name(requester.patient.full_name)
    if args.patient_first_name:
        wanted = args.patient_first_name.strip().lower()
        if wanted != first_name.lower():
            allowed, _ = scheduling.check_family_authorisation(ctx.session, requester)
            match = next(
                (
                    patient
                    for patient in allowed
                    if patient.full_name.split(" ")[0].lower() == wanted
                ),
                None,
            )
            if match is None:
                return _error(
                    "not_authorized",
                    (
                        "I can only book for you and the children on your account. "
                        "Another adult needs to contact us themselves."
                    ),
                )
            target_id = match.id
            first_name = patients.first_name(match.full_name)

    options = scheduling.options_for_slots(
        ctx.session,
        appointment_type=appointment_type,
        slot_ids=args.slot_ids,
        patient_id=target_id,
        patient_first_name=first_name,
    )
    if not options:
        return _error(
            "slot_unavailable",
            "That time is no longer open. I'll show current options.",
        )
    return scheduling.propose_booking(
        ctx.session,
        requester=requester,
        appointment_type=appointment_type,
        options=options,
        summary=args.summary,
        create_key=args.create_key,
    )


# --------------------------------------------------------------------------- #
# Proposals
# --------------------------------------------------------------------------- #


def _tool_confirm_booking_proposal(ctx: ToolContext, args: ProposalIdArgs) -> dict[str, Any]:
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")
    # A reschedule or cancellation card must be confirmed through
    # confirm_appointment_change, which knows the original to retire.
    # scheduling.confirm_booking refuses one; this names the right tool.
    # scheduling.confirm runs its own write transaction (and the commit trailer),
    # so the request session must not wrap it in a second one.
    result = scheduling.confirm_booking(
        ctx.session,
        requester=requester,
        proposal_id=args.proposal_id,
        confirm_key=args.confirm_key,
        actor="patient",
    )
    if not result.get("ok") and result.get("error") == "wrong_proposal_kind":
        result["confirm_with"] = "confirm_appointment_change"
    return result


def _tool_decline_booking_proposal(ctx: ToolContext, args: DeclineProposalArgs) -> dict[str, Any]:
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")
    return scheduling.decline(
        ctx.session, requester=requester, proposal_id=args.proposal_id, reason=args.reason
    )


# --------------------------------------------------------------------------- #
# Existing appointments
# --------------------------------------------------------------------------- #


def _tool_get_patient_appointments(ctx: ToolContext, args: ListAppointmentsArgs) -> dict[str, Any]:
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")

    views = appointments.list_for_patient(ctx.session, requester.patient.id)
    upcoming = [view for view in views if view.is_upcoming]
    shown = views if args.include_past else upcoming
    pending = _pending_proposal_summary(ctx, requester)
    move = next((entry for entry in pending if entry["kind"] == "reschedule"), None)
    payload: dict[str, Any] = {
        "ok": True,
        "first_name": patients.first_name(requester.patient.full_name),
        # Count of *active* visits: a retired original is history, not a second
        # appointment, so after a move this is exactly one.
        "upcoming_count": len(upcoming),
        "appointments": [view.as_dict() for view in shown],
        "pending_proposals": pending,
        "pending_reschedule": move,
        "cards": [scheduling.appointment_cards(shown)] if shown else [],
        "note": (
            "These are the only visits on the books. Never imply another appointment exists. "
            "To move one, call reschedule_appointment with its appointment_id; that is not a "
            "new booking. If a reschedule or cancellation proposal is pending, confirm it with "
            "confirm_appointment_change."
            if shown
            else "No upcoming appointments are scheduled."
        ),
    }
    if move is not None:
        payload["note"] = (
            "A move is waiting for confirmation. If the patient picks another time, search for "
            "replacement times (find_replacement_times) and finish the same move; do not book an "
            "additional visit. To finish, call confirm_appointment_change."
        )
    return payload


def _resolve_target(
    ctx: ToolContext, requester: Requester, args: AppointmentTargetArgs
) -> tuple[appointments.AppointmentView | None, dict[str, Any] | None]:
    """The existing visit a cancellation is about, proved to be this patient's."""
    try:
        return (
            appointments.resolve_reschedule_target(
                ctx.session,
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
                    appointments.upcoming_for_patient(ctx.session, requester.patient.id)
                )
            ]
        return None, _error(exc.code, exc.message, **extra)


def _tool_reschedule_appointment(ctx: ToolContext, args: RescheduleArgs) -> dict[str, Any]:
    """Propose moving one *existing* appointment to a new slot. Books nothing.

    This is the only tool that can express a reschedule. It never creates a
    booking proposal, so the confirmation that follows can only be a move: the
    original is retired and the replacement booked in one transaction. The
    appointment is proved to belong to the verified patient before anything is
    held, and its own type/duration is kept unless the patient asked otherwise.

    Two ways in, both ending in a reschedule proposal:

    * a ``slot_id`` from a rendered replacement card (the normal path);
    * a date/time hint without an id ("October 29 afternoon") -- the visit is
      resolved and verified here first, and the matching times come back as
      replacement cards. No slot id is guessed, and nothing is held until the
      patient picks one.
    """
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")

    if not args.slot_id:
        try:
            view = appointments.resolve_reschedule_target(
                ctx.session,
                patient_id=requester.patient.id,
                appointment_id=args.appointment_id,
                which=args.which,
            )
        except appointments.RescheduleTargetError as exc:
            return _error(
                exc.code,
                exc.message,
                kind="reschedule",
                intent="reschedule",
                appointment_unchanged=True,
                **exc.extra,
            )
        if not (args.date_phrases or args.time_windows):
            return _error(
                "missing_slot",
                "Which new time should I hold for the move?",
                kind="reschedule",
                intent="reschedule",
                appointment_unchanged=True,
                appointment_id=view.appointment.id,
                next_step=(
                    "Search with find_replacement_times (or pass date_phrases/time_windows "
                    "here) and let the patient pick one of the replacement cards."
                ),
            )
        return scheduling.find_replacement_availability(
            ctx.session,
            requester=requester,
            target_id=view.appointment.id,
            date_phrases=args.date_phrases,
            time_windows=args.time_windows,
        )

    # A slot identifies the destination, not the appointment being moved. If
    # the model omitted an appointment id, resolve the patient's sole matching
    # upcoming visit exactly as the search-only path above does. Passing None
    # here used to turn the normal "move me to this time" call into
    # no_reschedule_target before a proposal could be created.
    target_id = args.appointment_id
    if target_id is None:
        try:
            target_id = appointments.resolve_reschedule_target(
                ctx.session,
                patient_id=requester.patient.id,
                which=args.which,
            ).appointment.id
        except appointments.RescheduleTargetError as exc:
            # A patient may book an additional visit in this thread and then
            # continue a request to move the one they already had. When exactly
            # one upcoming visit predates this conversation, that is the only
            # unambiguous existing target. If zero or several qualify, keep the
            # normal multiple-appointments clarification.
            prior = [
                view
                for view in appointments.upcoming_for_patient(
                    ctx.session, requester.patient.id
                )
                if view.appointment.conversation_id != requester.conversation_id
            ]
            if exc.code == "multiple_appointments" and len(prior) == 1:
                target_id = prior[0].appointment.id
            else:
                details = dict(exc.extra)
                details.setdefault("appointment_unchanged", True)
                return _error(
                    exc.code,
                    exc.message,
                    kind="reschedule",
                    intent="reschedule",
                    **details,
                )

    result = scheduling.replacement_for_slot(
        ctx.session,
        requester=requester,
        target_id=target_id,
        slot_id=args.slot_id,
        appointment_type_code=args.appointment_type,
        which=args.which,
    )
    if not result.get("ok"):
        return result
    ctx.session.commit()
    return result


def _tool_cancel_appointment(ctx: ToolContext, args: CancelArgs) -> dict[str, Any]:
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")
    view, target_problem = _resolve_target(ctx, requester, args)
    if target_problem is not None or view is None:
        return target_problem or _error("unknown_appointment", "I can't find that appointment.")

    outcome = appointments.propose_cancellation(
        ctx.session,
        conversation_id=requester.conversation_id,
        patient_id=requester.patient.id,
        view=view,
        reason=args.reason,
    )
    if not outcome.ok:
        return _error(
            outcome.error or "cancel_failed",
            outcome.message or "I couldn't set up the cancellation.",
            appointment_unchanged=True,
        )
    ctx.session.commit()

    proposal = _load_proposal(ctx, outcome.proposal_id)
    return {
        "ok": True,
        "awaiting_confirmation": True,
        "appointment_unchanged": True,
        "proposal_id": outcome.proposal_id,
        "message": (
            "Nothing is cancelled yet. Confirm below and I'll release the appointment."
        ),
        "cards": [scheduling.proposal_card(proposal, include_actions=True)] if proposal else [],
    }


def _tool_confirm_appointment_change(ctx: ToolContext, args: ProposalIdArgs) -> dict[str, Any]:
    """Confirm a reschedule or cancellation. Same atomic path as a booking."""
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")
    # Ownership and kind are re-checked in the service: a proposal id from
    # another conversation is indistinguishable from one that does not exist, and
    # a new-booking card must be confirmed with confirm_booking_proposal.
    return scheduling.confirm_change(
        ctx.session,
        requester=requester,
        proposal_id=args.proposal_id,
        confirm_key=args.confirm_key,
        actor="patient",
    )


def _tool_find_replacement_times(ctx: ToolContext, args: SearchAvailabilityArgs) -> dict[str, Any]:
    """Times a *pending move* could take, without leaving the reschedule flow.

    Unlike search_availability this does not depend on getting the visit type
    right: it reads the pending move's own appointment, so the replacement keeps
    the visit's duration. When it returns ``no_reschedule_target`` the patient is
    not moving anything and ordinary search is the right tool.
    """
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")
    gate = _registration_gate(ctx, requester.patient)
    if gate is not None:
        return gate

    result = scheduling.find_replacement_availability(
        ctx.session,
        requester=requester,
        target_id=scheduling.pending_target(ctx.session, requester),
        date_phrases=args.date_phrases,
        start_date=args.start_date,
        time_windows=args.time_windows,
        weekdays=args.weekdays,
        exclude_weekdays=args.exclude_weekdays,
        range_days=args.range_days,
        provider_name=args.provider_name,
    )
    if not result.get("ok") and result.get("error") == "no_active_booking":
        result["next_step"] = (
            "There is no visit to move. Use search_availability to book a new one."
        )
    return result


def _tool_confirm_slot_selection(
    ctx: ToolContext, args: ConfirmSlotSelectionArgs
) -> dict[str, Any]:
    """A replacement slot was clicked (or replied as a time). Continue the move.

    This is the deterministic continuation the patient's click triggers. It
    decides from stored state, not from wording:

    * the card carries ``intent="reschedule"`` and an appointment id -> hold that
      replacement for that appointment;
    * only ``intent="reschedule"`` -> hold it for the pending move;
    * a plain new-booking card, but a move is pending and the visit itself is
      not being moved -> keep the move's context instead of starting a second
      booking (the card is stale; the patient is clearly still moving a visit);
    * otherwise -> hold a new visit.

    Either way one proposal exists afterwards, and a reschedule proposal can only
    be confirmed by the atomic change confirmation.
    """
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")
    gate = _registration_gate(ctx, requester.patient)
    if gate is not None:
        return gate

    wants_move = args.intent == "reschedule" or args.appointment_id is not None
    if not wants_move:
        # A stale new-booking card clicked while a move is pending: the safest
        # reading is "continue the move", never "book a second appointment".
        wants_move = scheduling.pending_target(ctx.session, requester) is not None
    if wants_move:
        result = scheduling.replacement_for_slot(
            ctx.session,
            requester=requester,
            target_id=args.appointment_id,
            slot_id=args.slot_id,
            appointment_type_code=args.appointment_type,
            from_ui=True,
        )
        ctx.session.commit()
        return result

    appointment_type, type_problem = _appointment_type(ctx, args.appointment_type or "checkup")
    if type_problem is not None or appointment_type is None:
        return type_problem or _error("unknown_type", "Unknown visit type.")
    options = scheduling.options_for_slots(
        ctx.session,
        appointment_type=appointment_type,
        slot_ids=[args.slot_id],
        patient_id=requester.patient.id,
        patient_first_name=patients.first_name(requester.patient.full_name),
    )
    if not options:
        return _error(
            "slot_unavailable",
            "That time is no longer open. I'll show current options.",
            intent="new_booking",
        )
    result = scheduling.propose_booking(
        ctx.session,
        requester=requester,
        appointment_type=appointment_type,
        options=options,
        from_ui=True,
    )
    ctx.session.commit()
    return result


def _pending_reschedule_summary(ctx: ToolContext, requester: Requester) -> dict[str, Any] | None:
    """The pending move, if any: which visit, which replacement, how to finish."""
    for entry in _pending_proposal_summary(ctx, requester):
        if entry["kind"] == "reschedule":
            return entry
    return None


def _pending_proposal_summary(ctx: ToolContext, requester: Requester) -> list[dict[str, Any]]:
    """Still-pending proposals, so the model confirms the one the patient means."""
    from app.services.proposals import pending_for_conversation

    summaries: list[dict[str, Any]] = []
    for proposal in pending_for_conversation(ctx.session, requester.conversation_id):
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


def _load_proposal(ctx: ToolContext, public_id: str | None):
    if not public_id:
        return None
    return find_by_public_id(ctx.session, public_id)


# --------------------------------------------------------------------------- #
# Family
# --------------------------------------------------------------------------- #


def _tool_search_family_availability(
    ctx: ToolContext, args: FamilyAvailabilityArgs
) -> dict[str, Any]:
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")
    members, member_problem = _family_member_patients(ctx, requester, args.members)
    if member_problem is not None or members is None:
        return member_problem or _error("unknown_family_member", "I couldn't match those names.")

    return scheduling.family_search(
        ctx.session,
        requester=requester,
        members=members,
        date_phrases=args.date_phrases,
        start_date=args.start_date,
        time_windows=args.time_windows,
        range_days=args.range_days,
        back_to_back=args.back_to_back,
    )


def _tool_hold_family_option(ctx: ToolContext, args: FamilyOptionArgs) -> dict[str, Any]:
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")
    members, member_problem = _family_member_patients(ctx, requester, args.members)
    if member_problem is not None or members is None:
        return member_problem or _error("unknown_family_member", "I couldn't match those names.")

    # One proposal containing every member's appointment, so confirmation is a
    # single transaction: the whole family is booked, or nobody is. Holding is
    # refused while another booking is pending or already booked.
    return scheduling.hold_family_option(
        ctx.session,
        requester=requester,
        members=members,
        option_index=args.option_index,
        option_identity=args.option_identity,
        date_phrases=args.date_phrases,
        start_date=args.start_date,
        time_windows=args.time_windows,
        range_days=args.range_days,
        summary=args.summary,
    )


def _tool_get_booking_context(ctx: ToolContext, args: BookingContextArgs) -> dict[str, Any]:
    """What this booking already has, and how many times it needs.

    This is the single place the model learns "one person, one visit" versus
    "two people, two back-to-back visits", plus whether a hold or a booking is
    already in force. Calling it before offering times keeps the prose (and the
    choice of tool) aligned with the cards.
    """
    requester, problem = _requester(ctx)
    if problem is not None or requester is None:
        return problem or _error("not_verified", "This session isn't verified.")
    del args

    allowed, _ = scheduling.check_family_authorisation(ctx.session, requester)
    patients = [requester.patient, *allowed]
    scope = booking_policy.booking_scope(
        ctx.session, patients=patients, conversation_id=requester.conversation_id
    )
    families = _household_summary(ctx, requester, allowed)
    payload = {
        "ok": True,
        **scope.summary(),
        "household": families,
        "rule": (
            "One booking covers one visit per person. A solo request needs exactly one time; "
            "a household request needs one time for each person, held as a single family option. "
            "Never hold a second booking for someone who already has one in this thread unless "
            "they explicitly ask for an additional visit."
        ),
        "cards": [],
    }
    if scope.has_booked_visit:
        payload["next_step"] = (
            "This booking is already done. Do not offer or hold another time. Offer to move "
            "the visit (reschedule_appointment) only if they want a different time."
        )
    elif scope.pending:
        payload["next_step"] = (
            "A hold is waiting. Ask the patient to confirm or decline that card rather than "
            "searching again, unless they asked for different times."
        )
    elif len(scope.covered_patient_ids) > 1:
        payload["next_step"] = (
            "This is a household booking. Use search_family_availability with one member entry "
            "per requested person so the cards show one time per person."
        )
    else:
        payload["next_step"] = (
            "This is a solo booking. search_availability returns single times, and exactly one "
            "of them is held for the patient."
        )
    return payload


def _household_summary(
    ctx: ToolContext, requester: Requester, allowed: list[Patient]
) -> list[dict[str, Any]]:
    """Who may be booked together, derived from the household link only.

    ``bookable_here`` is false for another adult: the household link is not
    consent, so the receptionist never offers them a time in this thread.
    """
    rows = ctx.session.scalars(
        select(FamilyMember).where(FamilyMember.patient_id == requester.patient.id)
    ).all()
    allowed_ids = {patient.id for patient in allowed}
    by_group: dict[int, list[dict[str, Any]]] = {}
    for membership in rows:
        same_group = ctx.session.scalars(
            select(FamilyMember).where(FamilyMember.family_group_id == membership.family_group_id)
        ).all()
        members: list[dict[str, Any]] = []
        for sibling in same_group:
            other = ctx.session.get(Patient, sibling.patient_id)
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


# --------------------------------------------------------------------------- #
# Emergencies and general inquiries
# --------------------------------------------------------------------------- #


def _tool_report_emergency(ctx: ToolContext, args: EmergencyArgs) -> dict[str, Any]:
    conversation_id = ctx.conversation_id
    patient = ctx.verified_patient

    if args.is_update:
        report = emergency.follow_up(
            ctx.session,
            conversation_id=conversation_id,
            patient_id=patient.id if patient else None,
            description=args.description or "Symptoms reported again.",
            caller_name=args.caller_name,
            caller_phone=args.caller_phone,
            patient_session=ctx.patient_session,
        )
        # The card is read *before* the commit: the alert was added in this
        # transaction, and committing first expires the session, which would
        # re-read row-level state and hand the model a stale concern card.
        cards = _handoff_card_for(ctx)
        ctx.session.commit()
        payload = report.triage.as_dict()
        payload["worsening"] = report.worsening
        payload["message"] = emergency.emergency_summary_line(report.triage)
        payload["cards"] = cards
        return payload

    result = emergency.triage(
        ctx.session,
        conversation_id=conversation_id,
        patient_id=patient.id if patient else None,
        description=args.description,
        symptoms=args.symptoms,
        wants_urgent_visit=args.wants_urgent_visit,
        caller_name=args.caller_name,
        caller_phone=args.caller_phone,
        patient_session=ctx.patient_session,
    )
    # Read before the commit for the same reason as the follow-up path above.
    cards = _handoff_card_for(ctx)
    ctx.session.commit()

    payload = result.as_dict()
    payload["message"] = emergency.emergency_summary_line(result)
    if result.life_threatening:
        payload["do_not_register_first"] = True
        payload["must_say"] = emergency.EMERGENCY_CALL_ADVICE
    elif result.contact_required:
        # Nothing was filed. Say so plainly so the model cannot imply otherwise.
        payload["blocked"] = True
    payload["cards"] = cards
    return payload


def _handoff_card_for(ctx: ToolContext) -> list[dict[str, Any]]:
    entries = staff_handoff.request_status(ctx.session, conversation_id=ctx.conversation_id)
    return [scheduling.handoff_cards(entries)] if entries else []


def _tool_create_staff_request(ctx: ToolContext, args: StaffRequestArgs) -> dict[str, Any]:
    conversation_id = ctx.conversation_id
    patient = ctx.verified_patient
    topic = args.topic.strip().lower()

    request_type = {
        "insurance": RequestType.BILLING,
        "billing": RequestType.BILLING,
        "financing": RequestType.BILLING,
        "membership": RequestType.BILLING,
        "clinical_question": RequestType.CLINICAL_QUESTION,
        "callback": RequestType.CALLBACK,
        "reschedule": RequestType.RESCHEDULE,
    }.get(topic, RequestType.CALLBACK)

    result = staff_handoff.raise_request(
        ctx.session,
        conversation_id=conversation_id,
        patient_id=patient.id if patient else None,
        request_type=request_type,
        details=f"{topic}: {args.details}".strip(": "),
        subject=topic,
    )
    ctx.session.commit()
    payload = result.as_dict()
    payload["message"] = (
        "I've sent this to the front desk. I can't confirm coverage, pricing, or financing "
        "myself, so a team member will follow up with you."
    )
    payload["grounded"] = False
    payload["cards"] = _handoff_card_for(ctx)
    return payload


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

SCHEDULING_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_availability",
        description=(
            "Find genuinely open appointment times. Pass date phrases exactly as the patient "
            "said them ('tomorrow', 'next tuesday', 'early next month', '2026-11-12') and let "
            "the server resolve dates. Results are real slots the patient can click; showing "
            "them books nothing. This tool is for solo availability. For a household booking, "
            "call search_family_availability so the cards show one complete same-day time per "
            "person, ranked as close together as possible."
        ),
        args_model=SearchAvailabilityArgs,
        handler=_tool_search_availability,
    ),
    ToolSpec(
        name="get_booking_context",
        description=(
            "Read what this booking already has and how many times it needs: solo (one person, "
            "one visit) or household (one visit per person), plus any pending hold or completed "
            "booking. Call it before offering times when the patient has asked for more than "
            "one appointment, or after a booking, so you hold exactly the right number."
        ),
        args_model=BookingContextArgs,
        handler=_tool_get_booking_context,
    ),
    ToolSpec(
        name="create_booking_proposal",
        description=(
            "Hold one or more slot ids for a NEW appointment so the patient can confirm. Use "
            "after the patient chooses a time for a visit they do not have yet -- never to move "
            "an existing visit (reschedule_appointment does that), and never when the patient "
            "said move/reschedule/change. A new booking adds a visit: it never cancels or "
            "replaces an existing appointment. While a move is pending this returns "
            "reschedule_in_progress; only retry with additional_visit=true when the patient "
            "explicitly asked for another visit as well. Returns an Awaiting Confirmation card. "
            "Never say an appointment is booked at this point."
        ),
        args_model=CreateProposalArgs,
        handler=_tool_create_proposal,
    ),
    ToolSpec(
        name="confirm_slot_selection",
        description=(
            "A time was picked on a card. Call this instead of create_booking_proposal when the "
            "patient clicked a slot after a time search: it continues whatever the card meant. "
            "For a replacement slot of an existing visit it holds the new time as a reschedule "
            "proposal; for a new-booking card it holds a new visit."
        ),
        args_model=ConfirmSlotSelectionArgs,
        handler=_tool_confirm_slot_selection,
    ),
    ToolSpec(
        name="find_replacement_times",
        description=(
            "When the patient is partway through moving an existing appointment and wants other "
            "times, search here. It keeps the visit's own type and duration and returns slot "
            "cards marked as reschedule replacements. Do not use it to book an additional visit."
        ),
        args_model=SearchAvailabilityArgs,
        handler=_tool_find_replacement_times,
    ),
    ToolSpec(
        name="confirm_booking_proposal",
        description=(
            "Confirm a pending NEW-booking proposal. Revalidates availability in a transaction "
            "and only then books. Use when the patient explicitly agrees in conversation, or "
            "when the UI reports a confirm click. Do not use it for reschedule or cancellation "
            "cards -- those go to confirm_appointment_change."
        ),
        args_model=ProposalIdArgs,
        handler=_tool_confirm_booking_proposal,
    ),
    ToolSpec(
        name="decline_booking_proposal",
        description=(
            "Release a held proposal without booking. Use when the patient says no, or wants "
            "different times."
        ),
        args_model=DeclineProposalArgs,
        handler=_tool_decline_booking_proposal,
    ),
    ToolSpec(
        name="get_patient_appointments",
        description=(
            "List the verified patient's real upcoming appointments. Use before any change to "
            "an appointment, and when asked what is booked."
        ),
        args_model=ListAppointmentsArgs,
        handler=_tool_get_patient_appointments,
    ),
    ToolSpec(
        name="reschedule_appointment",
        description=(
            "MOVE an existing appointment. This is a reschedule, not a new booking: it names "
            "the appointment_id being moved and creates a reschedule proposal that records "
            "intent='reschedule'. The original stays booked until the patient confirms, and "
            "confirming retires it at the same moment the new time is booked. If the patient "
            "says 'move/reschedule/change my appointment to ...', use this -- never "
            "create_booking_proposal. Pass slot_id when the patient picked a replacement card. "
            "When they only named a day/time ('October 29 afternoon'), omit slot_id and pass "
            "date_phrases/time_windows: it resolves the visit first and returns replacement "
            "cards to pick from. If a move is already pending, its appointment is used "
            "automatically, so the patient never has to repeat which visit."
        ),
        args_model=RescheduleArgs,
        handler=_tool_reschedule_appointment,
    ),
    ToolSpec(
        name="cancel_appointment",
        description=(
            "Propose cancelling an existing appointment. Requires explicit confirmation before "
            "anything is released."
        ),
        args_model=CancelArgs,
        handler=_tool_cancel_appointment,
    ),
    ToolSpec(
        name="confirm_appointment_change",
        description=(
            "Confirm a pending reschedule or cancellation proposal. Atomic: the replacement is "
            "booked and the original is marked rescheduled/cancelled together, so a failed move "
            "leaves the original appointment untouched. Repeating it replays the first result."
        ),
        args_model=ProposalIdArgs,
        handler=_tool_confirm_appointment_change,
    ),
    ToolSpec(
        name="search_family_availability",
        description=(
            "Find currently available times for several household members (self plus children), "
            "with all visits on the same day and ranked as close together as possible. Short "
            "gaps are allowed; each option has a time for every person, so one confirmation "
            "books everyone. Different visit types and durations are supported."
        ),
        args_model=FamilyAvailabilityArgs,
        handler=_tool_search_family_availability,
    ),
    ToolSpec(
        name="hold_family_option",
        description=(
            "Hold one family option as a single combined proposal. Confirming it books every "
            "member together or none. Pass the clicked card's option_identity when you have it. "
            "Never hold two options at once: the second hold is refused."
        ),
        args_model=FamilyOptionArgs,
        handler=_tool_hold_family_option,
    ),
    ToolSpec(
        name="report_emergency",
        description=(
            "Report dental symptoms or an urgent request. For breathing difficulty or other "
            "life-threatening signs the result tells you to direct the patient to 911 "
            "immediately, before any registration. Repeated reports update one alert."
        ),
        args_model=EmergencyArgs,
        handler=_tool_report_emergency,
    ),
    ToolSpec(
        name="create_staff_request",
        description=(
            "Ask the front desk to follow up on something you cannot verify (insurance "
            "networks, prices, financing, membership). Never invent an answer instead."
        ),
        args_model=StaffRequestArgs,
        handler=_tool_create_staff_request,
    ),
)

SCHEDULING_TOOL_NAMES = frozenset(spec.name for spec in SCHEDULING_TOOL_SPECS)


__all__ = [
    "SCHEDULING_TOOL_NAMES",
    "SCHEDULING_TOOL_SPECS",
    "NoArguments",
]
