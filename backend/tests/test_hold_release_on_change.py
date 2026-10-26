"""A hold is given back when the patient moves on, and is short by itself.

The bug this covers: clicking a time held it for the full window even when the
patient immediately asked for other times ("Change") and never confirmed. A
click that is reconsidered in ten seconds must not keep a dentist's interval
reserved, and the window is short (``HOLD_MINUTES``) for the patient who walks
away without saying anything at all.

Two independent behaviours, one test file:

* **Immediate release.** A search issued after a hold (which is what "Change"
  sends) releases that hold and supersedes its proposal in the same turn.
  A replacement search for a *move* does the same to the held replacement, and
  the original appointment is never touched either way.
* **Short window.** The hold window is minutes, not half an hour, and a lapsed
  hold is retired by the read that lists pending proposals -- while the proposal
  itself stays confirmable, because lapsing is not a cancellation.
"""

from __future__ import annotations

from datetime import time, timedelta

import pytest

from app.models import AppointmentStatus, SlotClaim, utcnow
from app.services import appointments, scheduling
from app.services.proposals import (
    HOLD_MINUTES,
    find_by_public_id,
    hold_deadline,
    pending_for_conversation,
    release_expired_holds,
)
from tests.test_reschedule_regression import (  # noqa: F401 - pytest fixtures
    ALTERNATE_TIME,
    NEW_TIME,
    active_holds,
    call,
    claim_count,
    confirm_change,
    pending,
    proposal_row,
)
from tests.test_reschedule_workflow import (  # noqa: F401 - pytest fixtures
    REPLACEMENT_DAY,
    active_for,
    db,
    get,
    open_session,
    propose_move,
    requester_in,
    world_session,
)

# --------------------------------------------------------------------------- #
# The window itself
# --------------------------------------------------------------------------- #


def test_the_hold_window_is_a_couple_of_minutes():
    """A click reserves a couple of minutes, not half an hour."""
    assert 0 < HOLD_MINUTES <= 5


def test_hold_deadline_is_the_configured_window_from_now():
    now = utcnow()
    assert hold_deadline(now) - now == timedelta(minutes=HOLD_MINUTES)


def test_a_created_proposal_holds_only_for_the_short_window(db, world_session):
    session, world, requester = world_session
    before = utcnow()
    result = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            "appointment_type": "cleaning",
        },
    )
    assert result["ok"] is True, result
    row = proposal_row(db, result["proposal_id"])
    assert row.expires_at <= before + timedelta(minutes=HOLD_MINUTES, seconds=5)


# --------------------------------------------------------------------------- #
# Changing your mind releases the hold immediately
# --------------------------------------------------------------------------- #


def test_changing_a_move_frees_the_replacement_hold_at_once(db, world_session):
    """Click a replacement, then ask for other times: the first is handed back."""
    session, world, requester = world_session
    proposal = propose_move(session, requester, world, REPLACEMENT_DAY, NEW_TIME)
    assert proposal["ok"] is True, proposal
    held = proposal["proposal_id"]
    assert active_holds(db, held) == 2

    # Same turn as the "Change" click: the assistant searches again for the move.
    answer = scheduling.find_replacement_availability(
        session, requester=requester, target_id=world.original.id
    )

    # The first replacement's hold is gone, immediately -- not in 2 minutes.
    assert active_holds(db, held) == 0
    assert find_by_public_id(session, held).status != "pending"
    # The move itself was never abandoned: the visit is still booked, untouched.
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_for(db, world.patient.id) == [world.original.id]
    # And the times it offers are replacement times for the *same* move.
    assert answer["intent"] == "reschedule"
    assert answer["target_appointment_id"] == world.original.id
    assert all(
        card.get("target_appointment_id") == world.original.id for card in answer.get("cards", [])
    )


def test_picking_a_different_replacement_only_ever_holds_one(db, world_session):
    """Two picks in a row leave exactly one live hold, not two."""
    session, world, requester = world_session
    first = propose_move(session, requester, world, REPLACEMENT_DAY, NEW_TIME)
    assert first["ok"] is True, first
    second = propose_move(session, requester, world, REPLACEMENT_DAY, ALTERNATE_TIME)
    assert second["ok"] is True, second

    assert active_holds(db, first["proposal_id"]) == 0
    assert active_holds(db, second["proposal_id"]) == 2
    assert pending(db, requester.conversation.id) == [
        (second["proposal_id"], "reschedule", "reschedule")
    ]


def test_changing_a_booking_releases_its_hold_in_the_same_turn(db, world_session):
    """The ordinary booking card: Change hands the time back at once."""
    session, world, requester = world_session
    result = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            "appointment_type": "cleaning",
        },
    )
    assert result["ok"] is True, result
    held = result["proposal_id"]
    assert active_holds(db, held) == 2

    answer = scheduling.find_availability(
        session,
        requester.clinic,
        patient=requester.patient,
        appointment_type_code="cleaning",
        requester=requester,
        start_date=REPLACEMENT_DAY.isoformat(),
    )

    assert active_holds(db, held) == 0
    assert pending(db, requester.conversation.id) == []
    # The freed time is genuinely open again: the grid offers that day's times.
    assert answer.payload.get("available") is True
    offered = {card["slot_id"] for card in answer.payload.get("cards", [])}
    assert world.stable_slot(REPLACEMENT_DAY, time(8, 0)) in offered


def test_releasing_does_not_touch_a_booking_that_was_confirmed(db, world_session):
    """Confirm first, then search: a real appointment is never released."""
    session, world, requester = world_session
    proposal = propose_move(session, requester, world, REPLACEMENT_DAY, NEW_TIME)
    confirmed = confirm_change(session, requester, proposal["proposal_id"])
    assert confirmed["ok"] is True, confirmed
    replacement_id = confirmed["appointments"][0]["appointment_id"]

    scheduling.find_availability(
        session,
        requester.clinic,
        patient=requester.patient,
        appointment_type_code="cleaning",
        requester=requester,
    )

    assert get(db, replacement_id).status == AppointmentStatus.BOOKED.value
    assert active_for(db, world.patient.id) == [replacement_id]


# --------------------------------------------------------------------------- #
# A lapsed hold is retired, but the card is still confirmable
# --------------------------------------------------------------------------- #


def test_a_lapsed_hold_is_released_and_not_offered_as_pending(db, world_session):
    session, world, requester = world_session
    result = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            "appointment_type": "cleaning",
        },
    )
    held = result["proposal_id"]
    assert active_holds(db, held) == 2

    # Age the proposal past its window, as the wall clock would.
    with open_session(db) as other:
        row = find_by_public_id(other, held)
        row.expires_at = utcnow() - timedelta(seconds=1)
        other.commit()

    with open_session(db) as reading:
        released = release_expired_holds(reading, requester.conversation.id)
        reading.commit()

    assert released == [held]
    assert active_holds(db, held) == 0
    # The card is still confirmable: the hold ended, the choice did not.
    assert find_by_public_id(session, held).status == "pending"


def test_a_lapsed_hold_can_still_be_confirmed_when_the_time_is_still_open(db, world_session):
    """Lapsing is not a decline: nothing was booked, and the time is still real."""
    session, world, requester = world_session
    proposal = propose_move(session, requester, world, REPLACEMENT_DAY, NEW_TIME)
    held = proposal["proposal_id"]

    with open_session(db) as other:
        row = find_by_public_id(other, held)
        row.expires_at = utcnow() - timedelta(seconds=1)
        other.commit()

    with open_session(db) as reading:
        release_expired_holds(reading, requester.conversation.id)
        reading.commit()

    # The hold is gone (the slot is free for anyone), and confirming now
    # revalidates it and books it: the patient did not lose their choice.
    result = confirm_change(session, requester, held)
    assert result["ok"] is True, result
    assert result["confirmed"] is True
    assert len(result["appointments"]) == 1
    assert get(db, world.original.id).status == AppointmentStatus.RESCHEDULED.value


def test_release_expired_holds_accepts_the_public_conversation_id(db, world_session):
    """Callers holding the conversation record pass its public id, not its row id."""
    session, world, requester = world_session
    result = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            "appointment_type": "cleaning",
        },
    )
    with open_session(db) as other:
        row = find_by_public_id(other, result["proposal_id"])
        row.expires_at = utcnow() - timedelta(seconds=1)
        other.commit()

    with open_session(db) as reading:
        released = release_expired_holds(reading, requester.conversation.public_id)
        reading.commit()

    assert released == [result["proposal_id"]]


def test_a_hold_without_a_window_is_treated_as_lapsed(db, world_session):
    """Fixtures carry no ``expires_at``; skipping them would pin a slot forever."""
    session, world, requester = world_session
    result = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            "appointment_type": "cleaning",
        },
    )
    held = result["proposal_id"]
    assert active_holds(db, held) == 2

    with open_session(db) as other:
        find_by_public_id(other, held).expires_at = None
        other.commit()

    with open_session(db) as reading:
        released = release_expired_holds(reading, requester.conversation.id)
        reading.commit()

    assert released == [held]
    assert active_holds(db, held) == 0
    assert find_by_public_id(session, held).status == "pending"


def test_an_unexpired_hold_is_left_alone(db, world_session):
    """The housekeeping is not a second, eager release path."""
    session, world, requester = world_session
    proposal = propose_move(session, requester, world, REPLACEMENT_DAY, NEW_TIME)
    assert proposal["ok"] is True, proposal

    with open_session(db) as reading:
        assert release_expired_holds(reading, requester.conversation.id) == []
        reading.commit()

    assert active_holds(db, proposal["proposal_id"]) == 2
    assert len(pending_for_conversation(session, requester.conversation.id)) == 1
