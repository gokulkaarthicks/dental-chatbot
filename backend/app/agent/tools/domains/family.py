"""Family tool: holding one combined option for a household booking."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

from app.agent.context import TurnDeps
from app.agent.tools._helpers import _family_member_patients, _requester
from app.agent.tools.results import ToolArguments, tool_error, tool_result
from app.services import scheduling


class FamilyMemberArgs(ToolArguments):
    patient_first_name: str = Field(description="First name of the family member.")
    appointment_type: str | None = Field(
        default=None,
        description=(
            "Visit type for this person. Required unless this person's type is already in the "
            "stored scheduling request; never silently default a family member to checkup."
        ),
    )
    provider_name: str | None = Field(
        default=None, description="Preferred dentist for this person."
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
    request_id: str | None = Field(
        default=None, description="The structured request_id from the clicked family card."
    )
    date_phrases: list[str] = Field(default_factory=list)
    start_date: str | None = None
    time_windows: list[str] = Field(default_factory=list)
    weekdays: list[str] = Field(default_factory=list)
    exclude_weekdays: list[str] = Field(default_factory=list)
    range_days: int | None = Field(default=None, ge=1, le=120)
    provider_name: str | None = None
    back_to_back: bool = False
    summary: str | None = None


@tool(
    "hold_family_option",
    args_schema=FamilyOptionArgs,
    response_format="content_and_artifact",
    description=(
        "Hold one family option as a single combined proposal. Confirming it books every "
        "member together or none. Pass the clicked card's option_identity when you have it. "
        "Never hold two options at once: the second hold is refused."
    ),
)
def hold_family_option(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    members: list[FamilyMemberArgs],
    option_index: int,
    option_identity: str | None,
    request_id: str | None,
    date_phrases: list[str],
    start_date: str | None,
    time_windows: list[str],
    weekdays: list[str],
    exclude_weekdays: list[str],
    range_days: int | None,
    provider_name: str | None,
    back_to_back: bool,
    summary: str | None,
) -> tuple[str, dict[str, Any]]:
    del back_to_back
    requester, _ = _requester(deps)
    resolved, member_problem = _family_member_patients(deps, requester, members)
    if member_problem is not None or resolved is None:
        return tool_result(
            member_problem or tool_error("unknown_family_member", "I couldn't match those names.")
        )

    stored = scheduling.scheduling_request(deps.session, requester.conversation_id) or {}
    stored_preferences = stored.get("preferences") or {}

    # One proposal containing every member's appointment, so confirmation is a
    # single transaction: the whole family is booked, or nobody is. Holding is
    # refused while another booking is pending or already booked.
    return tool_result(
        scheduling.hold_family_option(
            deps.session,
            requester=requester,
            members=resolved,
            option_index=option_index,
            option_identity=option_identity,
            request_id=request_id,
            date_phrases=date_phrases or stored_preferences.get("date_phrases") or [],
            start_date=start_date or stored_preferences.get("start_date"),
            time_windows=time_windows or stored_preferences.get("time_windows") or [],
            weekdays=weekdays or stored_preferences.get("weekdays") or [],
            exclude_weekdays=exclude_weekdays
            or stored_preferences.get("exclude_weekdays")
            or [],
            range_days=range_days or stored_preferences.get("range_days"),
            provider_name=provider_name or stored_preferences.get("provider_name"),
            summary=summary,
        )
    )


TOOLS = (hold_family_option,)


__all__ = ["TOOLS", "FamilyMemberArgs", "FamilyOptionArgs"]
