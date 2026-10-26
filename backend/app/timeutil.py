"""Clinic time, business-hour windows, and the per-tenant scheduling grid.

All datetimes are stored as **naive local clinic wall-clock time**, in each
clinic's own timezone, and serialised as ISO-8601 to clients. That keeps slot
arithmetic independent from the machine's local timezone and avoids DST
ambiguities during comparisons.

Multi-tenancy changed the shape of this module. Before it, the clinic's timezone,
slot length and business hours were module constants, which silently meant every
clinic shared one grid. They are now columns on the ``clinics`` row, assembled
here into :class:`SchedulingConfig`, and every function that once read a global
is now a method on that config. The consequence worth stating plainly: a naive
``10:00`` in ``America/New_York`` and a naive ``10:00`` in
``America/Los_Angeles`` are the *same stored value*, so a query must never be
able to compare the two. Tenant filtering happens before any time comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Python weekday(): Monday == 0.
ALL_WEEKDAYS = frozenset(range(7))


def minute_of(hour: int, minute: int = 0) -> int:
    return hour * 60 + minute


def datetime_minute_of(moment: time) -> int:
    return moment.hour * 60 + moment.minute


def day_label(day: date) -> str:
    """Weekday label for a date. Pure: a label is not a tenant's business."""
    return WEEKDAY_LABELS[day.weekday()]


def utc_now_iso() -> str:
    """Server clock in UTC, for response envelopes that are *not* about a clinic.

    Anything that answers "what time is it at the practice" must use
    ``SchedulingConfig.now_iso_with_offset()`` instead. This function exists so
    that need is never confused with a wall clock that happens to be the host's.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TimeWindow:
    """A same-day time-of-day range, in whole minutes from midnight."""

    start_minute: int
    end_minute: int
    label: str

    def contains(self, moment: datetime) -> bool:
        return self.start_minute <= minute_of(moment.hour, moment.minute) < self.end_minute

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True)
class SchedulingConfig:
    """One clinic's booking grid: timezone, slot length, and business hours.

    Built from a ``Clinic`` row by :func:`app.tenancy.scheduling_config_for`.
    Frozen so a turn cannot mutate the grid it is answering against.
    """

    timezone_name: str
    slot_minutes: int = 30
    open_time: time = time(8, 0)
    close_time: time = time(18, 0)
    open_weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4, 5})
    closed_day_labels: tuple[str, ...] = ("Sun",)
    seed_horizon_days: int = 60

    # --- derived ---------------------------------------------------------- #
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def slot_length(self) -> timedelta:
        return timedelta(minutes=self.slot_minutes)

    @property
    def open_minute(self) -> int:
        return datetime_minute_of(self.open_time)

    @property
    def close_minute(self) -> int:
        return datetime_minute_of(self.close_time)

    @property
    def slots_per_day(self) -> int:
        return max(1, (self.close_minute - self.open_minute) // self.slot_minutes)

    @property
    def utc_offset_label(self) -> str:
        """Human offset for *now*, e.g. ``UTC-07:00``. Used in logs and cards."""
        offset = datetime.now(self.tz).utcoffset() or timedelta(0)
        total = int(offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"

    # --- clock ------------------------------------------------------------ #
    def today(self) -> date:
        """Today's date in *this clinic's* timezone (never the container's)."""
        return datetime.now(self.tz).date()

    def now(self) -> datetime:
        """Current clinic-local wall-clock time, naive."""
        return datetime.now(self.tz).replace(tzinfo=None)

    def now_iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def now_iso_with_offset(self) -> str:
        """Clinic-local timestamp carrying its UTC offset.

        The offset lets clients show the correct clinic time without relying on
        the browser's own timezone, which for a second clinic in another zone is
        the difference between "open" and "closed" on screen.
        """
        return datetime.now(self.tz).isoformat(timespec="seconds")

    def to_utc(self, moment: datetime) -> datetime:
        """Interpret a stored naive clinic-local wall-clock as this clinic's UTC."""
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        return moment.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    # --- grid ------------------------------------------------------------- #
    def is_open_weekday(self, day: date) -> bool:
        return day.weekday() in self.open_weekdays

    def slot_opens_at(self, day: date, start_time: time) -> datetime:
        return datetime.combine(day, start_time)

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        return (
            datetime.combine(day, self.open_time),
            datetime.combine(day, self.close_time),
        )

    def day_slot_starts(self, day: date) -> list[datetime]:
        """The slot grid starts for one business day, honouring slot length."""
        first = datetime.combine(day, self.open_time)
        return [first + self.slot_length * index for index in range(self.slots_per_day)]

    def slots_required(self, duration_minutes: int) -> int:
        return max(1, -(-duration_minutes // self.slot_minutes))

    def interval_end(self, starts_at: datetime, slots: int) -> datetime:
        return starts_at + self.slot_length * slots

    def is_grid_aligned(self, starts_at: datetime, day: date) -> bool:
        """Whether a time sits on this clinic's grid for that day.

        A 20-minute clinic must not accept a timestamp that only lines up on a
        30-minute grid, and comparing offsets by minutes (not by the slot rows)
        is what lets validation run before a query.
        """
        first = datetime.combine(day, self.open_time)
        return (starts_at - first) % self.slot_length == timedelta(0)

    def day_span_between(self, first: datetime, last: datetime) -> int:
        return (last - first).days

    # --- time-of-day windows ---------------------------------------------- #
    # These used to be module constants derived from a single clinic's hours.
    # "Afternoon" means the same thing everywhere, but "late" has to stop at
    # *this* clinic's closing time.
    @property
    def morning_window(self) -> TimeWindow:
        return TimeWindow(self.open_minute, 12 * 60, "morning (before noon)")

    @property
    def afternoon_window(self) -> TimeWindow:
        return TimeWindow(12 * 60, min(17 * 60, self.close_minute), "afternoon (12-5 PM)")

    @property
    def late_window(self) -> TimeWindow:
        return TimeWindow(min(16 * 60, self.close_minute), self.close_minute, "late afternoon")

    @property
    def evening_window(self) -> TimeWindow:
        start = max(self.open_minute, min(17 * 60, self.close_minute))
        return TimeWindow(start, self.close_minute, "evening (after 5 PM)")

    def time_windows(self) -> tuple[TimeWindow, ...]:
        return (
            self.morning_window,
            self.afternoon_window,
            self.late_window,
            self.evening_window,
        )

    def window_for(self, name: str) -> TimeWindow | None:
        cleaned = (name or "").strip().lower()
        for window in self.time_windows():
            if window.label.split(" ")[0] == cleaned:
                return window
        aliases = {
            "morning": self.morning_window,
            "am": self.morning_window,
            "afternoon": self.afternoon_window,
            "pm": self.afternoon_window,
            "late": self.late_window,
            "evening": self.evening_window,
            "night": self.evening_window,
        }
        return aliases.get(cleaned)

    # --- labels ----------------------------------------------------------- #
    @property
    def open_day_labels(self) -> tuple[str, ...]:
        return tuple(WEEKDAY_LABELS[index] for index in sorted(self.open_weekdays))

    @property
    def booking_days_phrase(self) -> str:
        """``Mon-Sat`` style phrase for the system prompt and hours text."""
        labels = self.open_day_labels
        if not labels:
            return "no days"
        if len(labels) == 1:
            return labels[0]
        return f"{labels[0]}-{labels[-1]}" if self._is_contiguous(labels) else ", ".join(labels)

    def _is_contiguous(self, labels: tuple[str, ...]) -> bool:
        indexes = [WEEKDAY_LABELS.index(label) for label in labels]
        return indexes == list(range(indexes[0], indexes[-1] + 1))

    @property
    def open_time_label(self) -> str:
        return self.open_time.strftime("%I:%M %p").lstrip("0")

    @property
    def close_time_label(self) -> str:
        return self.close_time.strftime("%I:%M %p").lstrip("0")

    def hours_phrase(self, timezone_note: str | None = None) -> str:
        zone = timezone_note or self.timezone_name
        return (
            f"{self.booking_days_phrase}, {self.open_time_label} to "
            f"{self.close_time_label} {zone}"
        )

    def business_hours_map(self) -> dict[str, dict[str, str | None]]:
        """Per-day open/close labels, the shape ``/api/clinic`` and the prompt use.

        Tested against ``open_weekdays`` directly: this loop walks the seven
        weekday indexes, not real calendar days, and ``is_open_weekday`` takes a
        ``date`` because a holiday can close an otherwise open weekday.
        """
        open_label = self.open_time.strftime("%H:%M")
        close_label = self.close_time.strftime("%H:%M")
        return {
            label: {
                "open": open_label if index in self.open_weekdays else None,
                "close": close_label if index in self.open_weekdays else None,
            }
            for index, label in enumerate(WEEKDAY_LABELS)
        }


# The grid that used to be hardcoded here. Kept only so the first demo clinic can
# be seeded with exactly the values it had before multi-tenant, and so a database
# created before the ``clinics`` table existed can be backfilled with a config
# that matches its stored slot rows. Nothing at runtime may read it: tenant
# config comes from the clinic row. See ``app/seed_templates.py``.
LEGACY_CLINIC_SCHEDULING = SchedulingConfig(
    timezone_name="America/Los_Angeles",
    slot_minutes=30,
    open_time=time(8, 0),
    close_time=time(18, 0),
    open_weekdays=frozenset({0, 1, 2, 3, 4, 5}),
    closed_day_labels=("Sun",),
    seed_horizon_days=60,
)

# Historic aliases. Module constants must never be used to answer a scheduling
# question (that is what made the second clinic wrong), and these exist only for
# the seeding/backfill path and for tests that assert on the original grid.
LEGACY_CLINIC_TIMEZONE_NAME = LEGACY_CLINIC_SCHEDULING.timezone_name
LEGACY_CLINIC_TZ = LEGACY_CLINIC_SCHEDULING.tz
LEGACY_OPEN_WEEKDAYS = tuple(sorted(LEGACY_CLINIC_SCHEDULING.open_weekdays))
LEGACY_OPEN_TIME = LEGACY_CLINIC_SCHEDULING.open_time
LEGACY_CLOSE_TIME = LEGACY_CLINIC_SCHEDULING.close_time
LEGACY_SLOT_MINUTES = LEGACY_CLINIC_SCHEDULING.slot_minutes
LEGACY_SLOT_LENGTH = LEGACY_CLINIC_SCHEDULING.slot_length
LEGACY_SLOTS_PER_DAY = LEGACY_CLINIC_SCHEDULING.slots_per_day
LEGACY_SEED_HORIZON_DAYS = LEGACY_CLINIC_SCHEDULING.seed_horizon_days

__all__ = [
    "ALL_WEEKDAYS",
    "LEGACY_CLINIC_SCHEDULING",
    "LEGACY_CLINIC_TZ",
    "LEGACY_CLINIC_TIMEZONE_NAME",
    "WEEKDAY_LABELS",
    "SchedulingConfig",
    "TimeWindow",
    "day_label",
    "day_labels",
    "minute_of",
    "utc_now_iso",
]


def day_labels(indexes) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    """Weekday labels for a set of Python weekday ints, in calendar order."""
    return tuple(WEEKDAY_LABELS[index] for index in sorted(indexes))
