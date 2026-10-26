"""The resolved tenant for one request: clinic row, grid, and its knowledge.

``ClinicContext`` is the only way runtime code reaches clinic facts. It is a
frozen *snapshot* rather than the ORM row, on purpose:

* the context outlives the session it was read from (a turn commits mid-way and
  still has to build a prompt), so a detached instance would lazy-load and blow
  up at the worst moment;
* a snapshot cannot be written to by accident, so "clinic config" and "patient
  data" stay visibly different things;
* the tenant filter on the child tables is applied while building it, so
  everything inside belongs to exactly one clinic.

The cache is keyed on ``(clinic_id, updated_at)``. Clinic rows are low-churn
configuration, so per-turn prompt building gets one indexed read instead of three
queries, and editing a clinic's hours invalidates it on the next request without
a TTL that would serve stale policy text.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clinic import InsurancePlan, PracticePolicy
from app.models import Clinic, ClinicInsurancePlan, ClinicPolicy
from app.timeutil import WEEKDAY_LABELS, SchedulingConfig, TimeWindow


class UnknownClinic(Exception):
    """No clinic for the requested identifier, or the clinic is closed."""

    def __init__(self, slug_or_id: str | int | None, *, inactive: bool = False) -> None:
        self.slug_or_id = slug_or_id
        self.inactive = inactive
        super().__init__(f"clinic {slug_or_id!r} is not available" if not inactive else
                         f"clinic {slug_or_id!r} is inactive")


def scheduling_config_for(clinic: Clinic) -> SchedulingConfig:
    """Assemble the tenant's booking grid from its row.

    ``open_weekdays`` arrives as a JSON list of Python weekday ints. Anything
    outside 0-6 is dropped rather than trusted: a bad value would otherwise make
    the engine offer a day that does not exist.
    """
    raw_days = clinic.open_weekdays or []
    weekdays = frozenset(int(day) for day in raw_days if isinstance(day, int) or str(day).isdigit())
    weekdays = frozenset(day % 7 for day in weekdays if 0 <= int(day) < 7)
    if not weekdays:
        # Never let a misconfigured row produce an empty grid: fall back to the
        # days the clinic is not explicitly closed on.
        closed = {label for label in (clinic.closed_days or [])}
        weekdays = frozenset(
            index for index, label in enumerate(_WEEKDAY_ORDER) if label not in closed
        ) or frozenset({0, 1, 2, 3, 4, 5})
    return SchedulingConfig(
        timezone_name=clinic.timezone,
        slot_minutes=int(clinic.slot_minutes or 30),
        open_time=clinic.open_time,
        close_time=clinic.close_time,
        open_weekdays=weekdays,
        closed_day_labels=tuple(clinic.closed_days or ()),
        seed_horizon_days=int(clinic.seed_horizon_days or 60),
    )


_WEEKDAY_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class ClinicContext:
    """Everything one request is allowed to know about a clinic."""

    clinic_id: int
    slug: str
    public_id: str
    name: str
    legal_note: str | None
    address_line1: str
    address_line2: str | None
    city: str
    state: str
    postal_code: str
    phone: str
    email: str
    config: SchedulingConfig
    timezone_note: str | None
    parking_note: str | None
    insurance_note: str | None
    self_pay_label: str
    fictional: bool
    is_default: bool
    active: bool
    insurance_plans: tuple[InsurancePlan, ...]
    policies: tuple[PracticePolicy, ...]
    version: datetime

    # --- identity --------------------------------------------------------- #
    @property
    def location(self) -> str:
        return f"{self.city}, {self.state}"

    @property
    def full_address(self) -> str:
        return f"{self.address_line1}, {self.address_line2}, {self.city}, {self.state} {self.postal_code}"

    @property
    def timezone_name(self) -> str:
        return self.config.timezone_name

    # --- knowledge lookups ------------------------------------------------ #
    def policy(self, topic: str | None) -> PracticePolicy | None:
        cleaned = (topic or "").strip().lower().replace(" ", "_").replace("-", "_")
        if not cleaned:
            return None
        for candidate in self.policies:
            if candidate.topic == cleaned:
                return candidate
        return None

    def plans_matching(self, compact_name: str) -> InsurancePlan | None:
        """Resolve a normalised provider string against this clinic's plans."""
        for plan in self.insurance_plans:
            if _compact(plan.name) == compact_name:
                return plan
        return None

    @property
    def accepted_plan_names(self) -> frozenset[str]:
        return frozenset(plan.name for plan in self.insurance_plans if plan.accepted)

    @property
    def accepted_plans(self) -> tuple[InsurancePlan, ...]:
        return tuple(plan for plan in self.insurance_plans if plan.accepted)

    def topics(self) -> list[str]:
        return [policy.topic for policy in self.policies]

    # --- presentation ----------------------------------------------------- #
    def hours_block(self) -> str:
        return (
            f"Hours ({self.config.timezone_name}): "
            + ", ".join(
                f"{day} {window['open']}-{window['close']}"
                if window["open"] and window["close"]
                else f"{day} closed"
                for day, window in self.config.business_hours_map().items()
            )
            + "."
        )

    def front_desk_hours_phrase(self) -> str:
        return self.config.hours_phrase(self.timezone_note or self.config.timezone_name)

    def as_public_dict(self) -> dict:
        """The ``/api/clinic`` payload shape, kept identical to the Phase-1 one.

        Optional columns are coerced to empty strings so the response model can
        stay an explicit allow-list of ``str`` fields: a clinic that left its
        parking note blank should read as "no note", not as a missing field the
        client has to special-case.
        """
        return {
            "slug": self.slug,
            "clinic_id": self.clinic_id,
            "name": self.name,
            "legal_note": self.legal_note or "",
            "address": {
                "line1": self.address_line1,
                "line2": self.address_line2 or "",
                "city": self.city,
                "state": self.state,
                "postal_code": self.postal_code,
            },
            "phone": self.phone,
            "email": self.email,
            "timezone": self.config.timezone_name,
            "timezone_note": self.timezone_note or "",
            "slot_minutes": self.config.slot_minutes,
            "business_hours": self.config.business_hours_map(),
            "closed_days": list(self.config.closed_day_labels),
            "parking_note": self.parking_note or "",
            "fictional": self.fictional,
            "scheduling": self.scheduling_public(),
        }

    def summary(self) -> dict:
        """The public directory entry: enough to pick a clinic, nothing more."""
        return {
            "slug": self.slug,
            "clinic_id": self.clinic_id,
            "name": self.name,
            "city": self.city,
            "state": self.state,
            "timezone": self.config.timezone_name,
            "is_default": self.is_default,
        }

    def scheduling_public(self) -> dict:
        """Grid parameters a client needs to render slots without assuming them."""
        return {
            "timezone": self.config.timezone_name,
            "slot_minutes": self.config.slot_minutes,
            "open_time": self.config.open_time.strftime("%H:%M"),
            "close_time": self.config.close_time.strftime("%H:%M"),
            "open_weekdays": sorted(self.config.open_weekdays),
            "open_day_labels": list(self.config.open_day_labels),
            "closed_day_labels": list(self.config.closed_day_labels),
            "booking_days_phrase": self.config.booking_days_phrase,
            "utc_offset": _utc_offset_label(self.config.timezone_name),
            "time_windows": {
                window.label.split(" ")[0]: [window.start_minute, window.end_minute]
                for window in self.config.time_windows()
            },
        }

    def window(self, name: str) -> TimeWindow | None:
        return self.config.window_for(name)


def _compact(text: str) -> str:
    return "".join(character for character in text.lower() if character.isalnum())


def _utc_offset_label(timezone_name: str) -> str:
    try:
        return datetime.now(ZoneInfo(timezone_name)).strftime("%z")
    except (ZoneInfoNotFoundError, ValueError):
        return ""


# --------------------------------------------------------------------------- #
# Loading and caching
# --------------------------------------------------------------------------- #

_CACHE: dict[int, tuple[datetime, ClinicContext]] = {}
_CACHE_LOCK = threading.Lock()


def load_clinic_row(session: Session, clinic_id: int) -> Clinic | None:
    return session.get(Clinic, clinic_id)


def find_clinic(session: Session, slug_or_id: str | int | None) -> Clinic | None:
    """Resolve a tenant by slug or numeric id. No tenant filter is applied.

    This runs *before* a session is bound to a tenant, which is why it reads the
    ``clinics`` table directly: the table has no ``clinic_id`` of its own, and
    resolving one tenant must never require already knowing another.
    """
    if slug_or_id is None or str(slug_or_id).strip() == "":
        return None
    cleaned = str(slug_or_id).strip()
    clinic = session.scalar(select(Clinic).where(Clinic.slug == cleaned))
    if clinic is not None:
        return clinic
    if cleaned.isdigit():
        return session.get(Clinic, int(cleaned))
    return None


def default_clinic(session: Session) -> Clinic | None:
    """The tenant used when a request names none, so single-clinic clients work."""
    clinic = session.scalar(select(Clinic).where(Clinic.is_default.is_(True)).limit(1))
    if clinic is not None:
        return clinic
    return session.scalar(select(Clinic).where(Clinic.active.is_(True)).order_by(Clinic.id).limit(1))


def context_from(session: Session, clinic: Clinic, *, use_cache: bool = True) -> ClinicContext:
    """Build (or reuse) the snapshot for one clinic row."""
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(clinic.id)
        if cached is not None and cached[0] == clinic.updated_at:
            return cached[1]

    plans = session.scalars(
        select(ClinicInsurancePlan)
        .where(ClinicInsurancePlan.clinic_id == clinic.id)
        .order_by(ClinicInsurancePlan.id)
    )
    policies = session.scalars(
        select(ClinicPolicy).where(ClinicPolicy.clinic_id == clinic.id).order_by(ClinicPolicy.id)
    )
    context = ClinicContext(
        clinic_id=clinic.id,
        slug=clinic.slug,
        public_id=clinic.public_id,
        name=clinic.name,
        legal_note=clinic.legal_note,
        address_line1=clinic.address_line1,
        address_line2=clinic.address_line2,
        city=clinic.city,
        state=clinic.state,
        postal_code=clinic.postal_code,
        phone=clinic.phone,
        email=clinic.email,
        config=scheduling_config_for(clinic),
        timezone_note=clinic.timezone_note,
        parking_note=clinic.parking_note,
        insurance_note=clinic.insurance_note,
        self_pay_label=clinic.self_pay_label,
        fictional=clinic.fictional,
        is_default=clinic.is_default,
        active=clinic.active,
        insurance_plans=tuple(
            InsurancePlan(name=plan.name, accepted=plan.accepted, note=plan.note or "")
            for plan in plans
        ),
        policies=tuple(
            PracticePolicy(
                topic=policy.topic,
                question=policy.question,
                answer=policy.answer,
                keywords=tuple(policy.keywords or ()),
            )
            for policy in policies
        ),
        version=clinic.updated_at,
    )
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[clinic.id] = (context.version, context)
    return context


def resolve_clinic(session: Session, slug_or_id: str | int | None) -> ClinicContext:
    """Resolve a request's tenant. Raises ``UnknownClinic`` if it cannot."""
    clinic = find_clinic(session, slug_or_id)
    if clinic is None:
        raise UnknownClinic(slug_or_id)
    if not clinic.active:
        raise UnknownClinic(slug_or_id, inactive=True)
    return context_from(session, clinic)


def resolve_clinic_or_default(session: Session, slug_or_id: str | int | None) -> ClinicContext:
    """Resolve the named tenant, falling back to the default clinic when unnamed."""
    if slug_or_id is None or str(slug_or_id).strip() == "":
        clinic = default_clinic(session)
        if clinic is None:
            raise UnknownClinic(slug_or_id)
        if not clinic.active:
            raise UnknownClinic(clinic.slug, inactive=True)
        return context_from(session, clinic)
    return resolve_clinic(session, slug_or_id)


def list_clinics(session: Session) -> list[ClinicContext]:
    """Every active tenant, for the public clinic directory."""
    contexts: list[ClinicContext] = []
    for clinic in session.scalars(
        select(Clinic).where(Clinic.active.is_(True)).order_by(Clinic.name)
    ):
        contexts.append(context_from(session, clinic))
    return contexts


def contexts_for_slugs(session: Session, slugs: list[str] | None) -> list[ClinicContext]:
    """The named tenants, or every active one when nothing was named.

    Operational tooling (CLI, demo reset) needs the same snapshot a request gets,
    but chosen by slug list rather than by header. Reading the directory unscoped
    is deliberate: this runs before any single tenant has been picked. Unknown
    slugs raise rather than being skipped, because silently resetting "every
    clinic" when the operator typed one typo would be the worst possible answer.
    """
    if not slugs:
        return list_clinics(session)
    contexts: list[ClinicContext] = []
    missing: list[str] = []
    for slug in slugs:
        cleaned = (slug or "").strip().lower()
        clinic = find_clinic(session, cleaned)
        if clinic is None:
            missing.append(cleaned)
            continue
        contexts.append(context_from(session, clinic))
    if missing:
        raise UnknownClinic(", ".join(missing))
    return contexts


def parse_weekdays(spec: str | None) -> frozenset[int] | None:
    """``Mon,Tue`` or ``0,1,2`` into Python weekday ints. ``None`` when unreadable."""
    aliases = {
        "mon": 0,
        "tue": 1,
        "wed": 2,
        "thu": 3,
        "fri": 4,
        "sat": 5,
        "sun": 6,
    }
    days: set[int] = set()
    for part in (spec or "").split(","):
        token = part.strip().lower()
        if not token:
            continue
        if token in aliases:
            days.add(aliases[token])
        elif token.isdigit() and int(token) < 7:
            days.add(int(token))
        else:
            return None
    return frozenset(days) if days else None


def create_clinic(
    session: Session,
    *,
    slug: str,
    name: str,
    city: str,
    state: str,
    timezone: str,
    slot_minutes: int = 30,
    open_time,
    close_time,
    open_weekdays: frozenset[int],
    postal_code: str = "",
    phone: str = "",
    email: str = "",
    address_line1: str = "",
    closed_day_labels: tuple[str, ...] | None = None,
    seed_horizon_days: int = 60,
) -> Clinic:
    """Insert one tenant's configuration row.

    The row is what every later read of this clinic resolves against, so it has
    to be written before any child row can exist -- which is why creating a
    clinic never seeds data for it. ``updated_at`` moving forward is also what
    drops the in-process snapshot for this slug.
    """
    cleaned = (slug or "").strip().lower()
    if not cleaned:
        raise ValueError("slug is required")
    if find_clinic(session, cleaned) is not None:
        raise ValueError(f"clinic {cleaned} already exists")
    closed = closed_day_labels if closed_day_labels is not None else tuple(
        WEEKDAY_LABELS[index] for index in range(7) if index not in open_weekdays
    )
    clinic = Clinic(
        slug=cleaned,
        public_id=f"clinic_{hashlib.sha256(cleaned.encode()).hexdigest()[:12]}",
        name=name,
        address_line1=address_line1,
        city=city,
        state=state,
        postal_code=postal_code,
        phone=phone,
        email=email,
        timezone=timezone,
        slot_minutes=slot_minutes,
        open_time=open_time,
        close_time=close_time,
        open_weekdays=sorted(open_weekdays),
        closed_days=list(closed),
        seed_horizon_days=seed_horizon_days,
        self_pay_label="Self-pay",
        fictional=False,
        is_default=False,
        active=True,
    )
    session.add(clinic)
    session.flush()
    invalidate_cache(clinic.id)
    return clinic


def invalidate_cache(clinic_id: int | None = None) -> None:
    """Drop the snapshot for one clinic, or all of them after a bulk config edit."""
    with _CACHE_LOCK:
        if clinic_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(clinic_id, None)


__all__ = [
    "ClinicContext",
    "UnknownClinic",
    "context_from",
    "contexts_for_slugs",
    "create_clinic",
    "default_clinic",
    "find_clinic",
    "invalidate_cache",
    "list_clinics",
    "load_clinic_row",
    "parse_weekdays",
    "resolve_clinic",
    "resolve_clinic_or_default",
    "scheduling_config_for",
]
