"""People and catalogue tables: patients, providers, appointment types."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.family import FamilyMember
    from app.models.scheduling import Appointment, AvailabilitySlot


class Patient(Base, TimestampMixin):
    """A patient record.

    Phone numbers are deliberately **not unique**: family members routinely share
    one household number, and matching is resolved by name/DOB conversationally.
    """

    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    seed_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    insurance_provider: Mapped[str | None] = mapped_column(String(120))
    insurance_member_id: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)

    appointments: Mapped[list[Appointment]] = relationship(back_populates="patient")
    family_memberships: Mapped[list[FamilyMember]] = relationship(
        back_populates="patient",
        foreign_keys="FamilyMember.patient_id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_patients_phone", "phone"),
        Index("ix_patients_full_name", "full_name"),
    )


class Provider(Base, TimestampMixin):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    seed_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    credentials: Mapped[str] = mapped_column(String(40), nullable=False)
    email: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    bio: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)

    specialties: Mapped[list[ProviderSpecialty]] = relationship(
        back_populates="provider",
        cascade="all, delete-orphan",
        order_by="ProviderSpecialty.id",
    )
    availability_slots: Mapped[list[AvailabilitySlot]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )
    appointments: Mapped[list[Appointment]] = relationship(
        back_populates="provider", foreign_keys="Appointment.provider_id"
    )

    @property
    def specialty_names(self) -> list[str]:
        return [specialty.specialty for specialty in self.specialties]


class ProviderSpecialty(Base):
    """One row per specialty so a provider can hold several."""

    __tablename__ = "provider_specialties"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    specialty: Mapped[str] = mapped_column(String(80), nullable=False)

    provider: Mapped[Provider] = relationship(back_populates="specialties")

    __table_args__ = (
        UniqueConstraint("provider_id", "specialty", name="uq_provider_specialty"),
        Index("ix_provider_specialties_specialty", "specialty"),
    )


class AppointmentType(Base):
    """Bookable visit kinds. ``code`` is a stable slug used by the assistant."""

    __tablename__ = "appointment_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(nullable=False)
    is_emergency: Mapped[bool] = mapped_column(default=False, nullable=False)
    required_specialty: Mapped[str | None] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)
