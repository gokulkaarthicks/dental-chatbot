"""Appointment tools: what is booked, and how to move or cancel it."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

from app.agent.context import TurnDeps
from app.agent.tools._helpers import (
    _confirm_key,
    _load_proposal,
    _pending_proposal_summary,
    _requester,
    _resolve_target,
)
from app.agent.tools.domains.booking import ProposalIdArgs
from app.agent.tools.results import ToolArguments, tool_error, tool_result
from app.services import appointments, patients, scheduling


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


class ListAppointmentsArgs(ToolArguments):
    include_past: bool = Field(default=False, description="Also include past visits.")


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


@tool(
    "get_patient_appointments",
    args_schema=ListAppointmentsArgs,
    response_format="content_and_artifact",
    description=(
        "List the verified patient's real upcoming appointments. Use before any change to "
        "an appointment, and when asked what is booked."
    ),
)
def get_patient_appointments(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    include_past: bool,
) -> tuple[str, dict[str, Any]]:
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))

    views = appointments.list_for_patient(deps.session, deps.clinic, requester.patient.id)
    upcoming = [view for view in views if view.is_upcoming]
    shown = views if include_past else upcoming
    pending = _pending_proposal_summary(deps, requester)
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
    return tool_result(payload)


@tool(
    "reschedule_appointment",
    args_schema=RescheduleArgs,
    response_format="content_and_artifact",
    description=(
        "MOVE an existing appointment. This is a reschedule, not a new booking: it names "
        "the appointment_id being moved and creates a reschedule proposal that records "
        "intent='reschedule'. The original stays booked until the patient confirms, and "
        "confirming retires it at the same moment the new time is booked. If the patient "
        "says 'move/reschedule/change my appointment to ...', use this, never "
        "create_booking_proposal. Pass slot_id when the patient picked a replacement card. "
        "When they only named a day/time ('October 29 afternoon'), omit slot_id and pass "
        "date_phrases/time_windows: it resolves the visit first and returns replacement "
        "cards to pick from. If a move is already pending, its appointment is used "
        "automatically, so the patient never has to repeat which visit."
    ),
)
def reschedule_appointment(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    appointment_id: int | None,
    which: str | None,
    slot_id: str | None,
    appointment_type: str | None,
    date_phrases: list[str],
    time_windows: list[str],
) -> tuple[str, dict[str, Any]]:
    """Propose moving one *existing* appointment to a new slot. Books nothing.

    This is the only tool that can express a reschedule. It never creates a
    booking proposal, so the confirmation that follows can only be a move: the
    original is retired and the replacement booked in one transaction. The
    appointment is proved to belong to the verified patient before anything is
    held, and its own type/duration is kept unless the patient asked otherwise.

    Two ways in, both ending in a reschedule proposal:

    * a ``slot_id`` from a rendered replacement card (the normal path);
    * a date/time hint without an id ("October 29 afternoon"): the visit is
      resolved and verified here first, and the matching times come back as
      replacement cards. No slot id is guessed, and nothing is held until the
      patient picks one.
    """
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))

    if not slot_id:
        try:
            view = appointments.resolve_reschedule_target(
                deps.session,
                deps.clinic,
                patient_id=requester.patient.id,
                appointment_id=appointment_id,
                which=which,
            )
        except appointments.RescheduleTargetError as exc:
            return tool_result(
                tool_error(
                    exc.code,
                    exc.message,
                    kind="reschedule",
                    intent="reschedule",
                    appointment_unchanged=True,
                    **exc.extra,
                )
            )
        if not (date_phrases or time_windows):
            return tool_result(
                tool_error(
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
            )
        return tool_result(
            scheduling.find_replacement_availability(
                deps.session,
                requester=requester,
                target_id=view.appointment.id,
                date_phrases=date_phrases,
                time_windows=time_windows,
            )
        )

    # A slot identifies the destination, not the appointment being moved. If
    # the model omitted an appointment id, resolve the patient's sole matching
    # upcoming visit exactly as the search-only path above does. Passing None
    # here used to turn the normal "move me to this time" call into
    # no_reschedule_target before a proposal could be created.
    target_id = appointment_id
    if target_id is None:
        try:
            target_id = appointments.resolve_reschedule_target(
                deps.session,
                deps.clinic,
                patient_id=requester.patient.id,
                which=which,
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
                    deps.session, deps.clinic, requester.patient.id
                )
                if view.appointment.conversation_id != requester.conversation_id
            ]
            if exc.code == "multiple_appointments" and len(prior) == 1:
                target_id = prior[0].appointment.id
            else:
                details = dict(exc.extra)
                details.setdefault("appointment_unchanged", True)
                return tool_result(
                    tool_error(
                        exc.code,
                        exc.message,
                        kind="reschedule",
                        intent="reschedule",
                        **details,
                    )
                )

    result = scheduling.replacement_for_slot(
        deps.session,
        requester=requester,
        target_id=target_id,
        slot_id=slot_id,
        appointment_type_code=appointment_type,
        which=which,
    )
    if not result.get("ok"):
        return tool_result(result)
    deps.session.commit()
    return tool_result(result)


@tool(
    "cancel_appointment",
    args_schema=CancelArgs,
    response_format="content_and_artifact",
    description=(
        "Propose cancelling an existing appointment. Requires explicit confirmation before "
        "anything is released."
    ),
)
def cancel_appointment(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    appointment_id: int | None,
    which: str | None,
    reason: str | None,
) -> tuple[str, dict[str, Any]]:
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))
    view, target_problem = _resolve_target(
        deps, requester, AppointmentTargetArgs(appointment_id=appointment_id, which=which)
    )
    if target_problem is not None or view is None:
        return tool_result(
            target_problem or tool_error("unknown_appointment", "I can't find that appointment.")
        )

    outcome = appointments.propose_cancellation(
        deps.session,
        conversation_id=requester.conversation_id,
        patient_id=requester.patient.id,
        view=view,
        reason=reason,
    )
    if not outcome.ok:
        return tool_result(
            tool_error(
                outcome.error or "cancel_failed",
                outcome.message or "I couldn't set up the cancellation.",
                appointment_unchanged=True,
            )
        )
    deps.session.commit()

    proposal = _load_proposal(deps, outcome.proposal_id)
    return tool_result(
        {
            "ok": True,
            "awaiting_confirmation": True,
            "appointment_unchanged": True,
            "proposal_id": outcome.proposal_id,
            "message": (
                "Nothing is cancelled yet. Confirm below and I'll release the appointment."
            ),
            "cards": (
                [scheduling.proposal_card(proposal, include_actions=True)] if proposal else []
            ),
        }
    )


@tool(
    "confirm_appointment_change",
    args_schema=ProposalIdArgs,
    response_format="content_and_artifact",
    description=(
        "Confirm a pending reschedule or cancellation proposal. Atomic: the replacement is "
        "booked and the original is marked rescheduled/cancelled together, so a failed move "
        "leaves the original appointment untouched. Repeating it replays the first result."
    ),
)
def confirm_appointment_change(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    proposal_id: str,
) -> tuple[str, dict[str, Any]]:
    """Confirm a reschedule or cancellation. Same atomic path as a booking."""
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))
    # Ownership and kind are re-checked in the service: a proposal id from
    # another conversation is indistinguishable from one that does not exist, and
    # a new-booking card must be confirmed with confirm_booking_proposal.
    return tool_result(
        scheduling.confirm_change(
            deps.session,
            requester=requester,
            proposal_id=proposal_id,
            confirm_key=_confirm_key(deps, proposal_id),
            actor="patient",
        )
    )


TOOLS = (
    get_patient_appointments,
    reschedule_appointment,
    cancel_appointment,
    confirm_appointment_change,
)


__all__ = [
    "TOOLS",
    "AppointmentTargetArgs",
    "CancelArgs",
    "ListAppointmentsArgs",
    "RescheduleArgs",
]
