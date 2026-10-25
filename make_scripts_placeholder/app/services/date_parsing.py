"""Deterministic natural-language scheduling preferences.

The model *extracts* what the patient said; this module *resolves* it. No date
arithmetic here trusts the model: relative phrases are resolved against the
clinic's current date in ``America/Los_Angeles`` (never the server's timezone),
and every time-of-day window is clamped to clinic business hours afterwards.

Design rules
------------
* **Impossible beats ambiguous.** "Sunday" is not ambiguous, it is closed. A
  past date stays past instead of being silently rolled forward.
* **Ambiguity is explicit.** "Next Tuesday" is flagged when two readings are
  defensible (late in the week, or the day named is today). A bare weekday that
  equals today is flagged too. The assistant asks instead of guessing.
* **Never widen the request.** "Weekends only" removes weekdays even when the
  weekday part of the range has free time.

Public entry point: :func:`resolve_preferences`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.services.date_phrases import WEEKDAY_INDEX, WEEKDAY_NAMES, resolve_phrase, weekday_index
from app.timeutil import CLOSE_TIME, OPEN_TIME, clinic_today

# Bounded scanning so a confused model can never request a year of slots.
MAX_SEARCH_DAYS = 120
DEFAULT_SEARCH_DAYS = 21
MAX_WINDOW_DAYS = 62


def _minute_of(hour: int, minute: int = 0) -> int:
    return hour * 60 + minute


OPEN_MINUTE = _minute_of(OPEN_TIME.hour, OPEN_TIME.minute)
CLOSE_MINUTE = _minute_of(CLOSE_TIME.hour, CLOSE_TIME.minute)


@dataclass(frozen=True)
class TimeWindow:
    """A same-day time-of-day range, in whole minutes from midnight."""

    start_minute: int
    end_minute: int
    label: str

    def contains(self, moment: datetime) -> bool:
        return self.start_minute <= _minute_of(moment.hour, moment.minute) < self.end_minute


MORNING = TimeWindow(OPEN_MINUTE, 12 * 60, "morning (before noon)")
AFTERNOON = TimeWindow(12 * 60, 17 * 60, "afternoon (12-5 PM)")
LATE = TimeWindow(16 * 60, CLOSE_MINUTE, "late afternoon")


@dataclass
class ResolvedPreferences:
    """What the availability engine needs to apply the patient's constraints."""

    today: date
    days: list[date] = field(default_factory=list)
    windows: list[TimeWindow] = field(default_factory=list)
    weekday_filter: set[int] | None = None
    provider_name: str | None = None
    requested: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    ambiguous: bool = False
    clarification: str | None = None
    past_rejected: bool = False
    scan_start: date | None = None
    scan_end: date | None = None

    def accepts_day(self, day: date) -> bool:
        if self.days:
            return day in self.days
        if self.weekday_filter is not None:
            return day.weekday() in self.weekday_filter
        return True

    def accepts_time(self, moment: datetime) -> bool:
        return not self.windows or any(window.contains(moment) for window in self.windows)

    @property
    def window_label(self) -> str | None:
        if not self.windows:
            return None
        return ", ".join(window.label for window in self.windows)

    @property
    def scan_bounds(self) -> tuple[date, date]:
        """Inclusive day range to query when no explicit days were named."""
        if self.days:
            return self.days[0], self.days[-1]
        start = max(self.scan_start or self.today, self.today)
        end = self.scan_end or start
        return start, max(start, end)


def _parse_clock(raw: str) -> tuple[int, int] | None:
    """'3pm', '3:30 pm', '15:00', 'noon' -> (hour, minute)."""
    cleaned = raw.strip().lower().replace(" ", "")
    if cleaned == "noon":
        return 12, 0
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", cleaned)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _clamp(start_minute: int, end_minute: int, label: str) -> TimeWindow | None:
    """Fit a window into business hours; None when nothing overlaps."""
    lo, hi = max(start_minute, OPEN_MINUTE), min(end_minute, CLOSE_MINUTE)
    if hi <= lo:
        return None
    return TimeWindow(lo, hi, label if (lo, hi) == (start_minute, end_minute) else f"{label} (clinic hours)")


def resolve_time_window(raw: str) -> TimeWindow | None:
    """Map one time-of-day phrase to a clinic-hours window."""
    text = re.sub(r"\s+", " ", (raw or "").strip().lower())
    text = re.sub(r"\s+only$", "", text)
    if not text:
        return None

    exact = {
        "morning": MORNING,
        "mornings": MORNING,
        "early": MORNING,
        "early morning": MORNING,
        "in the morning": MORNING,
        "before lunch": MORNING,
        "before noon": MORNING,
        "am": MORNING,
        "afternoon": AFTERNOON,
        "afternoons": AFTERNOON,
        "in the afternoon": AFTERNOON,
        "after lunch": AFTERNOON,
        "pm": AFTERNOON,
        "late": LATE,
        "later in the day": LATE,
        "late afternoon": LATE,
        "end of day": LATE,
        "evening": LATE,
        "evenings": LATE,
        "after work": LATE,
    }
    if text in exact:
        return exact[text]

    if text in {"around lunch", "at lunch", "lunchtime"}:
        return _clamp(_minute_of(11, 30), _minute_of(13, 30), "around lunch")

    after = re.fullmatch(r"(?:any\s+time\s+)?after\s+([\w: ]+)", text)
    if after:
        clock = _parse_clock(after.group(1))
        if clock:
            return _clamp(_minute_of(*clock), CLOSE_MINUTE, f"after {after.group(1).strip()}")
        return None

    before = re.fullmatch(r"(?:any\s+time\s+)?before\s+([\w: ]+)", text)
    if before:
        clock = _parse_clock(before.group(1))
        if clock:
            return _clamp(OPEN_MINUTE, _minute_of(*clock), f"before {before.group(1).strip()}")
        return None

    between = re.fullmatch(
        r"(?:between\s+)?([\w: ]+?)\s*(?:and|to|until|-|–)\s*([\w: ]+)", text
    )
    if between:
        start_clock = _parse_clock(between.group(1))
        end_clock = _parse_clock(between.group(2))
        if start_clock and end_clock:
            label = f"{between.group(1).strip()}-{between.group(2).strip()}"
            return _clamp(_minute_of(*start_clock), _minute_of(*end_clock), label)
    return None


__all__ = [
    "DEFAULT_SEARCH_DAYS",
    "MAX_SEARCH_DAYS",
    "MAX_WINDOW_DAYS",
    "ResolvedPreferences",
    "TimeWindow",
    "WEEKDAY_INDEX",
    "WEEKDAY_NAMES",
    "resolve_preferences",
    "resolve_time_window",
]


def _normalized_phrases(values: list[str] | None) -> list[str]:
    return [value.strip() for value in (values or []) if value and value.strip()]


def resolve_preferences(
    *,
    start_date: str | None = None,
    date_phrases: list[str] | None = None,
    time_windows: list[str] | None = None,
    weekdays: list[str] | None = None,
    exclude_weekdays: list[str] | None = None,
    range_days: int | None = None,
    provider_name: str | None = None,
    today: date | None = None,
) -> ResolvedPreferences:
    """Resolve extracted scheduling language into concrete dates and windows.

    Precedence: explicit date phrases first, then ``start_date``/``range_days``.
    ``weekdays``/``exclude_weekdays`` narrow whichever of those applies and never
    widen it. A phrase that cannot be read is recorded so the assistant can ask
    again instead of quietly searching a different window.
    """
    current = today or clinic_today()
    resolved = ResolvedPreferences(today=current, provider_name=(provider_name or None))

    # --- explicit date phrases ---------------------------------------------
    explicit: list[date] = []
    for phrase in _normalized_phrases(date_phrases):
        resolved.requested.append(phrase)
        outcome = resolve_phrase(phrase, current)
        resolved.warnings.extend(outcome.warnings)
        if outcome.past:
            resolved.past_rejected = True
            resolved.rejected.append({"value": phrase, "reason": "That date has already passed."})
        if outcome.ambiguous:
            resolved.ambiguous = True
            resolved.clarification = outcome.clarification or resolved.clarification
        if outcome.unreadable and not outcome.warnings:
            resolved.warnings.append(f"I couldn't read '{phrase}' as a date.")
        explicit.extend(day for day in outcome.days if day >= current)

    if explicit:
        resolved.days = sorted(set(explicit))
        resolved.scan_start, resolved.scan_end = resolved.days[0], resolved.days[-1]
        _apply_weekday_filter(resolved, weekdays, exclude_weekdays)
        _apply_windows(resolved, time_windows)
        return resolved

    # --- range mode ---------------------------------------------------------
    anchor = current
    if start_date:
        try:
            parsed_start = date.fromisoformat(start_date.strip())
        except (ValueError, AttributeError):
            parsed_start = None
            resolved.warnings.append("That start date wasn't in a readable format.")
        if parsed_start is not None:
            if parsed_start < current:
                resolved.past_rejected = True
                resolved.rejected.append(
                    {"value": start_date, "reason": "That date has already passed."}
                )
            else:
                anchor = parsed_start

    span = range_days or DEFAULT_SEARCH_DAYS
    span = max(1, min(span, MAX_SEARCH_DAYS))
    resolved.scan_start = anchor
    resolved.scan_end = anchor + timedelta(days=span - 1)

    _apply_weekday_filter(resolved, weekdays, exclude_weekdays)
    _apply_windows(resolved, time_windows)
    return resolved


def _apply_weekday_filter(
    resolved: ResolvedPreferences,
    weekdays: list[str] | None,
    exclude_weekdays: list[str] | None,
) -> None:
    named: set[int] | None = None
    for raw in _normalized_phrases(weekdays):
        index = weekday_index(raw)
        if index is None:
            resolved.warnings.append(f"I couldn't read '{raw}' as a weekday.")
            continue
        named = (named or set()) | {index}

    for raw in _normalized_phrases(exclude_weekdays):
        index = weekday_index(raw)
        if index is None:
            resolved.warnings.append(f"I couldn't read '{raw}' as a weekday.")
            continue
        if named is None:
            # "Any day except Wednesday" means every open day but that one.
            named = set(WEEKDAY_INDEX.values()) - {index}
        else:
            named.discard(index)

    if named is None:
        return
    resolved.weekday_filter = named
    if resolved.days:
        resolved.days = [day for day in resolved.days if day.weekday() in named]


def _apply_windows(resolved: ResolvedPreferences, time_windows: list[str] | None) -> None:
    for raw in _normalized_phrases(time_windows):
        window = resolve_time_window(raw)
        if window is None:
            resolved.warnings.append(f"I couldn't read '{raw}' as a time of day.")
            continue
        if not any(
            existing.start_minute == window.start_minute
            and existing.end_minute == window.end_minute
            for existing in resolved.windows
        ):
            resolved.windows.append(window)
