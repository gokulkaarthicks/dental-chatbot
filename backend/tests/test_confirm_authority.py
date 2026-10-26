"""Confirmation authority comes from the patient's own message.

The card button has always been authoritative: a click names the option and the
server acts on it. The spoken equivalent was never checked. The model decided
when the patient had "agreed", so a paraphrase ("Great, I'll book that for you")
was enough to spend a hold, and a hallucinated yes could book an appointment
nobody accepted.

Now the server reads the patient's words once, at ingest, and the confirmation
tools are simply not offered on a turn that carries no agreement. A model that
calls one anyway is refused, because a refusal is the only honest answer to an
action this turn has no authority to take.
"""

from __future__ import annotations

import logging

from conftest import SEARCH_PHRASE
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.agent import runner
from app.models import Appointment, ProposalStatus
from app.services import patient_sessions, scheduling
from app.services.proposals import find_by_public_id
from tests.support import ScriptedChatModel, deps_for, invoke_tool
from tests.test_tool_gates import _unverified_deps

PUBLIC = {
    "create_staff_request",
    "find_registration_duplicates",
    "get_practice_info",
    "get_practice_summary",
    "get_session_context",
    "register_patient",
    "report_emergency",
    "verify_existing_patient",
}


def _hold(session, requester) -> str:  # noqa: ANN001
    """Hold a real open time through the booking tool, as the model would."""
    search = scheduling.find_availability(
        session,
        requester.clinic,
        patient=requester.patient,
        appointment_type_code="cleaning",
        date_phrases=[SEARCH_PHRASE],
        requester=requester,
    )
    slot_id = search.payload["cards"][0]["slot_id"]
    held = invoke_tool(
        deps_for(session, requester),
        "create_booking_proposal",
        {"slot_ids": [slot_id], "appointment_type": "cleaning"},
    )
    assert held["ok"] is True, held
    session.commit()
    return held["proposal_id"]


def _confirm_call(proposal_id: str) -> dict:
    return {
        "name": "confirm_booking_proposal",
        "args": {"proposal_id": proposal_id},
        "id": "call_confirm",
        "type": "tool_call",
    }


def _turn(scenario, text: str, model: ScriptedChatModel):  # noqa: ANN001, ANN202
    session, requester = scenario
    return runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text=text,
        llm=model,
    )


# --- a turn with no agreement of its own ----------------------------------


def test_a_question_turn_is_not_offered_the_confirmation_tools(scenario) -> None:
    session, requester = scenario
    proposal_id = _hold(session, requester)

    model = ScriptedChatModel([AIMessage(content="It's held for a few minutes.")])
    _turn(scenario, "is that the only time you have?", model)

    offered = set(model.tools_offered[0])
    assert "confirm_booking_proposal" not in offered
    assert "confirm_appointment_change" not in offered
    # Preparing a choice and turning it down are untouched.
    assert {"create_booking_proposal", "decline_booking_proposal"} <= offered
    assert find_by_public_id(session, proposal_id).status == ProposalStatus.PENDING.value


def test_a_confirmation_called_without_the_patients_yes_is_refused(scenario) -> None:
    """The guard runs at execution, so a hallucinated call books nothing."""
    session, requester = scenario
    proposal_id = _hold(session, requester)

    model = ScriptedChatModel(
        [
            AIMessage(content="", tool_calls=[_confirm_call(proposal_id)]),
            AIMessage(content="It's still held for you."),
        ]
    )
    result = _turn(scenario, "maybe later. what else is open?", model)

    assert result.degraded is False
    answers = [message for message in model.calls[1] if isinstance(message, ToolMessage)]
    assert any("tool_not_available" in str(message.content) for message in answers)

    assert find_by_public_id(session, proposal_id).status == ProposalStatus.PENDING.value
    assert list(session.scalars(select(Appointment))) == []


def test_a_hedge_is_not_an_agreement(scenario) -> None:
    session, requester = scenario
    _hold(session, requester)

    model = ScriptedChatModel([AIMessage(content="Take your time.")])
    _turn(scenario, "I'm not sure that works for me", model)

    assert "confirm_booking_proposal" not in set(model.tools_offered[0])


# --- the patient's own yes -------------------------------------------------


def test_the_patients_yes_is_offered_logged_and_acted_on(scenario, caplog) -> None:
    session, requester = scenario
    proposal_id = _hold(session, requester)

    model = ScriptedChatModel(
        [
            AIMessage(content="", tool_calls=[_confirm_call(proposal_id)]),
            AIMessage(content="You're booked."),
        ]
    )
    with caplog.at_level(logging.INFO, logger="app.agent.graph"):
        result = _turn(scenario, "yes that works, book it", model)

    assert result.degraded is False
    assert "confirm_booking_proposal" in set(model.tools_offered[0])
    assert find_by_public_id(session, proposal_id).status == ProposalStatus.ACCEPTED.value

    grants = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "turn.confirm_granted"
    ]
    assert len(grants) == 1
    # The grant is recorded as a fact about the turn, not by copying the patient.
    assert "book it" not in caplog.text


# --- the rest of the surface follows the same rule -------------------------


def test_an_unverified_chat_is_never_offered_the_booking_surface(scenario) -> None:
    session, _ = scenario
    deps = _unverified_deps(session)

    model = ScriptedChatModel([AIMessage(content="Let's get you sorted out.")])
    runner.run_turn(
        session,
        deps.clinic,
        conversation=deps.conversation,
        patient_session=deps.patient_session,
        patient_text="I'd like a cleaning next tuesday",
        llm=model,
    )

    offered = set(model.tools_offered[0])
    assert offered and offered <= PUBLIC
    assert "search_availability" not in offered


def test_verifying_mid_turn_widens_the_surface_for_the_next_round(scenario) -> None:
    """A verification inside a turn is read again before the next round binds."""
    session, requester = scenario
    deps = _unverified_deps(session)

    verify = {
        "name": "verify_existing_patient",
        "args": {
            "phone": requester.patient.phone,
            "date_of_birth": requester.patient.date_of_birth.strftime("%m/%d/%Y"),
        },
        "id": "call_verify",
        "type": "tool_call",
    }
    model = ScriptedChatModel(
        [
            AIMessage(content="", tool_calls=[verify]),
            AIMessage(content="Found you."),
        ]
    )
    runner.run_turn(
        session,
        requester.clinic,
        conversation=deps.conversation,
        patient_session=deps.patient_session,
        patient_text="it's Dana, +1 650 555 0123, born 04/12/1990",
        llm=model,
    )

    assert "search_availability" not in set(model.tools_offered[0])
    assert "search_availability" in set(model.tools_offered[1])
    assert patient_sessions.verified_patient(session, deps.patient_session) is not None
