"""Idempotency keys belong to the server, not to the model.

``create_key`` and ``confirm_key`` used to be arguments the provider could fill
in. That made the guard against a double hold or a double booking only as good as
the model's arithmetic: two calls in one turn with the same string replayed (a
good outcome), and two calls with different strings held twice (a bad one). Both
keys are now derived from the turn, so the only thing the model decides is *what
the patient asked for*.

Turn scoping is the half that matters. A key derived from the slot ids alone
would replay a proposal the patient already declined, which would put a refused
time back on the screen. Within one turn, a retry is the same request; across
turns, it is a new one.
"""

from __future__ import annotations

import pytest
from langgraph.prebuilt.tool_node import ToolInvocationError
from sqlalchemy import select

from app.agent.tools import TOOL_BY_NAME
from app.models import Appointment, AvailabilitySlot, BookingProposal, SlotStatus
from app.services import scheduling
from app.services.proposals import find_by_public_id
from tests.support import deps_for, invoke_tool


def _free_slot_id(session) -> str:  # noqa: ANN001
    slot = session.scalars(
        select(AvailabilitySlot)
        .where(AvailabilitySlot.status == SlotStatus.AVAILABLE.value)
        .order_by(AvailabilitySlot.starts_at)
    ).first()
    assert slot is not None, "the scenario needs an open time to hold"
    return slot.stable_id


def _hold(session, requester, slot_id: str, *, turn=None):  # noqa: ANN001, ANN002, ANN202
    arguments = {"slot_ids": [slot_id], "appointment_type": "cleaning"}
    return invoke_tool(turn or deps_for(session, requester), "create_booking_proposal", arguments)


# --- the model cannot supply one ------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "banned"),
    [
        ("create_booking_proposal", "create_key"),
        ("confirm_booking_proposal", "confirm_key"),
        ("confirm_appointment_change", "confirm_key"),
    ],
)
def test_no_tool_takes_an_idempotency_key_from_the_model(tool_name: str, banned: str) -> None:
    assert banned not in TOOL_BY_NAME[tool_name].args_schema.model_fields


def test_a_supplied_key_is_rejected_rather_than_ignored(scenario) -> None:
    """``extra="forbid"`` means a stale key in the prompt cannot smuggle itself in."""
    session, requester = scenario
    slot_id = _free_slot_id(session)

    with pytest.raises(ToolInvocationError):
        invoke_tool(
            deps_for(session, requester),
            "create_booking_proposal",
            {"slot_ids": [slot_id], "appointment_type": "cleaning", "create_key": "same-every-time"},
        )


# --- what the server derives ----------------------------------------------


def test_a_key_is_recorded_for_the_hold_and_another_for_the_booking(scenario) -> None:
    session, requester = scenario
    slot_id = _free_slot_id(session)

    deps = deps_for(session, requester)
    held = _hold(session, requester, slot_id, turn=deps)
    assert held["ok"] is True, held
    row = find_by_public_id(session, held["proposal_id"])
    assert row.create_key and row.create_key.startswith("turn:")
    assert row.confirm_key is None

    confirmed = invoke_tool(
        deps, "confirm_booking_proposal", {"proposal_id": held["proposal_id"]}
    )
    assert confirmed["ok"] is True, confirmed

    booked = find_by_public_id(session, held["proposal_id"])
    assert booked.confirm_key and booked.confirm_key.startswith("turn:")
    assert booked.confirm_key != booked.create_key


def test_a_retry_inside_one_turn_replays_the_same_hold(scenario) -> None:
    """The same turn asking for the same time twice is one proposal, not two.

    ``additional_visit`` is passed so the ordinary "a card is already waiting"
    guard cannot answer first: this has to be the key replaying.
    """
    session, requester = scenario
    slot_id = _free_slot_id(session)
    deps = deps_for(session, requester)
    arguments = {
        "slot_ids": [slot_id],
        "appointment_type": "cleaning",
        "additional_visit": True,
        "additional_visit_reason": "also wants a second cleaning",
        "summary": "additional visit: also wants a second cleaning",
    }

    first = invoke_tool(deps, "create_booking_proposal", arguments)
    again = invoke_tool(deps, "create_booking_proposal", arguments)

    assert first["ok"] is True and first.get("replayed") is not True, first
    assert again["ok"] is True and again["replayed"] is True, again
    assert again["proposal_id"] == first["proposal_id"]
    rows = list(
        session.scalars(
            select(BookingProposal).where(
                BookingProposal.conversation_id == requester.conversation.id
            )
        )
    )
    assert len(rows) == 1


def test_a_declined_hold_is_not_resurrected_by_a_later_turn(scenario) -> None:
    """The key is scoped to a turn, so a new turn makes a new request.

    This is the failure a slot-derived key would have produced: the patient says
    no, the next search offers the same time, they pick it, and the service
    replays the proposal they just declined instead of holding it again.
    """
    session, requester = scenario
    slot_id = _free_slot_id(session)

    declined = _hold(session, requester, slot_id)
    scheduling.decline(session, requester=requester, proposal_id=declined["proposal_id"])

    fresh = _hold(session, requester, slot_id)

    assert fresh["ok"] is True, fresh
    assert fresh["proposal_id"] != declined["proposal_id"]
    assert find_by_public_id(session, fresh["proposal_id"]).create_key != (
        find_by_public_id(session, declined["proposal_id"]).create_key
    )


def test_a_repeated_confirmation_books_once(scenario) -> None:
    """Two turns confirming the same card leave one appointment."""
    session, requester = scenario
    slot_id = _free_slot_id(session)
    held = _hold(session, requester, slot_id)

    first = invoke_tool(
        deps_for(session, requester), "confirm_booking_proposal", {"proposal_id": held["proposal_id"]}
    )
    again = invoke_tool(
        deps_for(session, requester), "confirm_booking_proposal", {"proposal_id": held["proposal_id"]}
    )

    assert first["ok"] is True and first["replayed"] is False, first
    assert again["ok"] is True and again["replayed"] is True, again
    booked = list(
        session.scalars(select(Appointment).where(Appointment.patient_id == requester.patient.id))
    )
    assert len(booked) == 1
