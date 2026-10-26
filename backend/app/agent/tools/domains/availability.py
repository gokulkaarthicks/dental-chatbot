"""Availability tools: which times are open for whom, and for how many."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

from app.agent.context import TurnDeps
from app.agent.tools._helpers import (
    _family_member_patients,
    _household_summary,
    _registration_gate,
    _requester,
)
from app.agent.tools.domains.family import FamilyMemberArgs
from app.agent.tools.results import (
    ToolArguments,
    require_verified,
    tool_error,
    tool_result,
)
from app.services import booking_policy, patients, scheduling


class SearchAvailabilityArgs(ToolArguments):
    appointment_type: str | None = Field(
        default=None,
        description=(
            "'cleaning', 'checkup', or 'emergency'. Omit only when the stored scheduling "
            "request already specifies the visit type."
        ),
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


class ReplacementSearchArgs(ToolArguments):
    date_phrases: list[str] = Field(default_factory=list)
    start_date: str | None = None
    time_windows: list[str] = Field(default_factory=list)
    weekdays: list[str] = Field(default_factory=list)
    exclude_weekdays: list[str] = Field(default_factory=list)
    range_days: int | None = Field(default=None, ge=1, le=120)
    provider_name: str | None = None


class BookingContextArgs(ToolArguments):
    include_past: bool = Field(
        default=False,
        description="Also list visits already completed or cancelled.",
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
    weekdays: list[str] = Field(default_factory=list)
    exclude_weekdays: list[str] = Field(default_factory=list)
    range_days: int | None = Field(default=None, ge=1, le=120)
    provider_name: str | None = None
    allow_alternatives: bool = Field(
        default=False,
        description=(
            "True when the patient prefers the named date but explicitly accepts other dates."
        ),
    )
    back_to_back: bool = Field(
        default=True,
        description=(
            "Prefer one continuous block with each visit immediately after the previous one. "
            "Set false only when the patient permits gaps between family visits."
        ),
    )


@tool(
    "search_availability",
    args_schema=SearchAvailabilityArgs,
    response_format="content_and_artifact",
    description=(
        "Find genuinely open appointment times. Pass date phrases exactly as the patient "
        "said them ('tomorrow', 'next tuesday', 'early next month', '2026-11-12') and let "
        "the server resolve dates. Results are real slots the patient can click; showing "
        "them books nothing. This tool is for solo availability. For a household booking, "
        "call search_family_availability so the cards show one complete same-day time per "
        "person, ranked as close together as possible."
    ),
)
def search_availability(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    appointment_type: str | None,
    date_phrases: list[str],
    start_date: str | None,
    time_windows: list[str],
    weekdays: list[str],
    exclude_weekdays: list[str],
    range_days: int | None,
    provider_name: str | None,
    group_first_name: str | None,
    additional_visit: bool,
) -> tuple[str, dict[str, Any]]:
    patient, problem = require_verified(deps)
    if problem is not None or patient is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))
    gate = _registration_gate(deps, patient)
    if gate is not None:
        return tool_result(gate)

    requester, requester_problem = _requester(deps)
    if requester_problem is not None or requester is None:
        return tool_result(
            requester_problem or tool_error("not_verified", "This session isn't verified.")
        )

    # When a move is already pending, the times being searched for are
    # replacement times: the cards must say so, so a click continues the move
    # instead of starting a second booking. The visit's own type is kept.
    target_id = scheduling.pending_target(deps.session, requester)
    if target_id is not None:
        return tool_result(
            scheduling.find_replacement_availability(
                deps.session,
                requester=requester,
                target_id=target_id,
                date_phrases=date_phrases,
                start_date=start_date,
                time_windows=time_windows,
                weekdays=weekdays,
                exclude_weekdays=exclude_weekdays,
                range_days=range_days,
                provider_name=provider_name,
            )
        )

    # A re-ask such as "current availability" often arrives without a second
    # structured extraction from the model. Reuse the canonical request rather
    # than silently turning it into a new checkup search with empty preferences.
    stored = scheduling.scheduling_request(deps.session, requester.conversation_id) or {}
    stored_preferences = stored.get("preferences") or {}
    stored_participants = stored.get("participants") or []
    if len(stored_participants) > 1:
        return tool_result(
            tool_error(
                "family_search_required",
                (
                    "This request includes multiple family members. Use "
                    "search_family_availability with every stored participant and their own "
                    "appointment type; do not run a solo availability search."
                ),
                next_step=(
                    "Call search_family_availability with the complete stored family request."
                ),
            )
        )
    stored_type = (
        stored_participants[0].get("appointment_type_code")
        if len(stored_participants) == 1 and isinstance(stored_participants[0], dict)
        else None
    )
    answer = scheduling.find_availability(
        deps.session,
        deps.clinic,
        patient=patient,
        appointment_type_code=appointment_type or stored_type or "checkup",
        date_phrases=date_phrases or stored_preferences.get("date_phrases") or [],
        start_date=start_date or stored_preferences.get("start_date"),
        time_windows=time_windows or stored_preferences.get("time_windows") or [],
        weekdays=weekdays or stored_preferences.get("weekdays") or [],
        exclude_weekdays=exclude_weekdays
        or stored_preferences.get("exclude_weekdays")
        or [],
        range_days=range_days or stored_preferences.get("range_days"),
        provider_name=provider_name or stored_preferences.get("provider_name"),
        requester=requester,
        group_first_name=group_first_name,
        requests_additional=additional_visit,
    )
    payload = dict(answer.payload)
    payload.setdefault("patient_first_name", patients.first_name(patient.full_name))
    payload.setdefault("intent", "new_booking")
    return tool_result(payload)


@tool(
    "get_booking_context",
    args_schema=BookingContextArgs,
    response_format="content_and_artifact",
    description=(
        "Read what this booking already has and how many times it needs: solo (one person, "
        "one visit) or household (one visit per person), plus any pending hold or completed "
        "booking. Call it before offering times when the patient has asked for more than "
        "one appointment, or after a booking, so you hold exactly the right number."
    ),
)
def get_booking_context(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    include_past: bool,
) -> tuple[str, dict[str, Any]]:
    """What this booking already has, and how many times it needs."""
    del include_past
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))

    allowed, _ = scheduling.check_family_authorisation(deps.session, requester)
    stored = scheduling.scheduling_request(deps.session, requester.conversation_id) or {}
    requested_ids = {
        row.get("patient_id")
        for row in stored.get("participants", [])
        if row.get("patient_id") is not None
    }
    covered = [
        patient
        for patient in [requester.patient, *allowed]
        if not requested_ids or patient.id in requested_ids
    ]
    scope = booking_policy.booking_scope(
        deps.session,
        deps.clinic,
        patients=covered,
        conversation_id=requester.conversation_id,
    )
    payload: dict[str, Any] = {
        "ok": True,
        **scope.summary(),
        "household": _household_summary(deps, requester, allowed),
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
    return tool_result(payload)


@tool(
    "find_replacement_times",
    args_schema=ReplacementSearchArgs,
    response_format="content_and_artifact",
    description=(
        "When the patient is partway through moving an existing appointment and wants other "
        "times, search here. It keeps the visit's own type and duration and returns slot "
        "cards marked as reschedule replacements. Do not use it to book an additional visit."
    ),
)
def find_replacement_times(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    date_phrases: list[str],
    start_date: str | None,
    time_windows: list[str],
    weekdays: list[str],
    exclude_weekdays: list[str],
    range_days: int | None,
    provider_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Times a *pending move* could take, without leaving the reschedule flow."""
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))
    gate = _registration_gate(deps, requester.patient)
    if gate is not None:
        return tool_result(gate)

    result = scheduling.find_replacement_availability(
        deps.session,
        requester=requester,
        target_id=scheduling.pending_target(deps.session, requester),
        date_phrases=date_phrases,
        start_date=start_date,
        time_windows=time_windows,
        weekdays=weekdays,
        exclude_weekdays=exclude_weekdays,
        range_days=range_days,
        provider_name=provider_name,
    )
    if not result.get("ok") and result.get("error") == "no_active_booking":
        result["next_step"] = (
            "There is no visit to move. Use search_availability to book a new one."
        )
    return tool_result(result)


@tool(
    "search_family_availability",
    args_schema=FamilyAvailabilityArgs,
    response_format="content_and_artifact",
    description=(
        "Find currently available times for several household members (self plus children), "
        "with all visits on the same day. Prefer one continuous back-to-back block unless "
        "the patient permits gaps; each option has a time for every person, so one "
        "confirmation books everyone. Different visit types and durations are supported."
    ),
)
def search_family_availability(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    members: list[FamilyMemberArgs],
    date_phrases: list[str],
    start_date: str | None,
    time_windows: list[str],
    weekdays: list[str],
    exclude_weekdays: list[str],
    range_days: int | None,
    provider_name: str | None,
    allow_alternatives: bool,
    back_to_back: bool,
) -> tuple[str, dict[str, Any]]:
    requester, problem = _requester(deps)
    if problem is not None or requester is None:
        return tool_result(problem or tool_error("not_verified", "This session isn't verified."))

    resolved, member_problem = _family_member_patients(deps, requester, members)
    if member_problem is not None or resolved is None:
        return tool_result(
            member_problem or tool_error("unknown_family_member", "I couldn't match those names.")
        )

    stored = scheduling.scheduling_request(deps.session, requester.conversation_id) or {}
    stored_preferences = stored.get("preferences") or {}
    return tool_result(
        scheduling.family_search(
            deps.session,
            requester=requester,
            members=resolved,
            date_phrases=date_phrases or stored_preferences.get("date_phrases") or [],
            start_date=start_date or stored_preferences.get("start_date"),
            time_windows=time_windows or stored_preferences.get("time_windows") or [],
            weekdays=weekdays or stored_preferences.get("weekdays") or [],
            exclude_weekdays=exclude_weekdays
            or stored_preferences.get("exclude_weekdays")
            or [],
            range_days=range_days or stored_preferences.get("range_days"),
            provider_name=provider_name or stored_preferences.get("provider_name"),
            allow_alternatives=allow_alternatives,
            back_to_back=back_to_back,
        )
    )


TOOLS = (
    search_availability,
    get_booking_context,
    find_replacement_times,
    search_family_availability,
)


__all__ = [
    "TOOLS",
    "BookingContextArgs",
    "FamilyAvailabilityArgs",
    "ReplacementSearchArgs",
    "SearchAvailabilityArgs",
]
