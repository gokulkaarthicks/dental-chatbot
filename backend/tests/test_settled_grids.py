"""A grid stops being an offer once its request is held or booked.

The bug these lock in: a stored slot card kept ``settled: false`` forever,
because the flag was computed only from *booked* visits at the moment the grid
was produced. After the patient clicked Select and kept talking, the old grid
stayed fully clickable above the proposal waiting for them, so a second hold
looked possible for a visit that was already one click from booked.
"""

from __future__ import annotations

from conftest import SEARCH_PHRASE

from app.services import scheduling


def _grid(slot_ids: list[str], **extra) -> list[dict]:
    return [
        {
            "type": "slot",
            "slot_id": slot_id,
            "settled": False,
            "require_open": True,
            **extra,
        }
        for slot_id in slot_ids
    ]


def test_a_plain_offer_is_not_settled_before_anything_is_held(scenario) -> None:
    session, requester = scenario
    cards = _grid(["a", "b"])

    assert scheduling.settles_plain_grids(session, requester.clinic, requester.conversation.id) == (
        False,
        None,
    )
    out = scheduling.settle_superseded_grids(
        session,
        requester.clinic, requester.conversation.id, cards
    )
    assert all(card["settled"] is False for card in out)
    # The caller's cards are untouched: this is a projection, not a mutation.
    assert all(card["settled"] is False for card in cards)


def test_a_held_proposal_retires_every_ordinary_grid(scenario) -> None:
    session, requester = scenario
    first = scheduling.find_availability(
        session,
        requester.clinic,
        patient=requester.patient,
        appointment_type_code="cleaning",
        date_phrases=[SEARCH_PHRASE],
        requester=requester,
    )
    assert first.options, first.payload
    held = scheduling.propose_booking(
        session,
        requester=requester,
        appointment_type=first.appointment_type,
        options=first.options[:1],
        from_ui=True,
    )
    session.commit()
    assert held.get("proposal_id"), held

    settled, reason = scheduling.settles_plain_grids(session, requester.clinic, requester.conversation.id)
    assert settled is True
    # Nothing is booked yet, so the reason must be the hold, not a booking:
    # the UI words these differently.
    assert reason == "hold"

    out = scheduling.settle_superseded_grids(
        session,
        requester.clinic, requester.conversation.id, _grid(["a", "b"])
    )
    assert all(card["settled"] is True for card in out)
    assert all(card["settled_by"] == "hold" for card in out)
    # A settled card must never claim it was just searched open.
    assert all(card["require_open"] is False for card in out)


def test_an_explicitly_requested_extra_visit_stays_bookable(scenario) -> None:
    session, requester = scenario
    first = scheduling.find_availability(
        session,
        requester.clinic,
        patient=requester.patient,
        appointment_type_code="cleaning",
        date_phrases=[SEARCH_PHRASE],
        requester=requester,
    )
    assert first.options, first.payload
    scheduling.propose_booking(
        session,
        requester=requester,
        appointment_type=first.appointment_type,
        options=first.options[:1],
        from_ui=True,
    )
    session.commit()

    extra = _grid(["a"], additional_visit=True)
    out = scheduling.settle_superseded_grids(
        session,
        requester.clinic, requester.conversation.id, extra
    )
    # Adding a separate visit is a different request; its times stay clickable.
    assert out[0]["settled"] is False
    assert "settled_by" not in out[0]


def test_a_held_proposal_retires_family_options_too(scenario) -> None:
    session, requester = scenario
    first = scheduling.find_availability(
        session,
        requester.clinic,
        patient=requester.patient,
        appointment_type_code="cleaning",
        date_phrases=[SEARCH_PHRASE],
        requester=requester,
    )
    assert first.options, first.payload
    held = scheduling.propose_booking(
        session,
        requester=requester,
        appointment_type=first.appointment_type,
        options=first.options[:1],
        from_ui=True,
    )
    session.commit()
    assert held.get("proposal_id"), held

    family = [
        {
            "type": "family_option",
            "option_id": "family:old",
            "settled": False,
            "require_open": True,
            "slots": _grid(["a", "b"]),
        }
    ]
    out = scheduling.settle_superseded_grids(
        session,
        requester.clinic, requester.conversation.id, family
    )

    assert out[0]["settled"] is True
    assert out[0]["require_open"] is False
    assert all(slot["settled"] is True for slot in out[0]["slots"])
