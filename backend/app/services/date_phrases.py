"""Relative and explicit date phrases, resolved deterministically.

Every phrase is resolved against "clinic today" in ``America/Los_Angeles``.
Nothing here guesses: phrases with two defensible readings return
``ambiguous=True`` with a clarification the assistant must voice, and phrases
that fall in the past are reported as rejected rather than rolled forward.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.timeutil import SchedulingConfig

WEEKDAY_NAMES: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
WEEKDAY_INDEX: dict[str, int] = {name: index for index, name in enumerate(WEEKDAY_NAMES)}

_MONTHS: dict[str, int] = {
    name.lower(): index for index, name in enumerate(calendar.month_name) if name
}
_MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
_MONTHS["sept"] = 9


@dataclass
class PhraseResult:
    """Outcome of resolving one date phrase."""

    days: list[date] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ambiguous: bool = False
    clarification: str | None = None
    past: bool = False
    unreadable: bool = False
    weekday_filter: set[int] | None = None
    scan_days: int | None = None


_FLEXIBLE_PHRASE = re.compile(
    r"^(?:"
    r"any(?:\s*time|\s+day(?:\s+(?:works|is\s+fine|is\s+ok(?:ay)?|is\s+good))?)?"
    r"|whenever(?:\s+(?:works|possible|is\s+fine))?"
    r"|flexible"
    r"|doesn'?t matter"
    r"|no preference"
    r"|i(?:'m| am) fine with any(?:\s+day)?"
    r"|any day (?:to|for) schedule"
    r"|i am fine with any day(?:\s+to\s+schedule)?"
    r")$",
    re.IGNORECASE,
)


def _is_flexible_phrase(text: str) -> bool:
    return bool(_FLEXIBLE_PHRASE.fullmatch(text.strip()))


def weekday_index(raw: str | None) -> int | None:
    """Accept full names, abbreviations, and plurals ("tuesdays")."""
    if not raw:
        return None
    cleaned = raw.strip().lower().rstrip(".")
    if cleaned.endswith("s") and cleaned[:-1] in WEEKDAY_INDEX:
        cleaned = cleaned[:-1]
    if cleaned in WEEKDAY_INDEX:
        return WEEKDAY_INDEX[cleaned]
    for name, index in WEEKDAY_INDEX.items():
        if name.startswith(cleaned) and len(cleaned) >= 3:
            return index
    return None


def _week_bounds(today: date) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def _span(start: date, days: int) -> list[date]:
    return [start + timedelta(days=offset) for offset in range(max(0, days))]


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_from(word: str) -> int | None:
    return _MONTHS.get(word.strip().lower().rstrip("."))


def _month_day_pair(text: str) -> tuple[int, int] | None:
    """'October 5', 'Oct 5th', '5th of October', '5 October' -> (month, day)."""
    first = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?", text)
    if first:
        month = _month_from(first.group(1))
        if month:
            return month, int(first.group(2))
    second = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\.?", text)
    if second:
        month = _month_from(second.group(2))
        if month:
            return month, int(second.group(1))
    return None


def _resolve_explicit(text: str, today: date) -> PhraseResult:
    """ISO, slash, written-month, and ordinal-only dates.

    Every pattern is fully anchored: an unanchored ``\\d{4}`` would match the year
    inside "march 5 2026" and be misread as an ISO date.
    """
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        parsed = _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        if parsed is None:
            return PhraseResult(warnings=["That date doesn't exist on the calendar."], unreadable=True)
        return _only_future([parsed], today)

    slash = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", text)
    if slash:
        first, second = int(slash.group(1)), int(slash.group(2))
        # A component above 12 cannot be a month, so the order is unambiguous.
        if first > 12 and second <= 12:
            month, day = second, first
            ambiguous = False
        else:
            month, day = first, second
            ambiguous = first <= 12 and second <= 12 and first != second
        if month > 12 or day > 31:
            return PhraseResult(warnings=["That doesn't look like a valid date."], unreadable=True)
        year_raw = slash.group(3)
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
            parsed = _safe_date(year, month, day)
            if parsed is None:
                return PhraseResult(
                    warnings=["That date doesn't exist on the calendar."], unreadable=True
                )
            result = _only_future([parsed], today)
        else:
            candidate = _safe_date(today.year, month, day)
            if candidate is None:
                return PhraseResult(
                    warnings=["That date doesn't exist on the calendar."], unreadable=True
                )
            if candidate < today:
                candidate = _safe_date(today.year + 1, month, day)
            assert candidate is not None
            result = _only_future([candidate], today)
        if ambiguous:
            result.ambiguous = True
            result.clarification = (
                f"{month}/{day} can be read as month/day or day/month. "
                f"Confirm the date ({candidate.strftime('%B %-d')})."
            )
        return result

    ordinal_match = re.fullmatch(r"(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)", text)
    if ordinal_match:
        return PhraseResult(
            ambiguous=True,
            clarification=(
                "Only a day of the month was given. Ask which month, or whether they mean the "
                "start or end of the upcoming month."
            ),
        )

    month_word = re.fullmatch(r"([a-z]+)\.?(?:\s+(\d{1,2})(?:st|nd|rd|th)?)?(?:,?\s+(\d{4}))?", text)
    if month_word and _month_from(month_word.group(1)):
        month = _month_from(month_word.group(1))
        assert month is not None
        explicit_year = bool(month_word.group(3))
        year = int(month_word.group(3)) if explicit_year else today.year
        day = int(month_word.group(2)) if month_word.group(2) else 1
        candidate = _safe_date(year, month, day)
        if candidate is None:
            return PhraseResult(warnings=["That date doesn't exist on the calendar."], unreadable=True)
        if explicit_year:
            # A stated year is taken literally: an old date is past, not next year.
            return _only_future([candidate], today)
        if candidate < today:
            candidate = _safe_date(year + 1, month, day) or candidate
        return _only_future([candidate], today)

    # "5th of October" / "5 October" (no year).
    pair = _month_day_pair(text)
    if pair:
        month, day = pair
        candidate = _safe_date(today.year, month, day)
        if candidate is None:
            return PhraseResult(warnings=["That date doesn't exist on the calendar."], unreadable=True)
        if candidate < today:
            candidate = _safe_date(today.year + 1, month, day) or candidate
        return _only_future([candidate], today)

    return PhraseResult(unreadable=True)


def _only_future(days: list[date], today: date) -> PhraseResult:
    future = [day for day in days if day >= today]
    if future:
        return PhraseResult(days=future)
    return PhraseResult(
        days=[],
        past=True,
        warnings=["The requested date has already passed."],
        clarification="The date they asked for has passed. Ask for an upcoming date.",
    )


def resolve_phrase(phrase: str, today: date, config: SchedulingConfig) -> PhraseResult:
    """Resolve one phrase: explicit dates first, then relative wording.

    ``config`` is required because "weekday" and "next week" are filtered against
    the days *this* clinic books on. Tenant A books Monday-Saturday, so "weekdays"
    includes Monday; tenant B opens Tuesday, and reading its own phrase through
    tenant A's calendar would offer a Monday that its grid has no slots for.
    """
    text = re.sub(r"\s+", " ", (phrase or "").strip().lower()).replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return PhraseResult(unreadable=True)

    # A phrase that names a qualifier or a weekday is never a bare month name.
    qualifies_weekday = bool(re.search(r"\b(?:this|coming|next)\b", text)) or (
        weekday_index(text) is not None
    )
    if not qualifies_weekday:
        literal = _resolve_explicit(text, today)
        # A clean literal reading wins: a resolved day, a past date, or an
        # ambiguity the assistant must voice (ordinal day, ambiguous slash date).
        if literal.days or literal.past or literal.ambiguous:
            return literal

    # --- named weekdays, with optional qualifier ---------------------------
    weekday_match = re.fullmatch(
        r"((?:(?:this|the|coming|next)\s+){1,2})?"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|weds?|thu(?:rs?)?|fri|sat|sun)"
        r"(?:\s+(?:of\s+|in\s+)?(?:the\s+)?(?:week|weekend))?",
        text,
    )
    if weekday_match:
        return _resolve_weekday(weekday_match.group(1), weekday_match.group(2), today, config)

    if text in {"today", "tonight"}:
        return PhraseResult(days=[today])

    if re.fullmatch(r"tomorrow|tmrw|tmw", text):
        return _only_future([today + timedelta(days=1)], today)

    if re.fullmatch(r"(?:the\s+)?day after tomorrow", text):
        return _only_future([today + timedelta(days=2)], today)

    # Open-ended flexibility is not a named day. Return a readable empty result
    # so preference resolution falls through to the default scan window without
    # warning the model that the phrase was unreadable.
    if _is_flexible_phrase(text):
        return PhraseResult(days=[])

    # --- weekends -----------------------------------------------------------
    if re.fullmatch(r"(?:this|next|coming|the coming)?\s*weekend", text):
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        return _only_future([saturday], today)

    if re.fullmatch(r"weekdays?|during the week|any weekday", text):
        upcoming = [day for day in _span(today, 7) if config.is_open_weekday(day)]
        return PhraseResult(days=upcoming)

    # --- weeks --------------------------------------------------------------
    if re.fullmatch(r"(?:later\s+)?next\s+week", text):
        monday, sunday = _week_bounds(today)
        del monday
        start = sunday + timedelta(days=1)
        if text.startswith("later"):
            start += timedelta(days=3)  # Thursday onward
            return PhraseResult(days=[day for day in _span(start, 4) if config.is_open_weekday(day)])
        return PhraseResult(days=[day for day in _span(start, 7) if config.is_open_weekday(day)])

    if re.fullmatch(r"(?:the\s+)?rest of (?:the|this) week|this week", text):
        _, sunday = _week_bounds(today)
        return PhraseResult(
            days=[
                day
                for day in _span(today, (sunday - today).days + 1)
                if config.is_open_weekday(day)
            ]
        )

    # --- months -------------------------------------------------------------
    month_match = re.fullmatch(
        r"(early|beginning of|late|end of|mid|middle of)?\s*(next|this|coming)?\s*month", text
    )
    if month_match:
        return _resolve_month(month_match.group(1) or "", month_match.group(2) or "", today, config)

    # --- offsets ------------------------------------------------------------
    offset = re.fullmatch(r"in\s+(\d{1,3})\s+(day|days|week|weeks)", text)
    if offset:
        amount = int(offset.group(1))
        if offset.group(2).startswith("week"):
            amount *= 7
        if amount <= 0:
            return PhraseResult(days=[today])
        return _only_future([today + timedelta(days=amount)], today)

    return PhraseResult(unreadable=True, warnings=[f"I couldn't read '{phrase}' as a date."])


def _resolve_weekday(
    qualifier: str | None, word: str, today: date, config: SchedulingConfig
) -> PhraseResult:
    index = weekday_index(word)
    if index is None:
        return PhraseResult(unreadable=True)
    label = WEEKDAY_NAMES[index].capitalize()
    words = set((qualifier or "").split())
    monday, sunday = _week_bounds(today)
    this_day = monday + timedelta(days=index)
    del monday

    if "next" in words:
        candidate = today + timedelta(days=(index - today.weekday()) % 7)
        if candidate == today:
            candidate += timedelta(days=7)
        result = _only_future([candidate], today)
        # Late in the week, "next <weekday>" that lands within the coming few
        # days can also read as the week after. Confirm rather than guess.
        if today.weekday() >= 4 and 0 < (candidate - today).days <= 2 and result.days:
            result.ambiguous = True
            result.clarification = (
                f"Today is {WEEKDAY_NAMES[today.weekday()].capitalize()}, so \"next {label}\" "
                f"could mean {candidate.strftime('%b %-d')} or "
                f"{(candidate + timedelta(days=7)).strftime('%b %-d')}. Confirm which."
            )
        return result

    if "this" in words or "coming" in words:
        if this_day >= today:
            return PhraseResult(days=[this_day])
        following = today + timedelta(days=(index - today.weekday()) % 7 or 7)
        # "This coming X" rolls to the next occurrence; bare "this X" is ambiguous.
        if "coming" in words:
            return PhraseResult(days=[following])
        return PhraseResult(
            ambiguous=True,
            clarification=(
                f"This {label} already passed. Confirm whether they mean "
                f"{following.strftime('%b %-d')}."
            ),
        )

    # Bare weekday: this week's day if it is still ahead, otherwise next week's.
    if this_day > today:
        return PhraseResult(days=[this_day])
    if this_day == today:
        return PhraseResult(
            days=[today],
            ambiguous=True,
            clarification=(
                f"Today is {label}. Confirm whether they mean today or {label} next week."
            ),
        )
    return PhraseResult(days=[this_day + timedelta(days=7)])


def _resolve_month(
    qualifier: str, which: str, today: date, config: SchedulingConfig
) -> PhraseResult:
    if which == "this":
        year, month = today.year, today.month
    else:
        if today.month == 12:
            year, month = today.year + 1, 1
        else:
            year, month = today.year, today.month + 1
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])

    if "early" in qualifier or "beginning" in qualifier:
        end = min(first + timedelta(days=9), last)
        window = _span(first, (end - first).days + 1)
    elif "late" in qualifier or "end" in qualifier:
        start = max(first, last - timedelta(days=9))
        window = _span(start, (last - start).days + 1)
    elif "mid" in qualifier:
        start = max(first, date(year, month, 10))
        end = min(last, date(year, month, 20))
        window = _span(start, (end - start).days + 1)
    else:
        window = _span(first, (last - first).days + 1)

    open_days = [day for day in window if day >= today and config.is_open_weekday(day)]
    result = PhraseResult(days=open_days)
    if not open_days:
        result.past = True
        result.warnings.append("That month window is already behind us.")
        result.clarification = "That window has passed. Ask which month they mean."
    return result


__all__ = ["PhraseResult", "WEEKDAY_INDEX", "WEEKDAY_NAMES", "resolve_phrase", "weekday_index"]
