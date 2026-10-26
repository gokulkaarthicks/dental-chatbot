"""Append-only audit trail helpers."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import bound_clinic_id, require_clinic_id
from app.models import AuditRecord, utcnow
from app.services.generations import get_generation


def record(
    session: Session,
    *,
    action: str,
    actor_type: str = "system",
    actor_id: str | int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    conversation_id: int | None = None,
    details: dict[str, Any] | str | None = None,
    visibility: str = "COMMIT",
) -> AuditRecord:
    """Add an audit row to the current transaction (caller commits).

    Phase 3 writes bookings as a *pair*: a ``HOLD`` row inside the write
    transaction and a ``COMMIT`` trailer after the transaction is known to have
    committed. ``find_trailer`` only trusts a matching ``COMMIT`` row, so a
    transaction that quietly failed to commit cannot masquerade as a booking.
    """
    if isinstance(details, dict):
        serialised = json.dumps(details, sort_keys=True, default=str)
    else:
        serialised = details
    # The tenant comes from the session rather than an argument: an audit row
    # records what *this* transaction did, so letting a caller name a clinic
    # would be the one way to write a finding against somebody else's record.
    clinic_id = require_clinic_id(session)
    entry = AuditRecord(
        clinic_id=clinic_id,
        occurred_at=utcnow(),
        actor_type=actor_type,
        actor_id=None if actor_id is None else str(actor_id),
        action=action,
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        conversation_id=conversation_id,
        details=serialised,
        reset_generation=get_generation(session, clinic_id=clinic_id) or "unknown",
        visibility=visibility,
    )
    session.add(entry)
    return entry


def trailer_committed(session: Session, trailer_key: str, entity_type: str | None = None) -> bool:
    """True when a COMMIT trailer for ``trailer_key`` is visible.

    Call this in a *fresh* transaction after a booking commit. If the write
    transaction did not actually commit, its trailer is absent, so absence is a
    reliable negative. Parsing happens in Python because the details column is
    a JSON string written by :func:`record`.
    """
    statement = select(AuditRecord.details).where(
        AuditRecord.action == "booking.commit",
        AuditRecord.visibility == "COMMIT",
    )
    # ``details`` is a projection, so the ORM tenant criterion is not applied for
    # us. Only filter when the session knows its tenant -- a bootstrap read has
    # none and legitimately sees the whole file.
    clinic_id = bound_clinic_id(session)
    if clinic_id is not None:
        statement = statement.where(AuditRecord.clinic_id == clinic_id)
    if entity_type:
        statement = statement.where(AuditRecord.entity_type == entity_type)
    for details in session.scalars(statement):
        try:
            payload = json.loads(details or "{}")
        except ValueError:
            continue
        if payload.get("trailer_key") == trailer_key:
            return True
    return False
