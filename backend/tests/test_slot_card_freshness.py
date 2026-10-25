"""What a rendered slot card may claim about its own freshness.

The regression: the browser restored an old thread holding a grid, re-checked
it on mount, and every row went disabled ("superseded or taken") even though a
new search in chat showed the same times open. The re-check cannot tell which
grid is current, so the card has to say whether the turn that produced it
actually searched the schedule:

* a fresh search says so (``require_open``), and its answer re-validates or
  downsizes the grid;
* a card that only says a time was once offered (``settled``, or nothing after a
  restore) must never be read as "confirmed open now".
"""

from __future__ import annotations

from datetime import datetime

from app.services.scheduling import slot_card


class _Option:
    stable_id = "slot_1"
    starts_at = datetime(2030, 5, 7, 10, 0)
    ends_at = datetime(2030, 5, 7, 11, 0)
    provider_name = "Dr. Marion Varga"
    provider_credentials = "DDS"
    patient_first_name = None


class _Type:
    name = "Cleaning"
    code = "cleaning"
    duration_minutes = 60


def test_a_search_card_says_it_is_open_now() -> None:
    card = slot_card(_Option(), _Type())  # type: ignore[arg-type]
    assert card["require_open"] is True
    assert card["settled"] is False


def test_a_settled_card_never_claims_to_be_open() -> None:
    card = slot_card(_Option(), _Type(), settled=True)  # type: ignore[arg-type]
    assert card["require_open"] is False
    assert card["settled"] is True


def test_an_explicit_additional_visit_card_keeps_its_booking_intent() -> None:
    card = slot_card(_Option(), _Type(), additional_visit=True)  # type: ignore[arg-type]
    assert card["additional_visit"] is True
    assert card["settled"] is False
    assert card["require_open"] is True
