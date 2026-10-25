"""Reschedule regression coverage: the workflow decisions around the move itself.

Companion to ``test_reschedule_workflow.py``. That file protects the *atomic
move* (one write transaction, rollback, replay). This one protects the
*workflow* that decides a move is what the patient asked for -- the part that
broke in the October 27 -> October 29 bug:

* a new booking and a reschedule are distinguishable everywhere: tool result,
  proposal row, card, and confirmation;
* the original appointment is proved to belong to the caller before anything is
  held;
* a replacement chosen by clicking the card, or by naming its time, continues
  the same move even if the model restates an earlier search;
* the ordinary booking tool cannot stand in for that move;
* confirmation re-checks ownership and the patient's other bookings inside the
  write transaction, and rolls everything back when anything fails;
* a repeated confirmation replays;
* an explicitly requested *additional* visit never cancels the existing one.

The whole flow is driven through the real tool layer (``execute_tool``, what the
model calls) and the real service layer (what the UI calls). No model and no
HTTP server is involved.
"""

from __future__ import annotations

import json
from datetime import datetime, time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.tools import ToolContext, execute_tool
from app.models import (
    Appointment,
    AppointmentStatus,
    BookingGroup,
    ProposalStatus,
    SlotClaim,
    SlotStatus,
    utcnow,
)
from app.services import appointments, scheduling
from app.services.proposals import (
    expire_proposal,
    find_by_public_id,
    pending_for_conversation,
)
from app.services.scheduling import Requester

# The scenario's fixtures are shared with the workflow suite so the two files
# describe the same world: the October 27 cleaning and its open replacement day.
from tests.test_reschedule_workflow import (  # noqa: F401 - pytest fixtures
    ORIGINAL_DAY,
    REPLACEMENT_DAY,
    SessionFactory,
    World,
    active_for,
    db,
    get,
    open_session,
    requester_in,
    slot_holder,
    slot_is_free,
    world_session,
)

# The October 29 afternoon time the patient picks in the regression scenario.
NEW_TIME = time(12, 30)
ALTERNATE_TIME = time(15, 0)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def tool_ctx(session: Session, requester: Requester) -> ToolContext:
    return ToolContext(
        session=session,
        patient_session=requester.patient_session,
        conversation_id=requester.conversation.id,
    )


def call(session: Session, requester: Requester, name: str, arguments: dict) -> dict:
    return execute_tool(tool_ctx(session, requester), name, json.dumps(arguments))


def move(
    session: Session,
    requester: Requester,
    world: World,
    starts_at: time = NEW_TIME,
    *,
    appointment_id: int | None = None,
) -> dict:
    """``reschedule_appointment`` exactly as the model sends it."""
    arguments: dict = {"slot_id": world.stable_slot(REPLACEMENT_DAY, starts_at)}
    if appointment_id is not None:
        arguments["appointment_id"] = appointment_id
    return call(session, requester, "reschedule_appointment", arguments)


def confirm_change(session: Session, requester: Requester, proposal_id: str) -> dict:
    return call(session, requester, "confirm_appointment_change", {"proposal_id": proposal_id})


def pending(db: SessionFactory, conversation_id: int) -> list[tuple[str, str, str | None]]:
    with open_session(db) as session:
        return [
            (row.public_id, row.kind, row.intent)
            for row in pending_for_conversation(session, conversation_id)
        ]


def proposal_row(db: SessionFactory, public_id: str):
    with open_session(db) as session:
        return find_by_public_id(session, public_id)


def active_claims(db: SessionFactory, appointment_id: int) -> int:
    with open_session(db) as session:
        return len(
            list(
                session.scalars(
                    select(SlotClaim).where(
                        SlotClaim.appointment_id == appointment_id,
                        SlotClaim.active.is_(True),
                    )
                )
            )
        )


def claim_count(db: SessionFactory) -> int:
    """Every active claim, held or booked: the fixture's ground truth for leaks."""
    with open_session(db) as session:
        return len(list(session.scalars(select(SlotClaim).where(SlotClaim.active.is_(True)))))


def active_holds(db: SessionFactory, proposal_public_id: str) -> int:
    with open_session(db) as session:
        proposal = find_by_public_id(session, proposal_public_id)
        assert proposal is not None
        return len(
            list(
                session.scalars(
                    select(SlotClaim).where(
                        SlotClaim.held_by_proposal_id == proposal.id,
                        SlotClaim.active.is_(True),
                    )
                )
            )
        )


def replacements(db: SessionFactory) -> list[Appointment]:
    with open_session(db) as session:
        return list(
            session.scalars(select(Appointment).where(Appointment.rescheduled_from_id.isnot(None)))
        )


def staff_upcoming_ids(db: SessionFactory, patient_id: int) -> list[int]:
    from app.services import staff_scheduling

    with open_session(db) as session:
        rows = staff_scheduling.list_appointments(
            session, staff_scheduling.AppointmentFilters(upcoming_only=True)
        )
        return [row.id for row in rows if row.patient_id == patient_id]


def upcoming_from_patient_chat(db: SessionFactory, patient_id: int) -> list[dict]:
    """Exactly what the patient's appointment listing shows as upcoming."""
    with open_session(db) as session:
        return [
            view.as_dict()
            for view in appointments.list_for_patient(session, patient_id)
            if view.is_upcoming
        ]


# --------------------------------------------------------------------------- #
# Intent: a new booking is not a reschedule
# --------------------------------------------------------------------------- #


def test_reschedule_records_the_original_and_its_intent(db, world_session):
    session, world, requester = world_session
    result = move(session, requester, world)

    assert result["ok"] is True, result
    assert result["kind"] == "reschedule"
    assert result["intent"] == "reschedule"
    assert result["target_appointment_id"] == world.original.id
    assert result["appointment_unchanged"] is True
    assert result["cards"][0]["type"] == "proposal"
    assert result["cards"][0]["intent"] == "reschedule"
    assert result["cards"][0]["target_appointment_id"] == world.original.id

    row = proposal_row(db, result["proposal_id"])
    assert (row.kind, row.intent) == ("reschedule", "reschedule")
    assert row.target_appointment_id == world.original.id
    # Persisted per item as well, so confirmation and replay agree on the target.
    assert [item.target_appointment_id for item in row.items] == [world.original.id]

    # Proposing moves nothing: the original is still the one active booking.
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_for(db, world.patient.id) == [world.original.id]
    assert active_claims(db, world.original.id) == 2
    # Exactly one proposal is waiting, and it is a move.
    assert pending(db, requester.conversation.id) == [
        (result["proposal_id"], "reschedule", "reschedule")
    ]


def test_new_booking_records_a_different_intent_and_targets_nothing(db, world_session):
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
    assert result["intent"] == "new_booking"
    assert result.get("target_appointment_id") is None
    assert result["cards"][0]["intent"] == "new_booking"

    row = proposal_row(db, result["proposal_id"])
    assert (row.kind, row.intent, row.target_appointment_id) == ("booking", "new_booking", None)
    assert all(item.target_appointment_id is None for item in row.items)


# --------------------------------------------------------------------------- #
# Ownership of the original
# --------------------------------------------------------------------------- #


def test_a_foreign_appointment_id_cannot_be_proposed_for_a_move(db, world_session):
    session, world, requester = world_session
    foreign = world.book(
        session,
        patient_id=world.other_patient.id,
        day=REPLACEMENT_DAY,
        start=time(8, 0),
        intervals=2,
        appointment_type=world.cleaning,
    )
    session.commit()
    foreign_id = foreign.id
    foreign_start = foreign.starts_at
    claims_before = claim_count(db)

    result = move(session, requester, world, appointment_id=foreign_id)
    # Deliberately indistinguishable from "that appointment does not exist".
    assert result["ok"] is False
    assert result["error"] == "unknown_appointment"
    assert result["appointment_unchanged"] is True

    untouched = get(db, foreign_id)
    assert untouched.status == AppointmentStatus.BOOKED.value
    assert untouched.starts_at == foreign_start
    assert active_claims(db, foreign_id) == 2
    assert claim_count(db) == claims_before
    assert pending(db, requester.conversation.id) == []


def test_a_tampered_target_is_refused_inside_the_write_transaction(db, world_session):
    """A proposal row rewritten to name another patient's visit moves nothing."""
    session, world, requester = world_session
    foreign = world.book(
        session,
        patient_id=world.other_patient.id,
        day=REPLACEMENT_DAY,
        start=time(8, 0),
        intervals=2,
        appointment_type=world.cleaning,
    )
    session.commit()
    foreign_id = foreign.id

    result = move(session, requester, world)
    with open_session(db) as fresh:
        proposal = find_by_public_id(fresh, result["proposal_id"])
        proposal.target_appointment_id = foreign_id
        for item in proposal.items:
            item.target_appointment_id = foreign_id
        fresh.commit()

    attempted = confirm_change(session, requester, result["proposal_id"])
    assert attempted["ok"] is False, attempted
    assert attempted["appointment_unchanged"] is True
    assert attempted["kind"] == "reschedule"

    # Neither visit changed, and no replacement row was created.
    assert get(db, foreign_id).status == AppointmentStatus.BOOKED.value
    assert active_claims(db, foreign_id) == 2
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_claims(db, world.original.id) == 2
    assert replacements(db) == []


# --------------------------------------------------------------------------- #
# Choosing the replacement continues the same move
# --------------------------------------------------------------------------- #


def test_clicking_a_replacement_slot_keeps_the_move(db, world_session):
    """The UI posts the card back; the service continues the move unaided."""
    session, world, requester = world_session

    # With nothing pending there is nothing to move, and the service says so
    # rather than quietly creating a booking.
    nothing = scheduling.replacement_for_slot(
        session,
        requester=requester,
        target_id=None,
        slot_id=world.stable_slot(REPLACEMENT_DAY, time(14, 0)),
        from_ui=True,
    )
    assert nothing["ok"] is False
    assert nothing["error"] == "no_reschedule_target"
    assert nothing["intent"] == "new_booking"
    assert pending(db, requester.conversation.id) == []

    started = move(session, requester, world)

    # A refresh of the replacement grid re-renders its cards as reschedule cards.
    replacement_cards = scheduling.replacement_cards(
        session,
        requester=requester,
        options=scheduling.options_for_slots(
            session,
            appointment_type=world.cleaning,
            slot_ids=[world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            patient_id=world.patient.id,
        ),
        appointment_type=world.cleaning,
        target_id=world.original.id,
    )
    assert replacement_cards, "the alternate time should still be open"
    card = replacement_cards[0]
    assert card["intent"] == "reschedule"
    assert card["target_appointment_id"] == world.original.id
    assert card["moving_from"].startswith(f"{ORIGINAL_DAY.isoformat()}T10:00")

    clicked = scheduling.replacement_for_slot(
        session,
        requester=requester,
        target_id=card["target_appointment_id"],
        slot_id=card["slot_id"],
        from_ui=True,
    )
    assert clicked["ok"] is True, clicked
    assert clicked["kind"] == "reschedule"
    assert clicked["intent"] == "reschedule"
    assert clicked["target_appointment_id"] == world.original.id
    assert clicked["proposal_id"] != started["proposal_id"]

    # One move stays pending, and the first replacement's hold was released so
    # the old time does not sit blocked until it expires.
    assert pending(db, requester.conversation.id) == [
        (clicked["proposal_id"], "reschedule", "reschedule")
    ]
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, NEW_TIME).id)
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, ALTERNATE_TIME).id)
    assert active_holds(db, clicked["proposal_id"]) == 2

    done = confirm_change(session, requester, clicked["proposal_id"])
    assert done["ok"] is True, done
    assert done["kind"] == "reschedule"
    assert len(active_for(db, world.patient.id)) == 1


def test_replying_with_a_time_uses_the_pending_move(db, world_session):
    """No card fields at all: the pending move is still the context."""
    session, world, requester = world_session
    move(session, requester, world)

    result = call(
        session,
        requester,
        "confirm_slot_selection",
        {"slot_id": world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)},
    )
    assert result["ok"] is True, result
    assert result["kind"] == "reschedule"
    assert result["target_appointment_id"] == world.original.id
    assert [entry[1] for entry in pending(db, requester.conversation.id)] == ["reschedule"]


def test_clicking_keeps_the_moves_own_type_and_duration(db, world_session):
    """A 60-minute cleaning must not be shortened to the default checkup."""
    session, world, requester = world_session
    move(session, requester, world)
    result = call(
        session,
        requester,
        "confirm_slot_selection",
        {
            "slot_id": world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME),
            "intent": "reschedule",
        },
    )
    assert result["ok"] is True, result
    item = result["cards"][0]["items"][0]
    assert item["duration_minutes"] == 60
    assert item["appointment_type_code"] == "cleaning"


def test_the_booking_tool_cannot_hold_the_pending_replacement(db, world_session):
    """The exact bug path: the model restates the chosen time as a new booking."""
    session, world, requester = world_session
    started = move(session, requester, world)
    claims_before = claim_count(db)

    result = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, NEW_TIME)],
            "appointment_type": "cleaning",
        },
    )
    assert result["ok"] is False, result
    assert result["error"] == "reschedule_in_progress"
    assert result["kind"] == "reschedule"
    assert result["intent"] == "reschedule"
    assert result["appointment_unchanged"] is True
    # It points at the move already waiting instead of starting another one.
    assert result["proposal_id"] == started["proposal_id"]
    assert result["appointment_id"] == world.original.id
    assert result["confirm_with"] == "confirm_appointment_change"
    assert result["same_slot_as_pending"] is True

    # Nothing changed: same pending proposal, same holds, original still booked.
    assert pending(db, requester.conversation.id) == [
        (started["proposal_id"], "reschedule", "reschedule")
    ]
    assert claim_count(db) == claims_before
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value

    # Finishing the move through the right tool still works.
    done = confirm_change(session, requester, started["proposal_id"])
    assert done["ok"] is True, done
    assert len(active_for(db, world.patient.id)) == 1


def test_the_booking_tool_refuses_another_time_while_a_move_is_pending(db, world_session):
    session, world, requester = world_session
    started = move(session, requester, world)
    result = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            "appointment_type": "cleaning",
        },
    )
    assert result["ok"] is False
    assert result["error"] == "reschedule_in_progress"
    assert result["same_slot_as_pending"] is False
    assert result["proposal_id"] == started["proposal_id"]
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, ALTERNATE_TIME).id)
    assert pending(db, requester.conversation.id) == [
        (started["proposal_id"], "reschedule", "reschedule")
    ]


def test_asking_for_a_reschedule_routes_the_booking_tool_to_the_move(db, world_session):
    session, world, requester = world_session
    result = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, NEW_TIME)],
            "appointment_type": "cleaning",
            "intent": "reschedule",
            "appointment_id": world.original.id,
        },
    )
    assert result["ok"] is True, result
    assert result["kind"] == "reschedule"
    assert result["target_appointment_id"] == world.original.id
    row = proposal_row(db, result["proposal_id"])
    assert (row.kind, row.intent) == ("reschedule", "reschedule")
    # It became a move, never a second booking.
    assert active_for(db, world.patient.id) == [world.original.id]


def test_confirmation_tools_cannot_consume_each_others_proposals(db, world_session):
    session, world, requester = world_session
    moved = move(session, requester, world)

    wrong_tool = call(
        session, requester, "confirm_booking_proposal", {"proposal_id": moved["proposal_id"]}
    )
    assert wrong_tool["ok"] is False
    assert wrong_tool["error"] == "wrong_proposal_kind"
    assert wrong_tool["confirm_with"] == "confirm_appointment_change"

    # A pending move also blocks opening a new booking from the same thread.
    booking = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            "appointment_type": "cleaning",
        },
    )
    assert booking["ok"] is False and booking["error"] == "reschedule_in_progress"

    # Refusals changed nothing: the original is still the only active visit.
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_for(db, world.patient.id) == [world.original.id]
    assert pending(db, requester.conversation.id) == [
        (moved["proposal_id"], "reschedule", "reschedule")
    ]


# --------------------------------------------------------------------------- #
# The critical regression scenario
# --------------------------------------------------------------------------- #


def test_october_27_to_october_29_leaves_exactly_one_active_appointment(db, world_session):
    """Request -> choose 12:30 -> confirm, with no separate cancellation."""
    session, world, requester = world_session
    original_id = world.original.id
    original_slot_ids = [
        world.slot(ORIGINAL_DAY, time(10, 0)).id,
        world.slot(ORIGINAL_DAY, time(10, 30)).id,
    ]
    replacement_slot_ids = [
        world.slot(REPLACEMENT_DAY, NEW_TIME).id,
        world.slot(REPLACEMENT_DAY, time(13, 0)).id,
    ]

    held = move(session, requester, world)
    assert held["ok"] is True, held

    # Between the search and the confirmation nothing is booked or cancelled.
    assert get(db, original_id).status == AppointmentStatus.BOOKED.value
    assert slot_holder(db, original_slot_ids[0]) == original_id

    done = confirm_change(session, requester, held["proposal_id"])
    assert done["ok"] is True and done["confirmed"] is True, done
    replacement_id = done["appointments"][0]["appointment_id"]

    original = get(db, original_id)
    replacement = get(db, replacement_id)
    # October 27 automatically becomes inactive; October 29 becomes booked.
    assert original.status == AppointmentStatus.RESCHEDULED.value
    assert original.rescheduled_to_id == replacement_id
    assert original.cancelled_at is None  # a move, not a cancellation
    assert replacement.status == AppointmentStatus.BOOKED.value
    assert replacement.rescheduled_from_id == original_id
    assert replacement.starts_at == datetime.combine(REPLACEMENT_DAY, NEW_TIME)
    assert replacement.duration_minutes == 60

    # October 27's claims are released; October 29's belong to the replacement.
    for slot_id in original_slot_ids:
        assert slot_is_free(db, slot_id)
    for slot_id in replacement_slot_ids:
        assert slot_holder(db, slot_id) == replacement_id
    assert active_claims(db, original_id) == 0
    assert active_claims(db, replacement_id) == 2

    # Exactly one active appointment, and every view agrees on which one.
    assert active_for(db, world.patient.id) == [replacement_id]
    chat = upcoming_from_patient_chat(db, world.patient.id)
    assert [entry["appointment_id"] for entry in chat] == [replacement_id]
    assert staff_upcoming_ids(db, world.patient.id) == [replacement_id]

    # No separate cancellation request was created for the move.
    with open_session(db) as fresh:
        groups = list(
            fresh.scalars(
                select(BookingGroup).where(
                    BookingGroup.conversation_id == requester.conversation.id
                )
            )
        )
    assert [group.public_id for group in groups] == [done["booking_group_id"]]
    assert groups[0].kind == "reschedule"
    assert pending(db, requester.conversation.id) == []


def test_patient_history_keeps_the_old_visit_but_not_as_upcoming(db, world_session):
    session, world, requester = world_session
    held = move(session, requester, world)
    done = confirm_change(session, requester, held["proposal_id"])
    replacement_id = done["appointments"][0]["appointment_id"]

    with open_session(db) as fresh:
        history = appointments.list_for_patient(fresh, world.patient.id)
    by_id = {view.appointment.id: view for view in history}
    assert set(by_id) == {world.original.id, replacement_id}
    assert by_id[world.original.id].is_upcoming is False
    assert by_id[world.original.id].replaced_by_id == replacement_id
    assert by_id[replacement_id].is_upcoming is True
    assert by_id[replacement_id].replaces_id == world.original.id


# --------------------------------------------------------------------------- #
# Conflicts: the original survives every one of them
# --------------------------------------------------------------------------- #


def _drop_holds(session: Session) -> None:
    """Stand in for holds that lapsed, so another booking can take the time."""
    for claim in session.scalars(
        select(SlotClaim).where(
            SlotClaim.held_by_proposal_id.is_not(None), SlotClaim.active.is_(True)
        )
    ):
        claim.active = False
    session.flush()


def test_replacement_taken_before_confirmation_leaves_the_original_booked(db, world_session):
    session, world, requester = world_session
    held = move(session, requester, world)
    _drop_holds(session)
    other = world.book(
        session,
        patient_id=world.other_patient.id,
        day=REPLACEMENT_DAY,
        start=NEW_TIME,
        intervals=2,
        appointment_type=world.cleaning,
    )
    session.commit()
    other_id = other.id

    result = confirm_change(session, requester, held["proposal_id"])
    assert result["ok"] is False
    assert result["error"] == "slot_taken"
    assert result["appointment_unchanged"] is True
    assert result["kind"] == "reschedule"

    # The patient's original is untouched and still the only active booking.
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_claims(db, world.original.id) == 2
    assert active_for(db, world.patient.id) == [world.original.id]
    # The other patient's booking is untouched, and no replacement exists.
    assert get(db, other_id).status == AppointmentStatus.BOOKED.value
    assert replacements(db) == []
    # The card stays live so the patient can pick again.
    assert pending(db, requester.conversation.id) == [
        (held["proposal_id"], "reschedule", "reschedule")
    ]


def test_a_blocked_replacement_day_is_refused_without_moving_anything(db, world_session):
    session, world, requester = world_session
    held = move(session, requester, world)
    for row in world.slots[REPLACEMENT_DAY].values():
        row.status = SlotStatus.BLOCKED.value
    session.commit()

    result = confirm_change(session, requester, held["proposal_id"])
    assert result["ok"] is False
    assert result["appointment_unchanged"] is True

    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_claims(db, world.original.id) == 2
    assert active_for(db, world.patient.id) == [world.original.id]


def test_a_replacement_that_overlaps_another_visit_is_refused_before_holding(db, world_session):
    """The model may hand back an id from a much older search."""
    session, world, requester = world_session
    earlier = world.book(
        session,
        patient_id=world.patient.id,
        day=REPLACEMENT_DAY,
        start=time(9, 0),
        intervals=2,
        appointment_type=world.checkup,
    )
    session.commit()
    earlier_id = earlier.id
    claims_before = claim_count(db)

    # 9:30 would run into the 9:00-10:00 checkup the patient already has.
    result = move(session, requester, world, time(9, 30))

    assert result["ok"] is False
    assert result["appointment_unchanged"] is True
    # Refused before any proposal existed, so nothing whatsoever was held.
    assert pending(db, requester.conversation.id) == []
    assert claim_count(db) == claims_before
    assert get(db, earlier_id).status == AppointmentStatus.BOOKED.value
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value


def test_failure_after_the_release_rolls_everything_back(db, world_session, monkeypatch):
    """The failure point where a non-atomic move would strand the patient."""
    session, world, requester = world_session
    from app.services import proposals as proposals_service

    held = move(session, requester, world)

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated failure after the original's claims were released")

    # ``_claim_rows`` runs while inserting the replacement, i.e. after
    # ``_release_claims`` has already deactivated the original's claims.
    monkeypatch.setattr(proposals_service, "_claim_rows", explode)
    with pytest.raises(RuntimeError):
        with open_session(db) as fresh:
            request = requester_in(
                fresh, world.patient.id, requester.patient_session.id, requester.conversation.id
            )
            scheduling.confirm(fresh, requester=request, proposal_id=held["proposal_id"])
    monkeypatch.undo()

    original = get(db, world.original.id)
    assert original.status == AppointmentStatus.BOOKED.value
    assert original.rescheduled_to_id is None
    assert original.rescheduled_at is None
    # Its claims are active again, so its time is still occupied by it.
    assert active_claims(db, world.original.id) == 2
    assert slot_holder(db, world.slot(ORIGINAL_DAY, time(10, 0)).id) == world.original.id
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, NEW_TIME).id)
    assert replacements(db) == []
    assert active_for(db, world.patient.id) == [world.original.id]

    # The card is still pending, so confirming again simply works.
    assert pending(db, requester.conversation.id) == [
        (held["proposal_id"], "reschedule", "reschedule")
    ]
    retry = confirm_change(session, requester, held["proposal_id"])
    assert retry["ok"] is True, retry
    assert len(active_for(db, world.patient.id)) == 1


def test_an_expired_move_never_touches_the_original(db, world_session):
    session, world, requester = world_session
    held = move(session, requester, world)
    # Retire the proposal outright (declined, or replaced by a newer choice).
    # A hold merely lapsing is different: it frees the interval but leaves the
    # card confirmable, so the patient cannot lose a time they chose.
    with open_session(db) as fresh:
        stale = find_by_public_id(fresh, held["proposal_id"])
        expire_proposal(fresh, stale, utcnow())
        stale.status = ProposalStatus.EXPIRED.value
        stale.resolved_at = utcnow()
        fresh.commit()

    result = confirm_change(session, requester, held["proposal_id"])
    assert result["ok"] is False
    assert result["appointment_unchanged"] is True
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_for(db, world.patient.id) == [world.original.id]


def test_a_cancelled_original_cannot_be_moved(db, world_session):
    session, world, requester = world_session
    cancel = call(
        session, requester, "cancel_appointment", {"appointment_id": world.original.id}
    )
    assert cancel["ok"] is True, cancel
    confirmed = call(
        session,
        requester,
        "confirm_appointment_change",
        {"proposal_id": cancel["proposal_id"]},
    )
    assert confirmed["ok"] is True, confirmed

    result = move(session, requester, world)
    assert result["ok"] is False
    assert result["appointment_unchanged"] is True
    assert get(db, world.original.id).status == AppointmentStatus.CANCELLED.value
    assert pending(db, requester.conversation.id) == []


# --------------------------------------------------------------------------- #
# Duplicate confirmation
# --------------------------------------------------------------------------- #


def test_duplicate_confirmation_replays_and_books_nothing_new(db, world_session):
    session, world, requester = world_session
    held = move(session, requester, world)
    first = confirm_change(session, requester, held["proposal_id"])
    assert first["ok"] is True and first["replayed"] is False
    assert len(replacements(db)) == 1

    for _ in range(3):
        again = confirm_change(session, requester, held["proposal_id"])
        assert again["ok"] is True, again
        assert again["replayed"] is True
        assert again["kind"] == "reschedule"
        assert again["appointments"] == first["appointments"]
        assert again["released_appointment_ids"] == [world.original.id]

    # The UI's retry path goes straight to the service; it replays too.
    with open_session(db) as fresh:
        request = requester_in(
            fresh, world.patient.id, requester.patient_session.id, requester.conversation.id
        )
        direct = scheduling.confirm(fresh, requester=request, proposal_id=held["proposal_id"])
    assert direct["ok"] is True and direct["replayed"] is True

    # Exactly the original plus one replacement, however often it is replayed.
    assert len(replacements(db)) == 1
    assert len(active_for(db, world.patient.id)) == 1


def test_a_button_click_and_a_chat_confirmation_do_not_book_twice(db, world_session):
    """Both the card button and the assistant's confirm land; the visit moves once."""
    session, world, requester = world_session
    held = move(session, requester, world)
    ids = (world.patient.id, requester.patient_session.id, requester.conversation.id)

    with open_session(db) as click_session, open_session(db) as chat_session:
        clicker = requester_in(click_session, *ids)
        chatter = requester_in(chat_session, *ids)
        clicked = scheduling.confirm(
            click_session, requester=clicker, proposal_id=held["proposal_id"]
        )
        chatted = scheduling.confirm_change(
            chat_session, requester=chatter, proposal_id=held["proposal_id"]
        )
        via_tool = confirm_change(session, requester, held["proposal_id"])

    assert clicked["ok"] is True and clicked["replayed"] is False
    assert chatted["ok"] is True and chatted["replayed"] is True
    assert via_tool["ok"] is True and via_tool["replayed"] is True

    with open_session(db) as fresh:
        proposal = find_by_public_id(fresh, held["proposal_id"])
        groups = list(
            fresh.scalars(select(BookingGroup).where(BookingGroup.proposal_id == proposal.id))
        )
    assert len(groups) == 1
    assert len(replacements(db)) == 1
    assert len(active_for(db, world.patient.id)) == 1


# --------------------------------------------------------------------------- #
# Additional visits are additions, not replacements
# --------------------------------------------------------------------------- #


def test_an_explicit_additional_visit_keeps_the_existing_appointment(db, world_session):
    session, world, requester = world_session
    booking = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            "appointment_type": "cleaning",
        },
    )
    assert booking["ok"] is True, booking
    assert booking["intent"] == "new_booking"
    assert proposal_row(db, booking["proposal_id"]).target_appointment_id is None

    confirmed = call(
        session, requester, "confirm_booking_proposal", {"proposal_id": booking["proposal_id"]}
    )
    assert confirmed["ok"] is True, confirmed
    assert confirmed["kind"] == "booking"
    assert confirmed["released_appointment_ids"] == []

    # The October 27 cleaning is untouched, and both visits are active.
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_claims(db, world.original.id) == 2
    assert len(active_for(db, world.patient.id)) == 2
    assert replacements(db) == []


def test_adding_a_visit_after_a_confirmed_move_keeps_both(db, world_session):
    session, world, requester = world_session
    held = move(session, requester, world)
    moved = confirm_change(session, requester, held["proposal_id"])
    replacement_id = moved["appointments"][0]["appointment_id"]

    extra = call(
        session,
        requester,
        "create_booking_proposal",
        {
            "slot_ids": [world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME)],
            "appointment_type": "cleaning",
        },
    )
    assert extra["ok"] is True, extra
    added = call(
        session, requester, "confirm_booking_proposal", {"proposal_id": extra["proposal_id"]}
    )
    assert added["ok"] is True, added
    added_id = added["appointments"][0]["appointment_id"]

    assert get(db, replacement_id).status == AppointmentStatus.BOOKED.value
    assert get(db, world.original.id).status == AppointmentStatus.RESCHEDULED.value
    assert added["released_appointment_ids"] == []
    assert set(active_for(db, world.patient.id)) == {replacement_id, added_id}


def test_a_second_move_supersedes_the_first_card_and_moves_once(db, world_session):
    session, world, requester = world_session
    first = move(session, requester, world)
    second = move(session, requester, world, ALTERNATE_TIME)

    # Only the newest replacement stays live; the first hold is released.
    assert pending(db, requester.conversation.id) == [
        (second["proposal_id"], "reschedule", "reschedule")
    ]
    assert slot_is_free(db, world.slot(REPLACEMENT_DAY, NEW_TIME).id)

    stale = confirm_change(session, requester, first["proposal_id"])
    assert stale["ok"] is False
    assert stale["appointment_unchanged"] is True
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value

    confirmed = confirm_change(session, requester, second["proposal_id"])
    assert confirmed["ok"] is True, confirmed
    assert len(active_for(db, world.patient.id)) == 1
    assert replacements(db)[0].starts_at == datetime.combine(REPLACEMENT_DAY, ALTERNATE_TIME)


# --------------------------------------------------------------------------- #
# Searching keeps the move's context; listing names it
# --------------------------------------------------------------------------- #


def test_searching_while_a_move_is_pending_returns_reschedule_cards(db, world_session):
    session, world, requester = world_session
    move(session, requester, world)

    result = call(
        session,
        requester,
        "find_replacement_times",
        {"appointment_type": "cleaning", "start_date": REPLACEMENT_DAY.isoformat()},
    )
    assert result["ok"] is True, result
    assert result["kind"] == "reschedule"
    assert result["intent"] == "reschedule"
    assert result["target_appointment_id"] == world.original.id
    cards = [card for card in result["cards"] if card["type"] == "slot"]
    assert cards, result
    assert all(card["intent"] == "reschedule" for card in cards)
    assert all(card["target_appointment_id"] == world.original.id for card in cards)
    # The replacement run keeps the visit's own duration.
    assert all(card["duration_minutes"] == 60 for card in cards)


def test_ordinary_search_without_a_move_is_a_new_booking_search(db, world_session):
    session, world, requester = world_session
    result = call(
        session,
        requester,
        "search_availability",
        {"appointment_type": "cleaning", "start_date": REPLACEMENT_DAY.isoformat()},
    )
    assert result["ok"] is True, result
    assert result["intent"] == "new_booking"
    cards = [card for card in result["cards"] if card["type"] == "slot"]
    assert cards
    assert all(card["intent"] == "new_booking" for card in cards)


def test_replacement_search_without_a_target_says_so(db, world_session):
    session, world, requester = world_session
    result = call(
        session,
        requester,
        "find_replacement_times",
        {"appointment_type": "cleaning", "start_date": REPLACEMENT_DAY.isoformat()},
    )
    assert result["ok"] is False
    assert result["error"] == "no_active_booking"
    assert result["appointment_unchanged"] is True


def test_the_appointment_listing_names_the_pending_move(db, world_session):
    session, world, requester = world_session
    held = move(session, requester, world)
    listed = call(session, requester, "get_patient_appointments", {})
    assert listed["upcoming_count"] == 1
    assert listed["pending_reschedule"]["proposal_id"] == held["proposal_id"]
    assert listed["pending_reschedule"]["appointment_id"] == world.original.id
    assert listed["pending_reschedule"]["slot_id"] == world.stable_slot(
        REPLACEMENT_DAY, NEW_TIME
    )
    assert listed["pending_reschedule"]["confirm_with"] == "confirm_appointment_change"
