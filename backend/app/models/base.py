"""Shared model machinery: declarative base, enums, column mixins, tenancy hooks.

Tenancy lives here rather than in a service so that no data path can opt out of
it: the ``ClinicScoped`` mixin is what marks a table as belonging to one clinic,
and the two event hooks below are what make the marker enforce itself.

* ``do_orm_execute`` appends a ``clinic_id`` criterion to every ORM select issued
  by a session that carries a tenant. That is the safety net for entity selects
  and relationship loads; bare-column selects are not rewritten by it, so those
  keep their explicit ``.where(Model.clinic_id == ...)``.
* ``before_flush`` rejects a tenant-owned row that is being inserted, updated
  *or deleted* whose ``clinic_id`` is unset or differs from the session's
  tenant. A forgotten stamp therefore fails loudly instead of writing -- or
  removing -- one clinic's record in another clinic's world.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.event import listens_for
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    with_loader_criteria,
)
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime stored as ISO text with an explicit offset.

    A custom type (rather than relying on SQLite's native datetime handling) is
    used so values always round-trip with their offset intact. Aware values are
    converted to UTC; naive values are assumed to already be UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None or value.tzinfo is None:
            return value
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )


class TenantMismatch(RuntimeError):
    """A write was attempted for a tenant the session is not bound to."""


class ClinicScoped:
    """Mixin for every table that belongs to exactly one clinic.

    ``index=True`` gives each table its own ``ix_<table>_clinic_id`` index, which
    is what keeps a tenant-filtered scan cheap as the number of clinics grows.
    """

    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="RESTRICT"), nullable=False, index=True
    )


def _tenant_scoped_classes() -> tuple[type, ...]:
    """Every mapped class carrying ``clinic_id``, discovered once.

    Discovery (rather than a hand-kept list) means a new tenant table is
    enforced automatically the moment it uses the mixin.
    """
    global _TENANT_SCOPED_CLASSES
    if _TENANT_SCOPED_CLASSES is None:
        _TENANT_SCOPED_CLASSES = tuple(
            mapper.class_
            for mapper in Base.registry.mappers
            if "clinic_id" in mapper.local_table.columns
        )
    return _TENANT_SCOPED_CLASSES


_TENANT_SCOPED_CLASSES: tuple[type, ...] | None = None


def _tenant_criterion(model: type, clinic_id: int):  # noqa: ANN202
    """One loader criterion pinned to ``clinic_id``.

    The value has to reach the lambda as a *closure* variable, never as a default
    argument. SQLAlchemy caches a criteria lambda's SQL and re-evaluates only the
    closures it tracks, so a default-argument value is baked into the first
    statement it compiles: every later clinic would then be filtered by the
    clinic that happened to run first, and its own rows would read as missing.
    """

    def criterion(cls):  # noqa: ANN001, ANN202
        return cls.clinic_id == clinic_id

    return with_loader_criteria(model, criterion, include_aliases=True)


@listens_for(Session, "do_orm_execute")
def _scope_selects_to_tenant(execute_state) -> None:  # noqa: ANN001
    """Bind every tenant-owned entity in a select to the session's clinic.

    Opt-out is explicit: ``execution_options(tenant_scoped=False)``. The only
    legitimate unscoped reads are credential lookups, where the token is a random
    256-bit digest that identifies one clinic rather than leaking another's data,
    and the admin/bootstrap paths that run before a tenant exists.
    """
    if not execute_state.is_select:
        return
    if execute_state.execution_options.get("tenant_scoped", True) is False:
        return
    # Column and relationship loads are issued by the loader itself. A criteria
    # option attached to the primary statement already propagates into them, so
    # re-wrapping here would double the predicate instead of adding reach.
    if execute_state.is_column_load or execute_state.is_relationship_load:
        return
    clinic_id = execute_state.session.info.get("clinic_id")
    if clinic_id is None:
        return
    statement = execute_state.statement
    options = [_tenant_criterion(model, clinic_id) for model in _tenant_scoped_classes()]
    execute_state.statement = statement.options(*options)


def _check_row(row: object, clinic_id: int) -> None:
    value = getattr(row, "clinic_id", None)
    if value is None:
        raise TenantMismatch(
            f"{type(row).__name__} written without clinic_id (session tenant {clinic_id})"
        )
    if int(value) != int(clinic_id):
        raise TenantMismatch(
            f"{type(row).__name__} carries clinic_id={value}; "
            f"this session is bound to clinic_id={clinic_id}"
        )


@listens_for(Session, "before_flush")
def _reject_cross_tenant_writes(session: Session, flush_context, instances) -> None:  # noqa: ANN001
    del flush_context, instances
    clinic_id = session.info.get("clinic_id")
    if clinic_id is None:
        return
    # Deleting another tenant's row is the same leak as writing one: the row is
    # gone either way. The same check covers both, so a future edit cannot
    # quietly drop one verb and leave the other guarded.
    #
    # Only ORM-level operations are visible here. Core ``session.execute(delete(
    # ...))`` (the demo reset's table wipe) never populates ``session.deleted``,
    # so an admin-level bulk statement is deliberately out of scope: those must
    # scope themselves, and every one in this codebase does.
    for pending in list(session.new) + list(session.dirty) + list(session.deleted):
        mapper = type(pending).__mapper__
        if "clinic_id" in mapper.local_table.columns:
            _check_row(pending, clinic_id)


class SlotStatus(StrEnum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    BLOCKED = "blocked"


class AppointmentStatus(StrEnum):
    BOOKED = "booked"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    # Moved to a replacement appointment (see ``Appointment.rescheduled_to_id``).
    # Inactive like ``cancelled``: the row keeps its slot/time history but no
    # longer occupies a slot or counts as an upcoming visit.
    RESCHEDULED = "rescheduled"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    # Replaced by a newer proposal for the same patient (the patient changed
    # their mind). Kept distinct from ``rejected`` so the UI can say "updated"
    # rather than "declined", and its held intervals were released.
    SUPERSEDED = "superseded"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class MessageRole(StrEnum):
    PATIENT = "patient"
    ASSISTANT = "assistant"
    STAFF = "staff"
    SYSTEM = "system"


class StaffRole(StrEnum):
    RECEPTIONIST = "receptionist"
    CLINIC_MANAGER = "clinic_manager"


class AlertStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class AlertKind(StrEnum):
    """Alert categories. Values are stored, so keep them stable."""

    EMERGENCY = "emergency"
    URGENT_CALLBACK = "urgent_callback"
    INSURANCE_VERIFICATION = "insurance_verification"
    ASSISTANCE_REQUEST = "assistance_request"


class RequestType(StrEnum):
    CALLBACK = "callback"
    RESCHEDULE = "reschedule"
    CLINICAL_QUESTION = "clinical_question"
    BILLING = "billing"


class RequestStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
