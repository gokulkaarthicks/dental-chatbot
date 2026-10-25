"""Searching again after the patient turns a held time down.

The regression: a patient picked a time (a hold was created), then said they
wanted something else. The next search answered with the same held card, so the
only path forward was "Change", and a reader following along saw one set of
times offered again and again with no way to accept them.

Pins the rule the patient sees: their own decline is an answer. After it, a
search finds current times; before it, a waiting hold is still shown back, so a
search cannot silently replace a booking that is one click away.
"""

from __future__ import annotations

from conftest import SEARCH_PHRASE

from app.services import scheduling
from app.services.scheduling import Requester


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


def test_a_waiting_hold_is_still_shown_back_before_the_patient_declines(scenario) -> None:
    session, requester = scenario
    first = _search(session, requester, [SEARCH_PHRASE])
    held = _hold(session, requester, first)
    session.commit()
    assert held.get("proposal_id")

    again = _search(session, requester, [SEARCH_PHRASE])
    # The hold is one click from booked: searching must not replace it.
    assert again.payload["error"] == "pending_booking_exists"
    assert again.options == []
    assert again.payload["pending_proposal_id"] == held["proposal_id"]


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
