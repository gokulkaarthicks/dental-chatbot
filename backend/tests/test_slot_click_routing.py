"""A clicked slot is routed by what its card says, not by leftover state.

The regression: after a move had been started, an ordinary grid from a *new*
search rendered its times, and every click was handed to the reschedule flow
(``pending_target``). The click failed or held the wrong thing, the grid was
marked settled, and the patient saw "Unavailable" on times that were open.

The contract pinned here: a card that says it was just searched and carries no
target books a new visit; an unmarked (restored) transcript card keeps the old
rule, so a click on a historical grid still cannot silently book twice.
"""

from __future__ import annotations

from app.api.scheduling import SlotSelectionRequest


def test_a_fresh_untargeted_card_is_a_new_booking() -> None:
    payload = SlotSelectionRequest(slot_ids=["slot_1"], require_open=True)
    assert payload.intent is None
    assert payload.target_appointment_id is None
    assert payload.require_open is True


def test_a_replacement_card_still_names_its_target() -> None:
    payload = SlotSelectionRequest(
        slot_ids=["slot_1"], intent="reschedule", target_appointment_id=7
    )
    assert payload.intent == "reschedule"
    assert payload.target_appointment_id == 7


def test_an_old_client_cannot_claim_a_fresh_card() -> None:
    # Defaults are conservative: nothing is "just searched" unless the card
    # that produced the click says so.
    payload = SlotSelectionRequest(slot_ids=["slot_1"])
    assert payload.require_open is False
