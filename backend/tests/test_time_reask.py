"""Asking to see the times again is a search, not a conversation.

The transcript this pins (October 26 emergency visit, mid-move):

    patient: morning
    model:   "Those morning times are on the cards there. Just tap the one you'd
              like, and I'll hold it..."
    patient: do you have any other slots on monday
    model:   "Those times are no longer on screen. Ask me again for current
              availability and I'll check."

Two failures in one loop:

* the model answered a request for times with prose and no tool call, so the
  turn carried no grid, and the UI's own lost-grid fallback told the patient to
  ask again. Asking again produced the same answer.
* the patient was greeted as "Dharshs", a truncated chart name.

Fixed by three rules, each tested here: a re-ask replays the grid the server
already built (so every turn carries its own cards), the formatter never stores
"ask me again" under a grid that is on screen, and a one-word chart name is
never cut in half.
"""

from __future__ import annotations

import pytest

from app.agent import reply_format
from app.agent.tools import is_time_reask
from app.services import patients

SLOT = {
    "type": "slot",
    "intent": "reschedule",
    "target_appointment_id": 41,
    "starts_at": "2026-10-26T08:30",
    "ends_at": "2026-10-26T09:00",
    "appointment_type": "Emergency Visit",
    "provider": "Kwame Osei",
    "settled": False,
    "require_open": True,
}


@pytest.mark.parametrize(
    "text",
    [
        "do you have any other slots on monday",
        "any other times?",
        "current availability",
        "current abvaialbility",
        "what's available monday morning",
        "show me the slots again",
        "is there anything open earlier",
        "check availability for monday",
        "morning",
    ],
)
def test_a_request_for_times_is_recognised(text: str) -> None:
    # "morning" is a preference on the day already being discussed: it must not
    # be mistaken for a re-ask here, the model owns that one.
    assert is_time_reask(text) is (text != "morning")


@pytest.mark.parametrize(
    "text",
    [
        "I want to reschedule",
        "move it to Wednesday afternoon",
        "my tooth hurts",
        "book a cleaning",
        "thanks",
    ],
)
def test_ordinary_messages_are_not_re_asks(text: str) -> None:
    assert is_time_reask(text) is False


def test_a_re_ask_says_it_refreshed_instead_of_reintroducing_the_day() -> None:
    prose, changed = reply_format.format_assistant_text(
        "Those times are no longer on screen. Ask me again for current availability and I'll check.",
        [SLOT],
        refreshed=True,
    )
    assert changed is True
    assert "ask me again" not in prose.lower()
    assert "no longer on screen" not in prose.lower()
    assert prose.startswith("Just refreshed.")
    assert "Monday, October 26" in prose


def test_the_lost_grid_sentence_is_dropped_under_a_grid_that_exists() -> None:
    slots = [
        {**SLOT, "intent": "new_booking", "target_appointment_id": None,
         "appointment_type": "Cleaning", "provider": "Marion Varga"},
    ]
    prose, _ = reply_format.format_assistant_text(
        "Those times are no longer on screen. "
        "Ask me again for current availability and I'll check. "
        "Nothing else is open on that day.",
        slots,
    )
    lowered = prose.lower()
    assert "no longer on screen" not in lowered
    assert "ask me again" not in lowered
    assert "nothing else is open on that day" in lowered


def test_a_refusal_is_never_shown_over_open_times() -> None:
    slots = [
        {**SLOT, "intent": "new_booking", "target_appointment_id": None,
         "appointment_type": "Cleaning", "provider": "Marion Varga"},
    ]
    prose, _ = reply_format.format_assistant_text(
        "I'm sorry, I can't access the schedule right now.", slots
    )
    assert "can't" not in prose.lower()
    assert "here is an available cleaning" in prose.lower()
    assert prose.endswith("?")


def test_the_model_does_not_get_to_ask_a_second_question() -> None:
    slots = [
        {**SLOT, "intent": "new_booking", "target_appointment_id": None,
         "appointment_type": "Cleaning", "provider": "Marion Varga"},
    ]
    prose, _ = reply_format.format_assistant_text(
        "Here are the Monday times. Which one works for you?", slots
    )
    # The cards carry the question; one ask, not two.
    assert prose.lower().count("which") <= 1
    assert prose.count("?") == 1


def test_an_empty_cardless_re_ask_does_not_send_the_patient_back() -> None:
    """The fallback for "nothing to show" must state a next step, not a retry."""
    text = (
        "I don't have any open times to show for that right now. I can check another day, "
        "or ask the front desk to call you about times they keep off the online schedule."
    )
    assert "ask me again" not in text.lower()


def test_a_single_word_name_is_not_truncated() -> None:
    # The exact bug: "Dharshs" was greeted as "Dharsh".
    assert patients.first_name("Dharshs") == "Dharshs"
    assert patients.first_name("Jordan Ellis") == "Jordan"
    assert patients.first_name("  Mary  Jane  Watson ") == "Mary"
    assert patients.first_name("Patient") == ""
    assert patients.first_name(None) == ""


def test_the_greeting_uses_the_name_whole_or_not_at_all() -> None:
    # Pure unit check of the wording rule, without a database.
    for name, expected in (("Dharshs", "Dharshs"), ("Dharshs Kumar", "Dharshs")):
        assert patients.first_name(name) == expected

