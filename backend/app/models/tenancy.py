"""Tenant (clinic) tables: the clinic row itself, plus its per-clinic knowledge.

Every other table in this schema carries ``clinic_id`` and hangs off one of these
rows. The clinic record is *configuration*, not patient data: it holds identity
(address, phone), the scheduling parameters the availability engine needs
(timezone, slot length, business hours), and the two knowledge tables the
assistant is allowed to quote from.

Why hours and slot length live here rather than in ``app/timeutil.py``: a second
clinic that books on a 20-minute grid in Eastern time cannot be expressed by a
module constant, and the constants would make the two clinics silently share one
grid. ``app/tenancy.py`` turns these columns into a ``SchedulingConfig``.

``is_default`` exists so a request that sends no ``X-Clinic`` header still
resolves. That keeps the existing single-clinic client working unchanged while
tenant selection is opt-in.
"""

from __future__ import annotations

from datetime import time

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Clinic(Base, TimestampMixin):
    """One tenant: a dental practice with its own staff, patients and schedule."""

    __tablename__ = "clinics"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable, URL-safe handle used by the ``X-Clinic`` header and the public
    # clinic directory. Never derived from ``id`` so a reseed cannot shuffle it.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    legal_note: Mapped[str | None] = mapped_column(Text)
    address_line1: Mapped[str] = mapped_column(String(160), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(160))
    city: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(16), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(160), nullable=False)

    # --- Scheduling parameters (were module constants in app/timeutil.py) ---
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_minutes: Mapped[int] = mapped_column(nullable=False, default=30)
    open_time: Mapped[time] = mapped_column(Time, nullable=False)
    close_time: Mapped[time] = mapped_column(Time, nullable=False)
    # Python weekday ints (Monday == 0) that the clinic accepts bookings on.
    open_weekdays: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    # Day labels the clinic is closed on, for the hours text shown to patients.
    closed_days: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    seed_horizon_days: Mapped[int] = mapped_column(nullable=False, default=60)

    parking_note: Mapped[str | None] = mapped_column(Text)
    timezone_note: Mapped[str | None] = mapped_column(Text)
    insurance_note: Mapped[str | None] = mapped_column(Text)
    # Self-pay wording. Stored per clinic because the label differs by practice.
    self_pay_label: Mapped[str] = mapped_column(
        String(48), nullable=False, default="Self-pay", server_default=text("'Self-pay'")
    )

    fictional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    # Only one clinic may be flagged. Resolution falls back to it when a request
    # carries no ``X-Clinic`` header.
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )

    insurance_plans: Mapped[list[ClinicInsurancePlan]] = relationship(
        back_populates="clinic",
        cascade="all, delete-orphan",
        order_by="ClinicInsurancePlan.id",
    )
    policies: Mapped[list[ClinicPolicy]] = relationship(
        back_populates="clinic",
        cascade="all, delete-orphan",
        order_by="ClinicPolicy.id",
    )

    __table_args__ = (Index("ix_clinic_is_default", "is_default"),)

    @property
    def location(self) -> str:
        return f"{self.city}, {self.state}"


class ClinicInsurancePlan(Base):
    """A carrier this clinic accepts. The only source the assistant may quote.

    ``accepted`` is stored rather than implied so a clinic can record a plan it
    deliberately does *not* take, and the assistant can say so plainly instead of
    falling back to "check with the front desk".
    """

    __tablename__ = "clinic_insurance_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    accepted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    note: Mapped[str | None] = mapped_column(Text)

    clinic: Mapped[Clinic] = relationship(back_populates="insurance_plans")

    __table_args__ = (
        UniqueConstraint("clinic_id", "name", name="uq_clinic_plan_name"),
        Index("ix_clinic_plans_clinic", "clinic_id"),
    )


class ClinicPolicy(Base):
    """One answerable clinic question: hours, prices, privacy, providers.

    ``topic`` is the stable lookup key the agent passes to ``get_practice_info``.
    ``answer`` must be self-contained because the assistant relays it rather than
    paraphrasing freely, which is what stops it inventing a price or a coverage
    promise. ``keywords`` are stored as JSON so a clinic can add its own phrasings
    without a schema change.
    """

    __tablename__ = "clinic_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    clinic: Mapped[Clinic] = relationship(back_populates="policies")

    __table_args__ = (
        UniqueConstraint("clinic_id", "topic", name="uq_clinic_policy_topic"),
        Index("ix_clinic_policies_clinic", "clinic_id"),
    )


__all__ = ["Clinic", "ClinicInsurancePlan", "ClinicPolicy"]
