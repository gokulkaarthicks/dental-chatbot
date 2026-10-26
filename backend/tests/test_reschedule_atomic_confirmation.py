"""Intent separation: ``new_booking`` and ``reschedule`` are never interchangeable.

Companion to ``test_reschedule_workflow.py`` (the atomic move) and
``test_reschedule_regression.py`` (the workflow around it). Those two files drive
the tool layer, so they never reach the two guards that sit underneath it:

* ``create_proposal`` -- refuses a draft whose recorded ``intent`` disagrees with
  its ``kind``, a reschedule with no target, and a booking that names a target.
* ``_validate_semantics`` -- re-checks the same rules *inside* the confirmation
  transaction, so a proposal row edited after creation (or written by an older
  build) cannot retire an appointment by claiming to be something it is not.

Both guards exist because a booking that silently stands in for a move is the
October 27 -> October 29 bug. They are the last line of defence, so they get
their own file rather than being inferred from tool-level results.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Appointment, AppointmentStatus, SlotClaim
from app.services import scheduling
from app.services.proposals import (
    ConfirmationRefused,
    DraftItem,
    create_proposal,
    find_by_public_id,
)
from tests.test_reschedule_regression import (  # noqa: F401 - pytest fixtures
    ALTERNATE_TIME,
    NEW_TIME,
    active_claims,
    call,
    claim_count,
    confirm_change,
    move,
    pending,
    proposal_row,
)
from tests.test_reschedule_workflow import (  # noqa: F401 - pytest fixtures
    ORIGINAL_DAY,
    REPLACEMENT_DAY,
    active_for,
    db,
    get,
    open_session,
    requester_in,
    world_session,
)


def _draft(
    session,
    world,
    slot_stable_id: str,
    *,
    target_appointment_id: int | None = None,
) -> DraftItem:
    options = scheduling.options_for_slots(
        session,
        world.clinic,
        appointment_type=world.cleaning,
        slot_ids=[slot_stable_id],
        patient_id=world.patient.id,
    )
    assert options, "the fixture slot should be open and bookable"
    return DraftItem(
        patient_id=world.patient.id,
        patient_first_name="Dana",
        appointment_type=world.cleaning,
        option=options[0],
        target_appointment_id=target_appointment_id,
    )


# --------------------------------------------------------------------------- #
# create_proposal: the intent recorded on a new proposal must match its kind
# --------------------------------------------------------------------------- #


def test_a_booking_cannot_be_created_claiming_to_be_a_move(db, world_session):
    """The single change that caused the bug, refused at the source."""
    session, world, requester = world_session
    claims_before = claim_count(db)

    outcome = create_proposal(
        session,
        conversation_id=requester.conversation.id,
        items=[_draft(session, world, world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME))],
        kind="booking",
        intent="reschedule",
    )

    assert outcome.ok is False
    assert outcome.error == "intent_mismatch"
    # Nothing held, nothing pending, the original still the only booking.
    assert claim_count(db) == claims_before
    assert pending(db, requester.conversation.id) == []
    assert active_for(db, world.patient.id) == [world.original.id]


def test_a_booking_cannot_name_an_appointment_even_with_a_new_booking_intent(db, world_session):
    """Right kind and right intent still cannot point at a visit to retire."""
    session, world, requester = world_session

    outcome = create_proposal(
        session,
        conversation_id=requester.conversation.id,
        items=[
            _draft(
                session,
                world,
                world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME),
                target_appointment_id=world.original.id,
            )
        ],
        kind="booking",
        intent="new_booking",
    )

    assert outcome.ok is False
    assert outcome.error == "intent_mismatch"
    assert pending(db, requester.conversation.id) == []
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value


def test_a_reschedule_without_a_target_is_refused_at_creation(db, world_session):
    session, world, requester = world_session

    outcome = create_proposal(
        session,
        conversation_id=requester.conversation.id,
        items=[_draft(session, world, world.stable_slot(REPLACEMENT_DAY, ALTERNATE_TIME))],
        kind="reschedule",
        intent="reschedule",
    )

    assert outcome.ok is False
    assert outcome.error == "missing_target"
    assert pending(db, requester.conversation.id) == []


def test_a_move_of_an_existing_appointment_is_created_as_a_reschedule(db, world_session):
    """The same draft, labelled correctly, is accepted -- and still moves nothing yet."""
    session, world, requester = world_session

    outcome = create_proposal(
        session,
        conversation_id=requester.conversation.id,
        items=[
            _draft(
                session,
                world,
                world.stable_slot(REPLACEMENT_DAY, NEW_TIME),
                target_appointment_id=world.original.id,
            )
        ],
        kind="reschedule",
        intent="reschedule",
        target_appointment_id=world.original.id,
    )

    assert outcome.ok is True, outcome.error
    row = proposal_row(db, outcome.proposal_id)
    assert (row.kind, row.intent, row.target_appointment_id) == (
        "reschedule",
        "reschedule",
        world.original.id,
    )
    # Creating a move is still only a hold.
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert confirm_change(session, requester, outcome.proposal_id)["ok"] is True


# --------------------------------------------------------------------------- #
# _validate_semantics: the same rules, re-checked inside the transaction
# --------------------------------------------------------------------------- #


def _confirm_directly(db, world, requester, public_id: str):
    """Call the service layer the way the card button does, in a fresh session."""
    with open_session(db) as fresh:
        request = requester_in(
            fresh, world.patient.id, requester.patient_session.id, requester.conversation.id
        )
        return scheduling.confirm(fresh, requester=request, proposal_id=public_id)


def test_a_booking_relabelled_as_a_move_is_refused_at_confirmation(db, world_session):
    """A row changed after creation cannot retire the appointment it names."""
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

    with open_session(db) as fresh:
        row = find_by_public_id(fresh, booking["proposal_id"])
        row.target_appointment_id = world.original.id
        for item in row.items:
            item.target_appointment_id = world.original.id
        fresh.commit()

    result = _confirm_directly(db, world, requester, booking["proposal_id"])
    assert result["ok"] is False
    assert result["appointment_unchanged"] is True
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_claims(db, world.original.id) == 2
    assert active_for(db, world.patient.id) == [world.original.id]
    # Nothing was booked, so the replacement is still free.
    with open_session(db) as fresh:
        claim = fresh.scalar(
            select(SlotClaim).where(
                SlotClaim.availability_slot_id
                == world.slot(REPLACEMENT_DAY, ALTERNATE_TIME).id,
                SlotClaim.active.is_(True),
            )
        )
    assert claim is None


def test_a_move_relabelled_as_a_booking_is_refused_at_confirmation(db, world_session):
    """The mirror case: a move row saying ``new_booking`` cannot commit a booking."""
    session, world, requester = world_session
    held = move(session, requester, world)

    with open_session(db) as fresh:
        row = find_by_public_id(fresh, held["proposal_id"])
        row.kind = "booking"
        fresh.commit()

    result = _confirm_directly(db, world, requester, held["proposal_id"])
    assert result["ok"] is False
    assert result["appointment_unchanged"] is True
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_claims(db, world.original.id) == 2
    assert active_for(db, world.patient.id) == [world.original.id]


def test_a_move_without_a_target_is_refused_at_confirmation(db, world_session):
    """No target recorded: there is nothing to retire, so nothing is booked."""
    session, world, requester = world_session
    held = move(session, requester, world)

    with open_session(db) as fresh:
        row = find_by_public_id(fresh, held["proposal_id"])
        row.target_appointment_id = None
        for item in row.items:
            item.target_appointment_id = None
        fresh.commit()

    result = _confirm_directly(db, world, requester, held["proposal_id"])
    assert result["ok"] is False
    assert result["appointment_unchanged"] is True
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert active_for(db, world.patient.id) == [world.original.id]


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("kind", "booking"), ("intent", "new_booking")],
)
def test_validate_semantics_raises_for_a_mismatched_row(db, world_session, attribute, value):
    """The guard itself, independent of any HTTP or tool plumbing."""
    from app.services.proposals import _validate_semantics

    session, world, requester = world_session
    held = move(session, requester, world)

    with open_session(db) as fresh:
        row = find_by_public_id(fresh, held["proposal_id"])
        setattr(row, attribute, value)
        with pytest.raises(ConfirmationRefused):
            _validate_semantics(row)


def test_a_correctly_labelled_move_passes_the_guard(db, world_session):
    from app.services.proposals import _validate_semantics

    session, world, requester = world_session
    held = move(session, requester, world)

    with open_session(db) as fresh:
        row = find_by_public_id(fresh, held["proposal_id"])
        _validate_semantics(row)  # does not raise


# --------------------------------------------------------------------------- #
# The regression scenario, expressed through the intent labels only
# --------------------------------------------------------------------------- #


def test_the_october_27_visit_can_only_be_retired_by_a_reschedule(db, world_session):
    """One targeting rule, checked both ways, then the real move succeeds."""
    session, world, requester = world_session
    target_slots = [world.stable_slot(REPLACEMENT_DAY, NEW_TIME)]

    wrong = call(
        session,
        requester,
        "create_booking_proposal",
        {"slot_ids": target_slots, "appointment_type": "cleaning"},
    )
    assert wrong["ok"] is True
    # A plain booking targets nothing, so confirming it adds a visit.
    confirmed = call(
        session, requester, "confirm_booking_proposal", {"proposal_id": wrong["proposal_id"]}
    )
    assert confirmed["ok"] is True, confirmed
    assert get(db, world.original.id).status == AppointmentStatus.BOOKED.value
    assert len(active_for(db, world.patient.id)) == 2

    # The visit itself is then moved by a reschedule, and only that retires it.
    held = move(session, requester, world, ALTERNATE_TIME)
    assert held["kind"] == "reschedule"
    done = confirm_change(session, requester, held["proposal_id"])
    assert done["ok"] is True, done
    assert done["released_appointment_ids"] == [world.original.id]
    assert get(db, world.original.id).status == AppointmentStatus.RESCHEDULED.value

    with open_session(db) as fresh:
        rows = list(
            fresh.scalars(select(Appointment).where(Appointment.patient_id == world.patient.id))
        )
    retired = [row for row in rows if row.status == AppointmentStatus.RESCHEDULED.value]
    assert [row.rescheduled_to_id for row in retired] == [
        done["appointments"][0]["appointment_id"]
    ]
    assert ORIGINAL_DAY == retired[0].starts_at.date()
