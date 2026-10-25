"""House style: the assistant never writes an em or en dash.

The model is told not to (``prompts``), but a prompt is a request. This suite
pins the deterministic half: whatever the model wrote is normalised before it is
stored or shown, and the stored copy matches what the patient saw.
"""

from __future__ import annotations

import pytest

from app.agent.reply_format import describe_slots, format_assistant_text, strip_dashes

EM = "\u2014"
EN = "\u2013"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # An aside becomes its own sentence.
        (f"Held for you {EM} nothing is booked yet.", "Held for you. Nothing is booked yet."),
        # A conjunction that belonged to the dash goes with it.
        (f"Held for you {EM} and nothing is booked.", "Held for you. Nothing is booked."),
        # En dashes used as an aside behave the same way.
        (f"Held for you {EN} nothing is booked yet.", "Held for you. Nothing is booked yet."),
        # A range keeps its meaning, with a plain hyphen.
        (f"9:00 AM {EN} 9:30 AM with Dr. Bell", "9:00 AM - 9:30 AM with Dr. Bell"),
        # A clause that already ended keeps its own punctuation.
        (f"Thanks, Dana. {EM} Your cleaning is booked.", "Thanks, Dana. Your cleaning is booked."),
        # Spacing without surrounding spaces is still a sentence break.
        (f"Booked{EM}see you soon", "Booked. See you soon"),
        # Nothing to change: the text is returned untouched.
        ("Your cleaning is booked for Tuesday.", "Your cleaning is booked for Tuesday."),
    ],
)
def test_strip_dashes_rewrites_every_dash(raw: str, expected: str) -> None:
    assert strip_dashes(raw) == expected


def test_strip_dashes_leaves_no_dash_of_either_kind() -> None:
    text = (
        f"One {EM} two {EN} three. A range 1{EN}2, a list {EM}and more {EN} far more."
    )
    cleaned = strip_dashes(text)
    assert EM not in cleaned
    assert EN not in cleaned
    # No doubled spaces or dangling punctuation left behind.
    assert "  " not in cleaned
    assert not cleaned.startswith((".", " ", "-"))
    assert not cleaned.endswith(("  ", " ."))


def test_plain_answers_are_normalised_by_the_formatter() -> None:
    """No cards: the formatter still applies house style without claiming a rewrite."""
    cleaned, reformatted = format_assistant_text(
        f"Your insurance is on file {EM} nothing else is needed.", None
    )
    assert cleaned == "Your insurance is on file. Nothing else is needed."
    assert reformatted is False


def test_card_intros_are_normalised_too() -> None:
    slots = [
        {
            "type": "slot",
            "starts_at": "2030-05-03T12:30",
            "appointment_type": "Cleaning",
            "provider": "Marion Varga",
        }
    ]
    cleaned, reformatted = format_assistant_text(
        f"Here are the available times {EM} which works for you?", slots
    )
    assert EM not in cleaned
    assert EN not in cleaned
    assert reformatted is True


def test_a_generated_intro_never_contains_a_dash() -> None:
    slots = [
        {
            "type": "slot",
            "starts_at": "2030-05-03T12:30",
            "appointment_type": "Cleaning",
            "provider": "Marion Varga",
        }
    ]
    for text in describe_slots(slots), describe_slots(slots * 2):
        assert EM not in text and EN not in text
