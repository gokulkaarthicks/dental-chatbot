"""Format the assistant's prose so it never duplicates what the cards show.

The model is told not to enumerate times, but a prompt is a request, not a
guarantee. This module is the deterministic half of that rule:

* when a turn returns structured slot cards, any line of prose that reads like a
  time list ("1. 9:00 AM – 9:30 AM with Dr. Bell") is removed, and the intro is
  reduced to one short sentence;
* when the model only echoed the slots and left no usable intro, a concise
  deterministic intro is written from the cards themselves;
* hand-offs, policy answers, confirmations and every non-card turn are returned
  untouched, so ordinary conversational answers keep their natural language.

Nothing here invents facts: day, visit type, provider and duration all come from
the card payload the backend built from SQLite.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# "9:00 AM", "9:00am", "9:00", optionally followed by a range ("– 9:30 AM")
CLOCK = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?(?:\s*(?:-|–|—|to|until)\s*"
    r"\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?\b",
    re.IGNORECASE,
)
# A clock reading with minutes or a meridiem is unambiguous enough to flag.
STRONG_CLOCK = re.compile(
    r"\b\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?|\b\d{1,2}\s*[ap]\.?m\.?\b",
    re.IGNORECASE,
)
# A numbered line ("1. ...", "1) ...", "• ...", "- ...") is a list entry.
LIST_PREFIX = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*•])\s+")
# "Dr. Bell", "Dr. Naomi Bell": the surname is what the cards carry. Written as
# an explicit group rather than \b so the boundary before "Dr" cannot shift the
# capture onto the given name.
PROVIDER_NAME = re.compile(
    r"(?:^|[^A-Za-z])[Dd]r\.?\s+(?:[A-Z][A-Za-z'’-]*\s+)?([A-Z][A-Za-z'’-]*)"
)
LEADING_PROVIDER = re.compile(
    r"^[Dd]r\.?\s+(?:[A-Z][A-Za-z'’-]*\s+)?[A-Z][A-Za-z'’-]*\s+(?:has|is|can|offers|works|sees)\b[^.]*?[.;,]\s*"
)
WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
MAX_INTRO_CHARS = 240
# After sentence-ending punctuation and a space, a lowercase word is raised.
# Digits, quotes, and capitals are left alone.
SENTENCE_START = re.compile(r"([.?!]\s+)([a-z])")
# "9:00 AM – 9:30 AM": the dash joins two clock times, never two sentences.
CLOCK_RANGE = re.compile(
    r"(?<=\d)(\s*[ap]\.?m\.?)?\s*[\u2014\u2013]+\s*", re.IGNORECASE
)

# House style: the assistant never writes a dash of the em/en variety. A prompt
# instruction alone is a request, so the output is normalised deterministically.
_DASH = "\u2014\u2013"
_DASH_RANGE = re.compile(rf"(?<=\d)\s*[{_DASH}]+\s*(?=\d)")
# An em dash joining clauses reads as a sentence break: "held — nothing" ->
# "held. Nothing". Some writers put a conjunction right after the dash, which
# must go with it ("held — and nothing" -> "held. Nothing").
_DASH_BREAK = re.compile(
    rf"\s*[{_DASH}]+\s*(?:(?:and|but|so|then|yet)\s+)?",
    re.IGNORECASE,
)
# A sentence that already ended keeps its punctuation; only the dash is dropped.
_DECLARED_END = re.compile(r"[.?!:;,]$")


def _month_day(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}"


def _weekday(value: date) -> str:
    return WEEKDAY_NAMES[value.weekday()]


def _and_list(items: list[str]) -> str:
    """"A", "A and B", "A, B, and C"."""
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _parse_day(slot: dict[str, Any]) -> date | None:
    raw = slot.get("starts_at") or slot.get("day")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def _mentioned_weekdays(text: str) -> set[int]:
    found: set[int] = set()
    lowered = text.lower()
    for index, name in enumerate(WEEKDAY_NAMES):
        if name.lower() in lowered or name.lower()[:3] in lowered:
            found.add(index)
    return found


def describe_slots(slots: list[dict[str, Any]], *, refreshed: bool = False) -> str:
    """One-sentence introduction built from the cards, e.g.

    "Here are the available cleaning appointments for Tuesday, October 27.
    Which works for you?"

    A grid is one request with alternatives, so the plural is about the visit,
    not the number of times shown: three cleaning times are still "cleaning
    appointments", never three appointments the patient would book.

    ``refreshed`` is set when the patient asked to see the times again. The
    intro then says so with the date, because "here are the times" over a grid
    the patient has already been shown reads as if nothing happened.
    """
    days = sorted({day for day in (_parse_day(slot) for slot in slots) if day is not None})
    call = slots[0] if slots else {}
    visit = str(call.get("appointment_type") or "appointment").strip().lower()
    moving = any(slot.get("intent") == "reschedule" for slot in slots)

    plural = "appointments"
    if not days:
        when = ""
    elif len(days) == 1:
        when = f" for {_weekday(days[0])}, {_month_day(days[0])}"
    elif len(days) <= 3:
        when = " for " + _and_list([f"{_weekday(day)}, {_month_day(day)}" for day in days])
    else:
        when = f" for {_weekday(days[0])}, {_month_day(days[0])}, and the days after"

    if moving:
        if refreshed:
            return f"Just refreshed. Here are the times I can move it to{when}. Which works for you?"
        return f"Here are times I can move it to{when}. Which works for you?"
    # One time is not a choice: it is named with its day and asked about as one.
    if len(slots) == 1 and not refreshed:
        return f"Here is an available {visit}{when}. Does that work for you?"
    if len(slots) == 1:
        return (
            f"Just refreshed. Here is an available {visit}{when}. Does that work for you?"
        )
    if refreshed:
        return f"Just refreshed. Here are the available {visit} {plural}{when}. Which works for you?"
    return f"Here are the available {visit} {plural}{when}. Which works for you?"


def describe_intro_shape(slots: list[dict[str, Any]]) -> str:
    """The question the cards themselves carry: "Which works for you?" or not.

    ``format_assistant_text`` keeps the model's statement but replaces its
    question with this one, so the day is said once and asked about once.
    """
    if len(slots) == 1 and not any(slot.get("intent") == "reschedule" for slot in slots):
        return "Does that work for you?"
    return "Which works for you?"


# One question per turn: the model's own ask is replaced by the card-derived
# one, so a reply never ends with two different "which?"s.
ASKING_SENTENCE = re.compile(
    r"[^.?!]*(?:\bwhich (?:one |time |slot )?(?:works|fits)|works for you|"
    r"does that work|tap (?:one|any|the card)|pick (?:one|a time))[^.?!]*[.?!]",
    re.IGNORECASE,
)
# Prose that points at times already rendered, with nothing new to say.
POINTS_AT_GRID = re.compile(
    r"\b(?:cards? (?:above|there)|on screen|above|those times|these times|"
    r"the times (?:listed|shown))\b",
    re.IGNORECASE,
)
REASK_CLAUSE = re.compile(
    r"\b(?:ask|tell) me again\b[^.]*?\.|\b(?:refresh|check) (?:it|again)\b[^.]*?\.",
    re.IGNORECASE,
)
# "I can't show you times on this screen right now. I'll have someone call."
REFUSAL = re.compile(r"\b(?:can't|cannot|unable to|sorry)\b", re.IGNORECASE)
# A sentence that already introduces the times ("Just refreshed the list.") is
# what the card-derived question should follow, not replace.
LEADS_INTO_GRID = re.compile(
    r"\b(?:refresh\w*|here|these|below|options?|available|open times?|updated)\b",
    re.IGNORECASE,
)


def settled_intro(slots: list[dict[str, Any]]) -> str:
    """What to say when a grid belongs to a request that is already settled.

    A settled grid means one of two things, and the wording must match: a visit
    is booked, or a time is being held and one click away from booked. Saying
    "you already have a booking" for a mere hold is wrong and confuses the
    patient about whether they are done.
    """
    settled = [slot for slot in slots if slot.get("settled")]
    if not settled:
        return ""
    held_only = all(slot.get("settled_by") == "hold" for slot in settled)
    if held_only:
        return (
            "I'm holding the time you picked, so these other times are no longer on "
            "offer for this visit. Confirm the card above to book it, or ask me for a "
            "separate visit."
        )
    return (
        "You already have a booking for this request, so I've stopped offering these "
        "other times. Ask me if you want to move it or add a separate visit."
    )


def _is_time_echo(line: str) -> bool:
    """A line that mainly restates an appointment time the cards already show.

    A line is an echo when it *starts* with a time ("9:00 AM with Dr. Bell",
    "9:00 AM – 9:30 AM") or is a list entry carrying one ("1. 9:00 AM ...",
    "- 9:30 AM with Dr. Bell"). A sentence that merely mentions a time in
    passing ("I can't do 9:00 AM, but 10:00 works") is conversation and stays.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if CLOCK.match(stripped):
        return True
    text = LIST_PREFIX.sub("", stripped)
    if not text:
        return False
    if LIST_PREFIX.match(stripped):
        return bool(STRONG_CLOCK.search(text) or CLOCK.search(text))
    return bool(STRONG_CLOCK.search(text)) and len(text.split()) <= 8


def _sentence_case(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    return text[0].upper() + text[1:]


def _trim_to_sentence(text: str) -> str:
    """Keep only the first sentence when it ends reasonably."""
    match = re.match(r"^(.+?[.?!])(?:\s|$)", text.strip())
    if match:
        return match.group(1).strip()
    return text.strip()


def strip_dashes(text: str) -> str:
    """Remove em and en dashes from patient-facing prose.

    The clinic writes plain sentences: no "—" and no "–". This is the last
    defence before text is shown, so a model that ignores the style rule cannot
    put one on screen. It only rewrites the two dash characters (and the spaces
    around them); every other character is left exactly as written.
    """
    if not text or not any(dash in text for dash in _DASH):
        return text

    def _break(match: re.Match[str]) -> str:
        # The clause before the dash already ended: keep its punctuation.
        if _DECLARED_END.search(text[: match.start()].rstrip()):
            return " "
        return ". "

    normalised = _DASH_RANGE.sub("-", text)
    # 9:00 AM - 9:30 AM is a range too; the dash only reads as a break between
    # two digits, which is preceded by "AM" here.
    normalised = CLOCK_RANGE.sub(
        lambda match: f"{match.group(1) or ''} - ", normalised
    )
    normalised = _DASH_BREAK.sub(_break, normalised)
    # A new sentence starts with a capital ("held — nothing" -> "held.
    # Nothing"). Words the text already capitalises (names, "Dr.") are left
    # exactly as written.
    normalised = SENTENCE_START.sub(
        lambda match: match.group(1) + match.group(2).upper(), normalised
    )
    # Any dash left (leading, trailing, doubled) is simply not written.
    return "".join(" " if char in _DASH else char for char in normalised).strip()


def format_assistant_text(
    text: str,
    cards: list[dict[str, Any]] | None,
    *,
    refreshed: bool = False,
) -> tuple[str, bool]:
    """Return ``(prose, changed)`` for one assistant turn.

    ``changed`` is True when card-driven formatting was applied, which the runner
    records so the behaviour is inspectable in logs without logging content.

    The intro must describe the cards that are shown with it. A model intro that
    moves the visit onto a day its grid does not contain is dropped rather than
    kept: an available time under a sentence naming another day reads as a
    mistake and invites the patient to accept the wrong date.
    """
    slots = [
        card
        for card in (cards or [])
        if isinstance(card, dict) and card.get("type") == "slot"
    ]
    if not slots:
        return strip_dashes((text or "").strip()), False

    if any(slot.get("settled") for slot in slots):
        # The request already has a booking: offering "available times" would
        # invite a second one the server will refuse. Said before anything the
        # model wrote is considered.
        return settled_intro(slots) or "", True

    # The intro to fall back on: built only from the cards.
    card_intro = describe_slots(slots, refreshed=refreshed)
    # An intro that has lost its day ("Here are the available times") is still
    # built from the cards, but the statement above it is the model's own and
    # is kept: it is only ever shown *with* this grid.
    if refreshed:
        return describe_slots(slots, refreshed=True), True

    original = (text or "").strip()
    kept: list[str] = []
    for raw_line in re.split(r"\n+", original):
        line = raw_line.strip()
        if not line:
            continue
        if _is_time_echo(line):
            continue
        kept.append(LIST_PREFIX.sub("", line))

    prose = " ".join(kept).strip()
    # Never leave a dangling lead-in like "Here are the options:".
    if prose.endswith((":", "—", "-", ",")):
        prose = prose.rstrip(":—-, ")

    if prose and not any(
        word in prose.lower() for word in ("here", "these", "below", "option", "available")
    ):
        # Strip a leading "Dr. X has ...", which reads oddly after a lead-in.
        stripped = LEADING_PROVIDER.sub("", prose)
        # Nothing but a pleasantry/preamble is left: the cards say it better.
        if len(re.sub(r"[^A-Za-z]+", "", stripped)) < 12 or not stripped:
            return card_intro, True
        prose = _sentence_case(stripped)
        prose = "Here are the available times. " + prose

    if not prose:
        return card_intro, True

    # "Those times are no longer on screen. Ask me again and I'll check." is the
    # UI's own fallback for a lost grid, and it must never be stored under a
    # grid that is right there: the patient is sent to ask for what they are
    # already looking at. A refusal sentence is dropped for the same reason.
    if REFUSAL.search(prose):
        return card_intro, True

    # "Those times are no longer on screen. Ask me again and I'll check." is the
    # UI's own fallback for a lost grid, and it must never be stored under a
    # grid that is right there: the patient is sent to ask for what they are
    # already looking at. Only the offending sentence goes, so the rest of the
    # turn (a question about the day, say) survives.
    kept_lines: list[str] = []
    for line in re.split(r"(?<=[.?!])\s+", prose):
        if POINTS_AT_GRID.search(line) or REASK_CLAUSE.search(line):
            continue
        kept_lines.append(line)
    prose = " ".join(kept_lines).strip()
    if not prose:
        return card_intro, True

    # The cards answer "which one?" themselves. A statement above them is the
    # intro, kept as written; the card-derived question becomes the turn's only
    # question. Two questions in one message is nobody's idea of a receptionist.
    asked = ASKING_SENTENCE.search(prose)
    card_question = describe_intro_shape(slots)
    if asked is not None and not LEADS_INTO_GRID.search(prose):
        return card_intro, True
    if asked is not None:
        prose = prose[: asked.start()].strip()
        if not prose:
            return card_intro, True
        if not prose.endswith((".", "?", "!")):
            prose = prose + "."
        return _sentence_case(strip_dashes(f"{prose} {card_question}")), True

    # A wrong introduction is worse than a generic one: drop it when it names a
    # day the cards are not on, names several days, or names a dentist who has
    # no slot in this grid.
    card_days = {day.weekday() for day in (_parse_day(slot) for slot in slots) if day}
    mentioned = _mentioned_weekdays(prose)
    # Surnames in the grid: "Dr. Naomi Bell" -> {"bell", "naomi"} (the given name
    # is tolerated because a model may write "Dr. Bell" or "Dr. Naomi").
    tiles = {
        word.strip(".,").lower()
        for slot in slots
        for word in str(slot.get("provider") or "").split()[1:]
    }
    wrong_provider = any(
        match.group(1).strip(".,").lower() not in tiles
        for match in PROVIDER_NAME.finditer(prose)
    )
    if (
        (mentioned and card_days and mentioned.isdisjoint(card_days))
        or len(mentioned) > 1
        or wrong_provider
    ):
        return card_intro, True

    if len(prose) > MAX_INTRO_CHARS:
        prose = _trim_to_sentence(prose)
        if len(prose) > MAX_INTRO_CHARS:
            return card_intro, True
    elif not prose.endswith((".", "?", "!")):
        prose = prose + "."

    return _sentence_case(strip_dashes(prose)), True


__all__ = [
    "describe_slots",
    "format_assistant_text",
    "settled_intro",
    "strip_dashes",
]
