"""Clinic value types.

These three dataclasses describe *what a clinic's configuration looks like*. They
carry no clinic's actual values any more: under multi-tenancy the facts live in
the ``clinics``, ``clinic_insurance_plans`` and ``clinic_policies`` tables and are
delivered to callers as :class:`app.tenancy.ClinicContext`.

Keeping the types here (rather than in ``tenancy.py``) is what lets ``app.db``,
the ORM and the seeding path all share one vocabulary without an import cycle:
this module imports nothing from the project.

:func:`hours_map_from` and :func:`normalise_open_weekdays` are the small
translation layer between the stored columns and the labels the prompt and
``/api/clinic`` render, so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class InsurancePlan:
    name: str
    accepted: bool
    note: str


@dataclass(frozen=True)
class PracticePolicy:
    """A single answerable question, stored per clinic.

    ``topic`` is the stable lookup key the agent passes to ``get_practice_info``.
    ``answer`` must be self-contained: the assistant relays it rather than
    paraphrasing freely, so it can never invent a price or a coverage promise.
    """

    topic: str
    question: str
    answer: str
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClinicConfig:
    """The shape a clinic row is described by, used as a seed template type.

    ``business_hours`` is keyed by weekday label with ``open``/``close`` values in
    24-hour ``HH:MM`` (or ``None`` when closed), which is the exact shape
    ``/api/clinic`` has always returned.
    """

    name: str
    legal_note: str
    address_line1: str
    address_line2: str
    city: str
    state: str
    postal_code: str
    phone: str
    email: str
    timezone: str
    slot_minutes: int
    business_hours: dict[str, dict[str, str | None]]
    closed_days: tuple[str, ...]
    parking_note: str
    timezone_note: str = ""
    fictional: bool = True
    extra: dict[str, str] = field(default_factory=dict)

    def as_public_dict(self) -> dict:
        return {
            "name": self.name,
            "legal_note": self.legal_note,
            "address": {
                "line1": self.address_line1,
                "line2": self.address_line2,
                "city": self.city,
                "state": self.state,
                "postal_code": self.postal_code,
            },
            "phone": self.phone,
            "email": self.email,
            "timezone": self.timezone,
            "timezone_note": self.timezone_note,
            "slot_minutes": self.slot_minutes,
            "business_hours": self.business_hours,
            "closed_days": list(self.closed_days),
            "parking_note": self.parking_note,
            "fictional": self.fictional,
        }


def hours_map_from(open_time: time, close_time: time, open_weekdays) -> dict[str, dict[str, str | None]]:
    """Weekday-labelled open/close map from a grid, matching ``/api/clinic``."""
    open_label = open_time.strftime("%H:%M")
    close_label = close_time.strftime("%H:%M")
    days = set(open_weekdays)
    return {
        label: {
            "open": open_label if index in days else None,
            "close": close_label if index in days else None,
        }
        for index, label in enumerate(WEEKDAY_LABELS)
    }


def open_weekdays_from(hours: dict[str, dict[str, str | None]]) -> tuple[int, ...]:
    """Inverse of :func:`hours_map_from`, for templates written as an hours map."""
    return tuple(
        index
        for index, label in enumerate(WEEKDAY_LABELS)
        if (hours.get(label) or {}).get("open") and (hours.get(label) or {}).get("close")
    )


__all__ = [
    "ClinicConfig",
    "InsurancePlan",
    "PracticePolicy",
    "WEEKDAY_LABELS",
    "hours_map_from",
    "open_weekdays_from",
]
