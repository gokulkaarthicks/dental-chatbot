"""Searching again after the patient turns a held time down.

The regression: a patient picked a time (a hold was created), then said they
wanted something else. The next search answered with the same held card, so the
only path forward was "Change", and a reader following along saw one set of
times offered again and again with no way to accept them.

Pins the rule the patient sees: asking for other times is an answer. A search
hands the held time back at once -- whether the patient pressed "Change" or
simply asked again -- so the calendar is free and the grid shows real times.
The card itself stays confirmable; ending the reservation is not a decline, and
the time can still be confirmed if nobody took it.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import SEARCH_PHRASE
from sqlalchemy import select

from app.models import SlotClaim, utcnow
from app.services import scheduling
from app.services.proposals import find_by_public_id, release_expired_holds
from app.services.scheduling import Requester


def active_holds_for(session, public_id: str) -> int:
    proposal = find_by_public_id(session, public_id)
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


def open_session():
    """A second connection, as another request would use."""
    from app import db as db_module

    return db_module.new_session()


def _search(session, requester: Requester, phrases: list[str]):
    return scheduling.find_availability(
        session,
        patient=requester.patient,
        appointment_type_code="cleaning",
        date_phrases=phrases,
        requester=requester,
    )


def _hold(session, requester: Requester, payload):
    assert payload.options, payload.payload
    return scheduling.propose_booking(
        session,
        requester=requester,
        appointment_type=payload.appointment_type,
        options=payload.options[:1],
        from_ui=True,
    )


def test_a_waiting_hold_is_released_when_the_patient_asks_for_other_times(scenario) -> None:
    """A search after a hold is the patient moving off it, so it is handed back.

    Previously the answer was the held card itself ("pending_booking_exists"),
    which kept the time reserved for the whole window -- that is the bug: a
    click that is reconsidered must not block the calendar. The card stays
    confirmable; only its reservation ends.
    """
    session, requester = scenario
    first = _search(session, requester, [SEARCH_PHRASE])
    held = _hold(session, requester, first)
    session.commit()
    assert held.get("proposal_id")

    again = _search(session, requester, [SEARCH_PHRASE])
    assert again.payload.get("available") is True, again.payload
    assert again.options, "the freed time must come back in the new grid"
    assert not any(card.get("settled") for card in again.payload["cards"])

    assert active_holds_for(session, held["proposal_id"]) == 0
    assert scheduling._pending_booking(session, requester) is None


def test_a_lapsed_hold_still_lets_the_patient_confirm_the_time(scenario) -> None:
    """Releasing the reservation never throws the choice away."""
    session, requester = scenario
    first = _search(session, requester, [SEARCH_PHRASE])
    held = _hold(session, requester, first)
    session.commit()

    with open_session() as other:
        row = find_by_public_id(other, held["proposal_id"])
        row.expires_at = utcnow() - timedelta(seconds=1)
        other.commit()

    with open_session() as reading:
        assert release_expired_holds(reading, requester.conversation.id) == [
            held["proposal_id"]
        ]
        reading.commit()

    assert active_holds_for(session, held["proposal_id"]) == 0
    result = scheduling.confirm(
        session, requester=requester, proposal_id=held["proposal_id"]
    )
    assert result["ok"] is True, result
    assert result["confirmed"] is True


def test_after_a_decline_the_search_returns_current_times(scenario) -> None:
    session, requester = scenario
    first = _search(session, requester, [SEARCH_PHRASE])
    held = _hold(session, requester, first)
    scheduling.decline(session, requester=requester, proposal_id=held["proposal_id"])

    again = _search(session, requester, [SEARCH_PHRASE])
    assert again.payload.get("available") is True, again.payload
    slots = [card for card in again.payload["cards"] if card.get("type") == "slot"]
    assert slots, "a decline must let the next search answer with real times"
    assert all(card.get("settled") is False for card in slots)
    assert not any(card.get("type") == "proposal" for card in again.payload["cards"])

def test_confirming_a_lone_hold_does_not_read_a_missing_column(scenario) -> None:
    """Regression: the supersede guard queried ``BookingProposal.held_until``,
    a column that does not exist, so every solo booking confirmation raised
    AttributeError and surfaced as HTTP 500. Holds live in ``slot_claims``.
    """
    session, requester = scenario
    first = _search(session, requester, [SEARCH_PHRASE])
    held = _hold(session, requester, first)
    session.commit()
    assert held.get("proposal_id")

    result = scheduling.confirm(
        session,
        requester=requester,
        proposal_id=held["proposal_id"],
        actor="ui",
    )
    assert result["ok"] is True, result
    assert result["confirmed"] is True
    assert any(card.get("type") == "confirmation" for card in result["cards"])
