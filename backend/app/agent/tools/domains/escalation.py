"""Escalation tools: emergencies and front-desk follow-ups."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

from app.agent.context import TurnDeps
from app.agent.tools._helpers import _handoff_card_for, _requester
from app.agent.tools.results import ToolArguments, tool_result
from app.models import RequestType
from app.services import emergency, scheduling, staff_handoff

logger = logging.getLogger(__name__)


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


@tool(
    "report_emergency",
    args_schema=EmergencyArgs,
    response_format="content_and_artifact",
    description=(
        "Report dental symptoms or an urgent request. For breathing difficulty or other "
        "life-threatening signs the result tells you to direct the patient to 911 "
        "immediately, before any registration. Repeated reports update one alert."
    ),
)
def report_emergency(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    description: str | None,
    symptoms: list[str],
    wants_urgent_visit: bool,
    is_update: bool,
    caller_name: str | None,
    caller_phone: str | None,
) -> tuple[str, dict[str, Any]]:
    """Triage a symptom report. Deliberately works before any verification.

    An emergency must never wait for registration, so this tool does not gate on
    a verified patient. An unverified caller supplies a call-back name and phone,
    which is what the front desk needs to act on the alert.
    """
    conversation_id = deps.conversation_id
    patient = deps.verified_patient

    if is_update:
        report = emergency.follow_up(
            deps.session,
            conversation_id=conversation_id,
            patient_id=patient.id if patient else None,
            description=description or "Symptoms reported again.",
            caller_name=caller_name,
            caller_phone=caller_phone,
            patient_session=deps.patient_session,
        )
        # The card is read *before* the commit: the alert was added in this
        # transaction, and committing first expires the session, which would
        # re-read row-level state and hand the model a stale concern card.
        cards = _handoff_card_for(deps)
        deps.session.commit()
        payload = report.triage.as_dict()
        payload["worsening"] = report.worsening
        payload["message"] = emergency.emergency_summary_line(report.triage)
        payload["cards"] = cards
        return tool_result(payload)

    result = emergency.triage(
        deps.session,
        conversation_id=conversation_id,
        patient_id=patient.id if patient else None,
        description=description,
        symptoms=symptoms,
        wants_urgent_visit=wants_urgent_visit,
        caller_name=caller_name,
        caller_phone=caller_phone,
        patient_session=deps.patient_session,
    )
    # Read before the commit for the same reason as the follow-up path above.
    cards = _handoff_card_for(deps)
    deps.session.commit()

    payload = result.as_dict()
    payload["message"] = emergency.emergency_summary_line(result)
    if result.life_threatening:
        payload["do_not_register_first"] = True
        payload["must_say"] = emergency.EMERGENCY_CALL_ADVICE
    elif result.contact_required:
        # Nothing was filed. Say so plainly so the model cannot imply otherwise.
        payload["blocked"] = True
    payload["cards"] = cards
    return tool_result(payload)


@tool(
    "create_staff_request",
    args_schema=StaffRequestArgs,
    response_format="content_and_artifact",
    description=(
        "Ask the front desk to follow up on something you cannot verify (insurance "
        "networks, prices, financing, membership). Never invent an answer instead."
    ),
)
def create_staff_request(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    topic: str,
    details: str,
) -> tuple[str, dict[str, Any]]:
    conversation_id = deps.conversation_id
    patient = deps.verified_patient
    normalized_topic = topic.strip().lower()

    # A failed model-side family search is not evidence that the schedule is
    # unavailable. Before turning a scheduling miss into a human callback, use
    # the canonical, verified family request to perform one fresh SQLite search.
    # If it produces complete options, return those cards and keep the booking
    # in chat. Explicit non-scheduling callbacks and genuinely empty searches
    # still follow the normal human handoff path.
    if normalized_topic == "callback" and patient is not None:
        details_lower = details.lower()
        scheduling_terms = ("availability", "available", "slot", "booking", "appointment")
        availability_failure_terms = (
            "could not",
            "couldn't",
            "failed",
            "unavailable",
            "no availability",
            "no cards",
            "stale",
            "no longer",
        )
        alternative_terms = (
            "alternative",
            "another day",
            "different day",
            "other day",
            "flexible",
            "whenever",
        )
        human_request_terms = (
            "human",
            "staff",
            "front desk",
            "call me",
            "callback",
            "representative",
        )
        stored = scheduling.scheduling_request(deps.session, conversation_id) or {}
        if len(stored.get("participants") or []) > 1 and any(
            term in details_lower for term in scheduling_terms
        ) and any(term in details_lower for term in availability_failure_terms) and not any(
            term in details_lower for term in human_request_terms
        ):
            requester, requester_problem = _requester(deps)
            refreshed: dict[str, Any] | None = None
            refresh_failed = False
            if requester is not None and requester_problem is None:
                try:
                    # Re-run the canonical request first. This clears stale
                    # proposal/card state without changing who or what the
                    # family asked to book.
                    refreshed = scheduling.refresh_scheduling_request(
                        deps.session, requester=requester, request=stored
                    )
                    # If the patient said alternatives were acceptable but the
                    # model omitted that preference from the date phrase, move
                    # past the searched range once while preserving every
                    # participant, duration, window, and provider preference.
                    if (
                        refreshed
                        and not refreshed.get("cards")
                        and any(term in details_lower for term in alternative_terms)
                    ):
                        refreshed = scheduling.refresh_scheduling_request(
                            deps.session,
                            requester=requester,
                            alternative_day=True,
                            request=scheduling.scheduling_request(
                                deps.session, requester.conversation_id
                            ),
                            alternative_day_anchor=stored,
                        )
                except Exception:  # noqa: BLE001 - classify as an automation failure
                    logger.exception("Family availability recovery failed")
                    refresh_failed = True

            if refreshed and refreshed.get("cards"):
                count = len(stored.get("participants") or [])
                refreshed["message"] = (
                    "I found current family options. Choose one of the cards to hold all "
                    f"{count} visits together."
                )
                refreshed["grounded"] = True
                return tool_result(refreshed)

            if refreshed and refreshed.get("availability_checked") and not refresh_failed:
                # An empty, complete search is not a staff-system failure. Keep
                # the conversation in scheduling so the patient can change
                # dates/provider/coordination preferences without a duplicate
                # or premature callback being created.
                refreshed["message"] = (
                    "I checked the requested range and couldn't find a complete set of times "
                    "for everyone. We can try different dates, another provider, or separate "
                    "visits."
                )
                refreshed["next_step"] = (
                    "Do not create a callback from this availability result. Ask which date, "
                    "provider, or coordination preference the patient wants to change."
                )
                refreshed["grounded"] = True
                return tool_result(refreshed)

    request_type = {
        "insurance": RequestType.BILLING,
        "billing": RequestType.BILLING,
        "financing": RequestType.BILLING,
        "membership": RequestType.BILLING,
        "clinical_question": RequestType.CLINICAL_QUESTION,
        "callback": RequestType.CALLBACK,
        "reschedule": RequestType.RESCHEDULE,
    }.get(normalized_topic, RequestType.CALLBACK)

    result = staff_handoff.raise_request(
        deps.session,
        conversation_id=conversation_id,
        patient_id=patient.id if patient else None,
        request_type=request_type,
        details=f"{normalized_topic}: {details}".strip(": "),
        subject=normalized_topic,
    )
    deps.session.commit()
    payload = result.as_dict()
    payload["message"] = (
        "I've sent this to the front desk. I can't confirm coverage, pricing, or financing "
        "myself, so a team member will follow up with you."
    )
    payload["grounded"] = False
    payload["cards"] = _handoff_card_for(deps)
    return tool_result(payload)


TOOLS = (report_emergency, create_staff_request)


__all__ = ["TOOLS", "EmergencyArgs", "StaffRequestArgs"]
