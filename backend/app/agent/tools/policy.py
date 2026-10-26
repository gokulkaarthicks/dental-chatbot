"""Turn policy: which tools one model round may see, and when to withdraw them.

The graph asks this module one question per round. Three inputs decide the
answer: whether this turn is a re-ask, whether a search already ran, and whether
the stored scheduling request covers more than one person.

Two behaviours are pinned here because they are product rules, not model
choices:

* A natural-language re-ask is not a card selection. The model may extract new
  people or preferences, but it must not dispatch a hold or confirm tool based
  on an older option mentioned in the transcript.
* A household request must not be collapsed into a solo search just because the
  model omitted one of the people on a follow-up turn.

Visibility also mirrors what the session can actually do: a tool that refuses
for want of a verified patient, or of a complete registration, is not offered to
a round that cannot use it. And a booking is confirmed only when the patient's
own message says yes, so the confirmation tools are withdrawn from every other
turn.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from enum import StrEnum

from langchain_core.tools import BaseTool

from app.agent.tools.registry import ALL_TOOLS

SOLO_SEARCH_TOOL = "search_availability"

SEARCH_TOOLS = frozenset({"search_availability", "search_family_availability"})

# The tools that work before anyone is verified. This is the whole surface of an
# anonymous chat: orient, identify or register a patient, answer a question about
# the practice, report an emergency, ask for a human. Anything that reads or
# changes one patient's schedule needs the chart bound to the session first.
PUBLIC_TOOL_NAMES = frozenset(
    {
        "create_staff_request",
        "find_registration_duplicates",
        "get_practice_info",
        "get_practice_summary",
        "get_session_context",
        "register_patient",
        "report_emergency",
        "verify_existing_patient",
    }
)

# Tools that additionally need a complete registration before they can succeed.
# This is exactly the set carrying the ``_registration_gate`` check, so no turn
# ever loses an action it could have completed.
REGISTRATION_GATED_TOOL_NAMES = frozenset(
    {
        "search_availability",
        "find_replacement_times",
        "confirm_slot_selection",
    }
)

# The steps that turn a held time into a booking, or retire an existing visit.
# Everything else only prepares a choice; these spend it.
CONFIRM_TOOL_NAMES = frozenset(
    {
        "confirm_booking_proposal",
        "confirm_appointment_change",
    }
)

# Only these stay visible on a re-ask. Child registration remains available
# because a family re-ask may name a child who is not linked yet, and the
# context readers stay available so the model can orient itself.
REASK_TOOL_NAMES = frozenset(
    {
        "get_booking_context",
        "get_session_context",
        "register_child_for_verified_parent",
        "search_availability",
        "search_family_availability",
    }
)


class TurnMode(StrEnum):
    """The two shapes a patient turn can take."""

    DEFAULT = "default"
    REASK = "reask"


# A patient asking to see the times they were already shown. Spellings drift
# ("abvaialbility" is a real one from a demo), so each word is matched loosely
# rather than the whole phrase.
_FUZZY_AVAILABILITY = r"[a-z]*?i[a-z]*?i[a-z]*?y\b"
REASK_TIMES = re.compile(
    r"\b(?:any|other|more|another|different|else)\b[^?]{0,20}\b(?:slots?|times?|openings?)\b"
    rf"|\b(?:current|other|more|any) {_FUZZY_AVAILABILITY}\b"
    r"|\bwhat(?:'s| is) available\b|\banything (?:open|available|else)\b"
    r"|\b(?:show|see|check) (?:me )?(?:the )?(?:times|slots|availability)(?: again)?\b"
    r"|\b(?:refresh|refreshed)\b[^?]{0,20}\b(?:availability|times|slots)\b"
    r"|\b(?:another|different) (?:day|date)\b"
    r"|\bwhat (?:times|slots) do you have\b|\bshow me again\b",
    re.IGNORECASE,
)


def is_time_reask(text: str) -> bool:
    """Whether the patient asked to be shown times again, rather than a new day."""
    value = text or ""
    if REASK_TIMES.search(value):
        return True
    return any(
        len(word) >= 7 and SequenceMatcher(None, word.lower(), "availability").ratio() >= 0.72
        for word in re.findall(r"[a-z]+", value.lower())
    )


def is_alternative_day_request(text: str) -> bool:
    return bool(re.search(r"\b(?:another|different) (?:day|date)\b", text or "", re.IGNORECASE))


# The patient's own "yes", phrased the way people phrase it in a chat. Deliberately
# idiomatic rather than a single polite word: "ok" on its own is often agreement to
# a question that is not about a booking at all, and it is not evidence on its own.
_AFFIRMATION = re.compile(
    r"\b(?:yes|yep|yup|yeah|confirmed|confirm|sounds good|sound good|that works|"
    r"works for me|this works|go ahead|go for it|book it|book that|do it|i'll take it|"
    r"ill take it|take that|perfect|great|absolutely|definitely|certainly|please do|"
    r"agreed|i agree|i approve|i want (?:it|that|this|to book)|"
    r"that(?:'s| s) fine|fine by me|that(?:'s| s) okay|that(?:'s| s) ok)\b",
    re.IGNORECASE,
)
# "I'm not sure that works" is not an agreement, so the words just before the
# idiom decide, up to the clause boundary in front of it.
_NEGATION = re.compile(r"\b(?:not|no|never|neither|cannot|can'?t|dont|don'?t|wont|won'?t)\b", re.IGNORECASE)
_CLAUSE_BREAKS = (".", ";", ",")


def is_explicit_affirmation(text: str) -> bool:
    """Whether this turn's patient message carries an agreement of its own.

    The graph uses this as the server's answer to "did the patient confirm?". The
    model's summary of the conversation is never evidence: only the words the
    patient just typed are, so a booking can be spent on the patient's own
    agreement and not on a paraphrase.
    """
    value = text or ""
    for match in _AFFIRMATION.finditer(value):
        before = value[max(0, match.start() - 40):match.start()]
        for break_marker in _CLAUSE_BREAKS:
            before = before.rsplit(break_marker, 1)[-1]
        if _NEGATION.search(before):
            continue
        return True
    return False


def is_household(request: dict | None) -> bool:
    """Whether the stored request covers more than one person."""
    if not request:
        return False
    participants = request.get("participants") or []
    return isinstance(participants, list) and len(participants) > 1


def turn_mode(*, reask: bool, family_option_identity: str | None) -> TurnMode:
    """Which policy governs this turn.

    A click is an action on a specific option, so it is never treated as a
    request to see a fresh grid, even when the accompanying text reads like one.
    """
    if reask and family_option_identity is None:
        return TurnMode.REASK
    return TurnMode.DEFAULT


def visible_tools(
    *,
    mode: TurnMode,
    searched: bool,
    exhausted: bool,
    household: bool,
    verified: bool = True,
    registration_complete: bool = True,
    may_confirm: bool = True,
) -> tuple[BaseTool, ...]:
    """The tools one model round may see.

    A re-ask that has already searched gets no further tools, and so does a turn
    whose search came back complete but empty: asking again cannot change the
    answer and only risks a loop.

    The rest of the surface follows the session's own state, so the model is
    never offered a call that can only come back as a refusal, and never offered
    a confirmation it has no authority to spend.
    """
    if exhausted or (mode is TurnMode.REASK and searched):
        return ()
    if mode is TurnMode.REASK:
        # A re-ask is a request to see the times again. It carries no agreement
        # to spend a hold, and the re-ask set names no confirmation tool.
        names = set(REASK_TOOL_NAMES)
        if household:
            names.discard(SOLO_SEARCH_TOOL)
        if not verified:
            names &= PUBLIC_TOOL_NAMES
        return tuple(tool for tool in ALL_TOOLS if tool.name in names)

    withheld: set[str] = set()
    if not verified:
        withheld |= {tool.name for tool in ALL_TOOLS} - PUBLIC_TOOL_NAMES
    elif not registration_complete:
        withheld |= REGISTRATION_GATED_TOOL_NAMES
    if not may_confirm:
        withheld |= CONFIRM_TOOL_NAMES
    if not withheld:
        return ALL_TOOLS
    return tuple(tool for tool in ALL_TOOLS if tool.name not in withheld)


__all__ = [
    "CONFIRM_TOOL_NAMES",
    "PUBLIC_TOOL_NAMES",
    "REGISTRATION_GATED_TOOL_NAMES",
    "REASK_TOOL_NAMES",
    "REASK_TIMES",
    "SEARCH_TOOLS",
    "SOLO_SEARCH_TOOL",
    "TurnMode",
    "is_alternative_day_request",
    "is_explicit_affirmation",
    "is_household",
    "is_time_reask",
    "turn_mode",
    "visible_tools",
]
