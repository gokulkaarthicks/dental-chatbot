"""Scheduling tables: availability slots, appointments, and slot claims.

Slot model
----------
A provider's bookable grid is 30-minute intervals (``AvailabilitySlot``). An
appointment may span several consecutive intervals. ``SlotClaim`` joins the two:
one row per (appointment, interval) pair, with a UNIQUE constraint on
``availability_slot_id`` so a single interval can only ever belong to one active
appointment. That uniqueness is the database-level guarantee against overlapping
bookings -- the service layer checks first for good error messages, but the
constraint is the source of truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import AppointmentStatus, Base, ClinicScoped, SlotStatus, TimestampMixin

if TYPE_CHECKING:
    from app.models.patients import AppointmentType, Patient, Provider


class AvailabilitySlot(Base, ClinicScoped):
    """One booking interval in this clinic's own local wall-clock time.

    The interval length comes from the clinic row (``slot_minutes``), not a global
    constant, so two clinics can run different grids. ``starts_at`` is stored in
    each clinic's own timezone, which is why the tenant filter must be applied
    before any comparison: an Eastern 10:00 and a Pacific 10:00 are the same naive
    value and completely different appointments.
    """

    __tablename__ = "availability_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SlotStatus.AVAILABLE.value
    )
    # Phase 3: a slot held by an employee (blocked time, lunch, maintenance) is
    # never offered. This is a *schedule* hold owned by the clinic -- not a
    # patient proposal hold, which uses ``SlotClaim.held_by_proposal_id``.
    hold_reason: Mapped[str | None] = mapped_column(String(120))

    provider: Mapped[Provider] = relationship(back_populates="availability_slots")
    claim: Mapped[SlotClaim | None] = relationship(back_populates="slot", uselist=False)

    __table_args__ = (
        UniqueConstraint("provider_id", "starts_at", name="uq_slot_provider_start"),
        CheckConstraint("ends_at > starts_at", name="ck_slot_positive_duration"),
        CheckConstraint("status IN ('available', 'occupied', 'blocked')", name="ck_slot_status"),
        Index("ix_slots_starts_at", "starts_at"),
        Index("ix_slots_provider_status_start", "provider_id", "status", "starts_at"),
        Index("ix_slots_clinic_status_start", "clinic_id", "status", "starts_at"),
    )

    @property
    def duration_minutes(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds() // 60)

    @property
    def stable_id(self) -> str:
        """Stable, non-secret identifier used by the UI and the model.

        Derived from (provider, start) which is already UNIQUE, so it survives
        reseeding and never reveals a surrogate key.
        """
        return f"slot_{self.provider_id}_{self.starts_at.strftime('%Y%m%d%H%M')}"


class Appointment(Base, ClinicScoped, TimestampMixin):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    seed_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False
    )
    appointment_type_id: Mapped[int] = mapped_column(
        ForeignKey("appointment_types.id", ondelete="RESTRICT"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AppointmentStatus.BOOKED.value
    )
    reason: Mapped[str | None] = mapped_column(Text)
    # ``solo`` for a one-person booking, ``household`` for one visit per person
    # in a family/household request. Lets a later confirmation tell "this request
    # is already booked" apart from "this party wants a second visit". NULL on
    # rows written before the column existed.
    visit_shape: Mapped[str | None] = mapped_column(String(16))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime())
    family_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_groups.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # Phase 3: the group created by one confirmation (family bookings share one).
    booking_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("booking_groups.id", ondelete="SET NULL")
    )
    created_by: Mapped[str] = mapped_column(String(24), nullable=False, default="system")

    # --- Reschedule history ------------------------------------------------
    # A confirmed reschedule never rewrites the row it moves. The original is
    # marked ``rescheduled`` and points forward to the replacement; the
    # replacement points back here and keeps a snapshot of the time, provider and
    # duration it replaced, so either row explains the whole move on its own.
    rescheduled_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL")
    )
    rescheduled_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL")
    )
    rescheduled_at: Mapped[datetime | None] = mapped_column(DateTime())
    rescheduled_from_starts_at: Mapped[datetime | None] = mapped_column(DateTime())
    rescheduled_from_provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("providers.id", ondelete="SET NULL")
    )
    rescheduled_from_appointment_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointment_types.id", ondelete="SET NULL")
    )
    rescheduled_from_duration_minutes: Mapped[int | None] = mapped_column()

    patient: Mapped[Patient] = relationship(back_populates="appointments")
    provider: Mapped[Provider] = relationship(
        back_populates="appointments", foreign_keys="Appointment.provider_id"
    )
    appointment_type: Mapped[AppointmentType] = relationship(
        foreign_keys="Appointment.appointment_type_id"
    )
    claims: Mapped[list[SlotClaim]] = relationship(
        back_populates="appointment", cascade="all, delete-orphan"
    )

    @property
    def is_reschedule_replacement(self) -> bool:
        """True when this row was created by confirming a reschedule.

        A replacement is a real booking but is *not* counted again by "upcoming
        stays": its original is its other half, and listing both would make one
        move look like two appointments.
        """
        return self.rescheduled_from_id is not None

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_appointment_positive_duration"),
        CheckConstraint("duration_minutes > 0", name="ck_appointment_duration"),
        CheckConstraint(
            "status IN ('booked', 'cancelled', 'completed', 'rescheduled')",
            name="ck_appointment_status",
        ),
        Index("ix_appointments_provider_start", "provider_id", "starts_at"),
        Index("ix_appointments_patient_start", "patient_id", "starts_at"),
        Index("ix_appointments_clinic_provider_start", "clinic_id", "provider_id", "starts_at"),
        Index("ix_appointments_clinic_patient_start", "clinic_id", "patient_id", "starts_at"),
        Index("ix_appointments_status", "status"),
    )


class SlotClaim(Base, ClinicScoped, TimestampMixin):
    """Reserves one slot interval for one appointment or one proposal.

    ``active`` flips to false (instead of deleting the row) when an appointment
    is cancelled, keeping an audit trail. Partial unique indexes allow one
    *active* claim per slot while permitting historical cancelled claims.

    Phase 3 reuses this single table to hold intervals for a proposal:

    * ``appointment_id`` set, ``held_by_proposal_id`` NULL  -> booked
    * both NULL, ``held_by_proposal_id`` set                -> proposal hold
    * both NULL, neither set                                -> released

    The partial unique indexes do not reference the proposal column, so a slot
    can only ever carry one active claim of *either* kind. That is what makes
    "two patients pick the same slot" a database-level conflict rather than a
    race.

    Because there is only one active claim per slot, a reschedule or
    cancellation proposal holds nothing at creation time: the target
    appointment's own claim is the reservation. It is therefore recorded in
    ``booking_proposals.held_slot_ids`` (JSON) so confirmation and supersede can
    tell which intervals "this proposal frees" and treat them as still available
    only for itself. ``release_holds`` does the same for ordinary holds.
    """

    __tablename__ = "slot_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    availability_slot_id: Mapped[int] = mapped_column(
        ForeignKey("availability_slots.id", ondelete="CASCADE"), nullable=False
    )
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE")
    )
    held_by_proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("booking_proposals.id", ondelete="CASCADE")
    )
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime())

    slot: Mapped[AvailabilitySlot] = relationship(back_populates="claim")
    appointment: Mapped[Appointment | None] = relationship(back_populates="claims")

    @property
    def is_hold(self) -> bool:
        return self.appointment_id is None and self.held_by_proposal_id is not None

    __table_args__ = (
        Index(
            "uq_slot_claims_active_slot",
            "availability_slot_id",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
        Index(
            "uq_slot_claims_active_appointment_slot",
            "appointment_id",
            "availability_slot_id",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
        Index("ix_slot_claims_appointment", "appointment_id"),
        Index("ix_slot_claims_clinic_active", "clinic_id", "active"),
    )
