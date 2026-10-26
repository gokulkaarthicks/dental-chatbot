"""Operational tables: idempotency records, audit log, application metadata."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, ClinicScoped, TimestampMixin, UTCDateTime


class IdempotencyRecord(Base, ClinicScoped):
    """Deduplicates side-effecting requests (e.g. a retried booking).

    ``scope`` namespaces keys per operation, so the same client key can be reused
    for different endpoints without collisions. The clinic is part of the
    namespace: one dashboard driving two clinics may legitimately send the same
    ``Idempotency-Key`` to both, and each must replay its own result.
    """

    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(128))
    response_status: Mapped[int | None] = mapped_column(Integer())
    response_body: Mapped[str | None] = mapped_column(Text)
    # Phase 3: a row inserted in a committed transaction *before* the
    # side-effecting write. Its UNIQUE(scope, key) index is what makes a
    # concurrent duplicate fail immediately instead of racing.
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="complete", server_default=text("'complete'")
    )
    owner_token: Mapped[str | None] = mapped_column(String(64))
    reset_generation: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime())

    __table_args__ = (
        Index("uq_idempotency_scope_key", "clinic_id", "scope", "key", unique=True),
        Index("ix_idempotency_expires", "expires_at"),
        CheckConstraint("state IN ('in_flight', 'complete')", name="ck_idempotency_state"),
    )


class AuditRecord(Base, ClinicScoped):
    """Append-only record of state changes. Never updated or deleted at runtime."""

    __tablename__ = "audit_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False, default="system")
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    # Phase 3: audit rows are written twice for a booking -- once as a HOLD
    # inside the write transaction, then COMMIT once the transaction is known to
    # have committed. A reader can therefore ignore uncommitted trailer rows
    # emitted by a rolled-back transaction (the sqlite3 driver does not always
    # report commit failures to the caller).
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="COMMIT", server_default=text("'COMMIT'")
    )
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    details: Mapped[str | None] = mapped_column(Text)
    reset_generation: Mapped[str] = mapped_column(String(36), nullable=False)

    __table_args__ = (
        Index("ix_audit_occurred", "occurred_at"),
        Index("ix_audit_action", "action"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_clinic_occurred", "clinic_id", "occurred_at"),
    )


class AppMetadata(Base):
    """Small key/value store for *per-clinic* application state.

    Holds ``reset_generation`` (a UUID regenerated on every demo reset, consumed
    by the browser to discard stale conversation/session state), ``reset_count``,
    and ``last_seeded_at``.

    The primary key is ``(clinic_id, key)`` rather than ``key`` alone: resetting
    one clinic rotates *its* generation only, so another clinic's open chats are
    not invalidated by someone else's ``make reset-demo``. The one genuinely
    global key, ``schema_version``, is written by an unscoped session during
    bootstrap and read before any tenant exists.

    ``public`` marks keys that are safe to expose through ``/api/demo/status``.
    Patient-identifying counters (e.g. ``patients_registered_total``) are stored
    here too, but with ``public = false`` so they never leave the server.
    """

    __tablename__ = "app_metadata"

    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )


class SeedManifest(Base, ClinicScoped, TimestampMixin):
    """Records which fixture rows this application created, for safe reseeding.

    Matching happens on the natural ``seed_key`` of each table; this manifest is
    the summary used by ``/api/demo/status`` and makes it easy to see, at a
    glance, that the seed is reproducible and was applied exactly once.

    ``fixture_set`` is unique per clinic, because every tenant applies a set with
    the same name.
    """

    __tablename__ = "seed_manifests"

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_set: Mapped[str] = mapped_column(String(64), nullable=False)
    fixture_version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("clinic_id", "fixture_set", name="uq_seed_manifest_clinic_set"),
    )
