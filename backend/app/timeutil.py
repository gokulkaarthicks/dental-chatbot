"""Clinic time and availability-window helpers.

All datetimes are stored as **naive local clinic wall-clock time** in
``America/Los_Angeles`` and serialised as ISO-8601 to clients. This is
intentional for the assessment scope: it keeps slot arithmetic (30-minute
intervals) independent from the machine's local timezone and avoids DST
ambiguities during comparisons. If real multi-timezone support is ever needed,
this module is the single place to change.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CLINIC_TIMEZONE_NAME = "America/Los_Angeles"
CLINIC_TZ = ZoneInfo(CLINIC_TIMEZONE_NAME)

# Monday..Saturday 08:00-18:00, Sunday closed. Python weekday(): Monday == 0.
OPEN_WEEKDAYS: tuple[int, ...] = (0, 1, 2, 3, 4, 5)
OPEN_TIME = time(8, 0)
CLOSE_TIME = time(18, 0)

SLOT_MINUTES = 30
SLOT_LENGTH = timedelta(minutes=SLOT_MINUTES)
SLOTS_PER_DAY = int((CLOSE_TIME.hour * 60 - OPEN_TIME.hour * 60) / SLOT_MINUTES)  # 20
SEED_HORIZON_DAYS = 60


def clinic_today() -> date:
    """Today's date in the clinic's timezone (not the container's)."""
    return datetime.now(CLINIC_TZ).date()


def clinic_now() -> datetime:
    """Current clinic-local wall-clock time, naive."""
    return datetime.now(CLINIC_TZ).replace(tzinfo=None)


def local_now_iso() -> str:
    return clinic_now().isoformat(timespec="seconds")


def clinic_now_iso_with_offset() -> str:
    """Clinic-local timestamp carrying its UTC offset, e.g. ``2026-10-24T15:04:00-07:00``.

    The offset lets clients show the correct clinic time without relying on the
    browser's own timezone.
    """
    return datetime.now(CLINIC_TZ).isoformat(timespec="seconds")


def clinic_day_label(moment: datetime) -> str:
    """Short weekday name (``Mon``..``Sun``) for a clinic-local datetime."""
    return WEEKDAY_LABELS[moment.weekday()]


WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def is_open_weekday(day: date) -> bool:
    return day.weekday() in OPEN_WEEKDAYS


def slot_opens_at(day: date, start_time: time) -> datetime:
    return datetime.combine(day, start_time)


def day_slot_starts(day: date) -> list[datetime]:
    """The 30-minute grid starts for one business day."""
    return [slot_opens_at(day, OPEN_TIME) + SLOT_LENGTH * index for index in range(SLOTS_PER_DAY)]
