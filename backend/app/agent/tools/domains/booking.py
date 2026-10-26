"""Booking tools: holds, confirmations and declines for a new visit."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

from app.agent.context import TurnDeps
from app.agent.tools._helpers import (
    _appointment_type,
    _confirm_key,
    _create_key,
    _pending_reschedule_summary,
    _registration_gate,
    _requester,
)
from app.agent.tools.results import ToolArguments, tool_error, tool_result
from app.services import patients, scheduling


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


class DeclineProposalArgs(ToolArguments):
    proposal_id: str = Field(description="Proposal id to release.")
    reason: str | None = Field(default=None, description="Why, if the patient said.")


@tool(
    "create_booking_proposal",
    args_schema=CreateProposalArgs,
    response_format="content_and_artifact",
    description=(
        "Hold one or more slot ids for a NEW appointment so the patient can confirm. Use "
        "after the patient chooses a time for a visit they do not have yet. Never use it to "
        "move an existing visit (reschedule_appointment does that), and never when the patient "
        "said move/reschedule/change. A new booking adds a visit: it never cancels or "
        "replaces an existing appointment. While a move is pending this returns "
        "reschedule_in_progress; only retry with additional_visit=true when the patient "
        "explicitly asked for another visit as well. Returns an Awaiting Confirmation card. "
        "Never say an appointment is booked at this point."
    ),
)
def create_booking_proposal(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    slot_ids: list[str],
    intent: str | None,
    appointment_id: int | None,
    appointment_type: str,
    additional_visit: bool,
    additional_visit_reason: str | None,
    patient_first_name: str | None,
    summary: str | None,
) -> tuple[str, dict[str, Any]]:
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
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))

    if intent == "reschedule" or appointment_id is not None:
        if not slot_ids:
            return tool_result(
                tool_error(
                    "missing_slot",
                    "Which replacement time should I hold for the move?",
                    kind="reschedule",
                    intent="reschedule",
                    appointment_unchanged=True,
                )
            )
        if len(slot_ids) > 1:
            return tool_result(
                tool_error(
                    "too_many_slots",
                    "A move replaces one appointment at a time. Which of those times do you want?",
                    kind="reschedule",
                    intent="reschedule",
                    appointment_unchanged=True,
                )
            )
        result = scheduling.replacement_for_slot(
            deps.session,
            requester=requester,
            target_id=appointment_id,
            slot_id=slot_ids[0],
            appointment_type_code=appointment_type,
        )
        if result.get("ok"):
            deps.session.commit()
        return tool_result(result)

    resolved_type, type_problem = _appointment_type(deps, appointment_type)
    if type_problem is not None or resolved_type is None:
        return tool_result(type_problem or tool_error("unknown_type", "Unknown visit type."))

    pending_move = _pending_reschedule_summary(deps, requester)
    if pending_move is not None:
        holds_the_move = pending_move.get("slot_id") in set(slot_ids)
        if not (
            additional_visit
            and additional_visit_reason
            and additional_visit_reason.strip()
        ):
            return tool_result(
                tool_error(
                    "reschedule_in_progress",
                    (
                        "You're partway through moving an appointment to the time you picked. "
                        "Finish that move, or tell me you'd rather book a separate visit instead."
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
            )

    target_id = requester.patient.id
    first_name = patients.first_name(requester.patient.full_name)
    if patient_first_name:
        wanted = patient_first_name.strip().lower()
        if wanted != first_name.lower():
            allowed, _ = scheduling.check_family_authorisation(deps.session, requester)
            match = next(
                (
                    patient
                    for patient in allowed
                    if patient.full_name.split(" ")[0].lower() == wanted
                ),
                None,
            )
            if match is None:
                return tool_result(
                    tool_error(
                        "not_authorized",
                        (
                            "I can only book for you and the other people on your household "
                            "account. Someone outside the household needs to contact us "
                            "themselves."
                        ),
                    )
                )
            target_id = match.id
            first_name = patients.first_name(match.full_name)

    # The key is derived from this turn and these slots, so a retry inside the
    # turn answers with the card the first call already created. That check has
    # to happen before the slots are resolved: the first call held them, and a
    # held slot no longer resolves as open.
    create_key = _create_key(deps, slot_ids)
    replayed = scheduling.replay_hold(deps.session, requester=requester, create_key=create_key)
    if replayed is not None:
        return tool_result(replayed)

    options = scheduling.options_for_slots(
        deps.session,
        deps.clinic,
        appointment_type=resolved_type,
        slot_ids=slot_ids,
        patient_id=target_id,
        patient_first_name=first_name,
    )
    if not options:
        return tool_result(
            tool_error(
                "slot_unavailable",
                "That time is no longer open. I'll show current options.",
            )
        )
    return tool_result(
        scheduling.propose_booking(
            deps.session,
            requester=requester,
            appointment_type=resolved_type,
            options=options,
            summary=summary,
            create_key=create_key,
        )
    )


@tool(
    "confirm_slot_selection",
    args_schema=ConfirmSlotSelectionArgs,
    response_format="content_and_artifact",
    description=(
        "A time was picked on a card. Call this instead of create_booking_proposal when the "
        "patient clicked a slot after a time search: it continues whatever the card meant. "
        "For a replacement slot of an existing visit it holds the new time as a reschedule "
        "proposal; for a new-booking card it holds a new visit."
    ),
)
def confirm_slot_selection(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    slot_id: str,
    intent: str | None,
    appointment_id: int | None,
    conversation_id: str | None,
    appointment_type: str | None,
) -> tuple[str, dict[str, Any]]:
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
    del conversation_id
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))
    gate = _registration_gate(deps, requester.patient)
    if gate is not None:
        return tool_result(gate)

    wants_move = intent == "reschedule" or appointment_id is not None
    if not wants_move:
        # A stale new-booking card clicked while a move is pending: the safest
        # reading is "continue the move", never "book a second appointment".
        wants_move = scheduling.pending_target(deps.session, requester) is not None
    if wants_move:
        result = scheduling.replacement_for_slot(
            deps.session,
            requester=requester,
            target_id=appointment_id,
            slot_id=slot_id,
            appointment_type_code=appointment_type,
            from_ui=True,
        )
        deps.session.commit()
        return tool_result(result)

    resolved_type, type_problem = _appointment_type(deps, appointment_type or "checkup")
    if type_problem is not None or resolved_type is None:
        return tool_result(type_problem or tool_error("unknown_type", "Unknown visit type."))
    options = scheduling.options_for_slots(
        deps.session,
        deps.clinic,
        appointment_type=resolved_type,
        slot_ids=[slot_id],
        patient_id=requester.patient.id,
        patient_first_name=patients.first_name(requester.patient.full_name),
    )
    if not options:
        return tool_result(
            tool_error(
                "slot_unavailable",
                "That time is no longer open. I'll show current options.",
                intent="new_booking",
            )
        )
    result = scheduling.propose_booking(
        deps.session,
        requester=requester,
        appointment_type=resolved_type,
        options=options,
        from_ui=True,
    )
    deps.session.commit()
    return tool_result(result)


@tool(
    "confirm_booking_proposal",
    args_schema=ProposalIdArgs,
    response_format="content_and_artifact",
    description=(
        "Confirm a pending NEW-booking proposal. Revalidates availability in a transaction "
        "and only then books. Use when the patient explicitly agrees in conversation, or "
        "when the UI reports a confirm click. Do not use it for reschedule or cancellation "
        "cards: those go to confirm_appointment_change."
    ),
)
def confirm_booking_proposal(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    proposal_id: str,
) -> tuple[str, dict[str, Any]]:
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))
    # A reschedule or cancellation card must be confirmed through
    # confirm_appointment_change, which knows the original to retire.
    # scheduling.confirm_booking refuses one; this names the right tool.
    # scheduling.confirm runs its own write transaction (and the commit trailer),
    # so the request session must not wrap it in a second one.
    result = scheduling.confirm_booking(
        deps.session,
        requester=requester,
        proposal_id=proposal_id,
        confirm_key=_confirm_key(deps, proposal_id),
        actor="patient",
    )
    if not result.get("ok") and result.get("error") == "wrong_proposal_kind":
        result["confirm_with"] = "confirm_appointment_change"
    return tool_result(result)


@tool(
    "decline_booking_proposal",
    args_schema=DeclineProposalArgs,
    response_format="content_and_artifact",
    description=(
        "Release a held proposal without booking. Use when the patient says no, or wants "
        "different times."
    ),
)
def decline_booking_proposal(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    proposal_id: str,
    reason: str | None,
) -> tuple[str, dict[str, Any]]:
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))
    return tool_result(
        scheduling.decline(
            deps.session, requester=requester, proposal_id=proposal_id, reason=reason
        )
    )


TOOLS = (
    create_booking_proposal,
    confirm_slot_selection,
    confirm_booking_proposal,
    decline_booking_proposal,
)


__all__ = [
    "TOOLS",
    "ConfirmSlotSelectionArgs",
    "CreateProposalArgs",
    "DeclineProposalArgs",
    "ProposalIdArgs",
    "SlotIdsArgs",
]
