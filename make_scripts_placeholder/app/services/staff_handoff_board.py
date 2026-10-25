"""Staff-side triage of alerts and pending requests (Phase 4).

Two queues, two questions
-------------------------
**Alerts** are things the assistant escalated: an emergency report, a caller who
may walk in, an insurance mismatch. They are not approvals -- they are
*awareness*. The desk acknowledges (someone has seen it) and later resolves (it
is dealt with). Both transitions are persisted with who did it and when.

**Pending requests** are things the assistant could not answer or do: coverage,
pricing, financing, a clinical question, a callback. Most just need a human
answer, so they move ``pending -> in_progress -> resolved``. A subset genuinely
needs an *approval* decision before the clinic can proceed; those carry
``approval_required``.

Why approval is derived, not stored
-----------------------------------
``approval_required`` is computed from the request type:

* billing / insurance / financing / membership -> approval required
* callback, clinical question, reschedule -> **not** approval required

Storing a mutable boolean would let the two disagree with reality -- a bug where
a routine callback is suddenly "approved" or an insurance question is silently
closed as routine. Deriving it means the rule lives in one place and cannot
drift from the row it describes.

Consistency with the patient flow
---------------------------------
A routine booking the *patient already confirmed* lands as a normal
``appointments`` row with a ``booking_group_id``. Nothing here creates a
"needs approval" record for those, so a confirmed booking never triggers an extra
staff step. That is asserted directly in :func:`approval_required`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AlertKind,
    AlertStatus,
    Appointment,
    AuditRecord,
    Patient,
    PendingRequest,
    RequestStatus,
    RequestType,
    StaffAlert,
    StaffUser,
    utcnow,
)
from app.services import audit
from app.services.transactions import WriteTransactionError, write_transaction
from app.timeutil import clinic_now

logger = logging.getLogger(__name__)

MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 100

# Request types where a human must make a decision, not just reply. Kept as a
# set rather than a column so the rule cannot drift from the data.
APPROVAL_REQUEST_TYPES = frozenset(
    {
        RequestType.BILLING.value,
        RequestType.RESCHEDULE.value,
    }
)

ALERT_TRANSITIONS = {
    AlertStatus.OPEN.value: {AlertStatus.ACKNOWLEDGED.value, AlertStatus.RESOLVED.value},
    AlertStatus.ACKNOWLEDGED.value: {AlertStatus.RESOLVED.value, AlertStatus.OPEN.value},
    AlertStatus.RESOLVED.value: {AlertStatus.OPEN.value},
}

REQUEST_TRANSITIONS = {
    RequestStatus.PENDING.value: {RequestStatus.IN_PROGRESS.value, RequestStatus.RESOLVED.value},
    RequestStatus.IN_PROGRESS.value: {RequestStatus.RESOLVED.value, RequestStatus.PENDING.value},
    RequestStatus.RESOLVED.value: {RequestStatus.IN_PROGRESS.value},
}


class HandoffError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AlertFilters:
    status: str | None = None
    kind: str | None = None
    severity: str | None = None
    search: str | None = None
    limit: int = DEFAULT_LIST_LIMIT


@dataclass
class RequestFilters:
    status: str | None = None
    request_type: str | None = None
    search: str | None = None
    limit: int = DEFAULT_LIST_LIMIT


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def approval_required(*, request_type: str | None, appointment: Appointment | None = None) -> bool:
    """Whether this item needs an explicit staff approval decision.

    An item tied to an appointment the patient already confirmed is *never*
    approval-gated: the patient made the decision, the booking committed, and
    adding a second gate would imply a routine booking is unconfirmed.
    """
    if appointment is not None and appointment.status == "booked":
        return False
    return (request_type or "") in APPROVAL_REQUEST_TYPES


def _severity_rank(severity: str) -> int:
    return {"urgent": 0, "high": 1, "normal": 2, "low": 3}.get(severity, 2)


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def alert_dict(session: Session, row: StaffAlert) -> dict[str, Any]:
    patient = session.get(Patient, row.patient_id) if row.patient_id else None
    conversation_public_id = None
    if row.conversation_id is not None:
        from app.models import Conversation

        conversation = session.get(Conversation, row.conversation_id)
        conversation_public_id = conversation.public_id if conversation else None

    return {
        "id": row.id,
        "kind": row.kind,
        "is_urgent": row.severity in {"urgent", "high"} or row.kind == AlertKind.EMERGENCY.value,
        "severity": row.severity,
        "message": row.message,
        "status": row.status,
        "report_count": row.report_count or 1,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
        "last_reported_at": row.last_reported_at.isoformat(timespec="seconds")
        if row.last_reported_at
        else None,
        "acknowledged_at": row.acknowledged_at.isoformat(timespec="seconds")
        if row.acknowledged_at
        else None,
        "resolved_at": row.resolved_at.isoformat(timespec="seconds") if row.resolved_at else None,
        "resolution_note": row.resolution_note,
        "patient": {
            "id": patient.id,
            "full_name": patient.full_name,
            "phone": patient.phone,
        }
        if patient
        else None,
        "conversation_id": conversation_public_id,
        # A caller who is not on a chart yet: the only way to reach them.
        # ``patient`` above stays null in that case, so the board must not show
        # this as unidentified.
        "caller": {
            "name": row.caller_name,
            "phone": row.caller_phone,
        }
        if (row.caller_name or row.caller_phone)
        else None,
    }


def request_dict(session: Session, row: PendingRequest) -> dict[str, Any]:
    patient = session.get(Patient, row.patient_id) if row.patient_id else None
    conversation_public_id = None
    if row.conversation_id is not None:
        from app.models import Conversation

        conversation = session.get(Conversation, row.conversation_id)
        conversation_public_id = conversation.public_id if conversation else None

    assigned = session.get(StaffUser, row.assigned_to) if row.assigned_to else None
    approved_by = session.get(StaffUser, row.approved_by) if row.approved_by else None
    return {
        "id": row.id,
        "request_type": row.request_type,
        "status": row.status,
        "details": row.details,
        "approval_required": approval_required(request_type=row.request_type),
        "approved_at": row.approved_at.isoformat(timespec="seconds") if row.approved_at else None,
        "approved_by": approved_by.display_name if approved_by else None,
        "assigned_to": assigned.display_name if assigned else None,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
        "resolved_at": row.resolved_at.isoformat(timespec="seconds") if row.resolved_at else None,
        "resolution_note": row.resolution_note,
        "preferred_callback_at": row.preferred_callback_at.isoformat(timespec="minutes")
        if row.preferred_callback_at
        else None,
        "patient": {
            "id": patient.id,
            "full_name": patient.full_name,
            "phone": patient.phone,
        }
        if patient
        else None,
        "conversation_id": conversation_public_id,
    }


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def list_alerts(session: Session, filters: AlertFilters) -> list[StaffAlert]:
    statement: Select = select(StaffAlert)
    if filters.status:
        statement = statement.where(StaffAlert.status == filters.status)
    if filters.kind:
        statement = statement.where(StaffAlert.kind == filters.kind)
    if filters.severity:
        statement = statement.where(StaffAlert.severity == filters.severity)
    if filters.search:
        needle = f"%{filters.search.strip().lower()}%"
        statement = statement.outerjoin(Patient, StaffAlert.patient_id == Patient.id).where(
            or_(
                func.lower(StaffAlert.message).like(needle),
                func.lower(Patient.full_name).like(needle),
            )
        )
    statement = statement.order_by(StaffAlert.created_at.desc(), StaffAlert.id.desc())
    rows = list(session.scalars(statement.limit(min(filters.limit, MAX_LIST_LIMIT))))

    # Unresolved first, then by urgency so the worst thing is at the top.
    return sorted(
        rows,
        key=lambda row: (
            row.status == AlertStatus.RESOLVED.value,
            _severity_rank(row.severity),
            -(row.created_at.timestamp() if row.created_at else 0),
        ),
    )


def list_requests(session: Session, filters: RequestFilters) -> list[PendingRequest]:
    statement: Select = select(PendingRequest)
    if filters.status:
        statement = statement.where(PendingRequest.status == filters.status)
    if filters.request_type:
        statement = statement.where(PendingRequest.request_type == filters.request_type)
    if filters.search:
        needle = f"%{filters.search.strip().lower()}%"
        statement = statement.outerjoin(
            Patient, PendingRequest.patient_id == Patient.id
        ).where(
            or_(
                func.lower(PendingRequest.details).like(needle),
                func.lower(Patient.full_name).like(needle),
            )
        )
    statement = statement.order_by(PendingRequest.created_at.desc(), PendingRequest.id.desc())
    rows = list(session.scalars(statement.limit(min(filters.limit, MAX_LIST_LIMIT))))
    return sorted(
        rows,
        key=lambda row: (
            row.status == RequestStatus.RESOLVED.value,
            row.status != RequestStatus.PENDING.value,
            -(row.created_at.timestamp() if row.created_at else 0),
        ),
    )


def get_alert(session: Session, alert_id: int) -> StaffAlert | None:
    return session.get(StaffAlert, alert_id)


def get_request(session: Session, request_id: int) -> PendingRequest | None:
    return session.get(PendingRequest, request_id)


def alert_activity(session: Session, alert_id: int) -> list[dict[str, Any]]:
    return _activity(session, "staff_alert", alert_id)


def request_activity(session: Session, request_id: int) -> list[dict[str, Any]]:
    return _activity(session, "pending_request", request_id)


def _activity(session: Session, entity_type: str, entity_id: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(AuditRecord)
        .where(AuditRecord.entity_type == entity_type, AuditRecord.entity_id == str(entity_id))
        .order_by(AuditRecord.id.desc())
        .limit(25)
    )
    return [
        {
            "action": row.action,
            "actor_type": row.actor_type,
            "actor_id": row.actor_id,
            "occurred_at": row.occurred_at.isoformat(timespec="seconds")
            if row.occurred_at
            else None,
            "details": row.details,
        }
        for row in rows
    ]


def summary_counts(session: Session, *, today: date | None = None) -> dict[str, Any]:
    """Counts for the dashboard tiles. One round-trip, no row loading.

    "Today" is the *clinic* day. Appointment timestamps are stored as naive
    clinic-local values, so the bounds must be naive too: binding an aware UTC
    value into SQLite sorts before every stored local timestamp and would
    silently undercount the day.
    """
    day = today or clinic_now().date()
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    def count(statement) -> int:
        return int(session.scalar(statement) or 0)

    booked = Appointment.status == "booked"
    return {
        "appointments_today": count(
            select(func.count(Appointment.id)).where(
                Appointment.starts_at >= day_start,
                Appointment.starts_at < day_end,
                Appointment.status != "cancelled",
            )
        ),
        "appointments_upcoming": count(
            select(func.count(Appointment.id)).where(booked, Appointment.starts_at >= day_start)
        ),
        "open_alerts": count(
            select(func.count(StaffAlert.id)).where(StaffAlert.status != AlertStatus.RESOLVED.value)
        ),
        "urgent_alerts": count(
            select(func.count(StaffAlert.id)).where(
                StaffAlert.status != AlertStatus.RESOLVED.value,
                StaffAlert.severity.in_(["urgent", "high"]),
            )
        ),
        "pending_requests": count(
            select(func.count(PendingRequest.id)).where(
                PendingRequest.status != RequestStatus.RESOLVED.value
            )
        ),
        "approvals_required": count(
            select(func.count(PendingRequest.id)).where(
                PendingRequest.status != RequestStatus.RESOLVED.value,
                PendingRequest.request_type.in_(sorted(APPROVAL_REQUEST_TYPES)),
            )
        ),
    }


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #


def set_alert_status(
    session: Session,
    *,
    staff: StaffUser,
    alert_id: int,
    status: str,
    note: str | None = None,
) -> StaffAlert:
    """Move an alert between open / acknowledged / resolved, with an audit row."""
    alert = get_alert(session, alert_id)
    if alert is None:
        raise HandoffError("unknown_alert", "That alert no longer exists.")
    if status not in ALERT_TRANSITIONS.get(alert.status, set()):
        raise HandoffError(
            "invalid_transition",
            f"An alert that is {alert.status} cannot become {status}.",
        )

    try:
        with write_transaction(session) as txn:
            now = utcnow()
            previous = alert.status
            alert.status = status
            if status == AlertStatus.ACKNOWLEDGED.value:
                alert.acknowledged_at = now
                alert.acknowledged_by = staff.id
            elif status == AlertStatus.RESOLVED.value:
                alert.resolved_at = now
                alert.resolution_note = note or alert.resolution_note
                if alert.acknowledged_at is None:
                    # Resolving implies it was seen; record that rather than
                    # leaving an acknowledged field that disagrees with status.
                    alert.acknowledged_at = now
                    alert.acknowledged_by = staff.id
            elif status == AlertStatus.OPEN.value:
                # Reopening clears the resolution but keeps the history rows.
                alert.resolved_at = None
            _record(
                session,
                entity_type="staff_alert",
                entity_id=alert.id,
                action=f"alert.{status}",
                staff=staff,
                conversation_id=alert.conversation_id,
                details={"from": previous, "to": status, "note": note},
            )
            txn.commit()
    except WriteTransactionError as exc:
        raise HandoffError("busy", "The dashboard is busy. Try again.") from exc
    return alert


def set_alert_severity(
    session: Session, *, staff: StaffUser, alert_id: int, severity: str
) -> StaffAlert:
    """Re-triage an alert's severity. Recorded, never silent."""
    if severity not in {"low", "normal", "high", "urgent"}:
        raise HandoffError("invalid_severity", "That severity is not valid.")
    alert = get_alert(session, alert_id)
    if alert is None:
        raise HandoffError("unknown_alert", "That alert no longer exists.")
    try:
        with write_transaction(session) as txn:
            previous = alert.severity
            alert.severity = severity
            _record(
                session,
                entity_type="staff_alert",
                entity_id=alert.id,
                action="alert.severity_changed",
                staff=staff,
                conversation_id=alert.conversation_id,
                details={"from": previous, "to": severity},
            )
            txn.commit()
    except WriteTransactionError as exc:
        raise HandoffError("busy", "The dashboard is busy. Try again.") from exc
    return alert


def set_request_status(
    session: Session,
    *,
    staff: StaffUser,
    request_id: int,
    status: str,
    note: str | None = None,
) -> PendingRequest:
    """Move a request through pending / in_progress / resolved."""
    request = get_request(session, request_id)
    if request is None:
        raise HandoffError("unknown_request", "That request no longer exists.")
    if status not in REQUEST_TRANSITIONS.get(request.status, set()):
        raise HandoffError(
            "invalid_transition",
            f"A request that is {request.status} cannot become {status}.",
        )
    try:
        with write_transaction(session) as txn:
            previous = request.status
            request.status = status
            if status == RequestStatus.IN_PROGRESS.value:
                request.assigned_to = staff.id
            elif status == RequestStatus.RESOLVED.value:
                request.resolved_at = utcnow()
                request.resolution_note = note or request.resolution_note
                request.assigned_to = request.assigned_to or staff.id
            elif status == RequestStatus.PENDING.value:
                request.resolved_at = None
            _record(
                session,
                entity_type="pending_request",
                entity_id=request.id,
                action=f"request.{status}",
                staff=staff,
                conversation_id=request.conversation_id,
                details={"from": previous, "to": status, "note": note},
            )
            txn.commit()
    except WriteTransactionError as exc:
        raise HandoffError("busy", "The dashboard is busy. Try again.") from exc
    return request


def approve_request(
    session: Session, *, staff: StaffUser, request_id: int, note: str | None = None
) -> PendingRequest:
    """Explicitly approve an approval-gated request.

    Refuses routine items: asking staff to approve a callback would create the
    impression that ordinary work needs sign-off.
    """
    request = get_request(session, request_id)
    if request is None:
        raise HandoffError("unknown_request", "That request no longer exists.")
    if not approval_required(request_type=request.request_type):
        raise HandoffError(
            "not_approvable",
            "This request is routine and does not need staff approval.",
        )
    if request.approved_at is not None:
        return request
    try:
        with write_transaction(session) as txn:
            now = utcnow()
            request.approved_at = now
            request.approved_by = staff.id
            if request.status == RequestStatus.PENDING.value:
                request.status = RequestStatus.IN_PROGRESS.value
                request.assigned_to = staff.id
            _record(
                session,
                entity_type="pending_request",
                entity_id=request.id,
                action="request.approved",
                staff=staff,
                conversation_id=request.conversation_id,
                details={"request_type": request.request_type, "note": note},
            )
            txn.commit()
    except WriteTransactionError as exc:
        raise HandoffError("busy", "The dashboard is busy. Try again.") from exc
    return request


def _record(
    session: Session,
    *,
    entity_type: str,
    entity_id: int,
    action: str,
    staff: StaffUser,
    conversation_id: int | None,
    details: dict[str, Any],
) -> None:
    """Audit a staff triage action.

    Free-text notes are staff-authored and may mention a patient, so only the
    *presence* of a note is recorded, not its contents. Ids, statuses and
    transitions are enough to reconstruct what happened.
    """
    safe_details = {
        key: value for key, value in details.items() if key != "note"
    }
    safe_details["note_provided"] = bool(details.get("note"))
    audit.record(
        session,
        action=action,
        actor_type="staff",
        actor_id=staff.id,
        entity_type=entity_type,
        entity_id=entity_id,
        conversation_id=conversation_id,
        details={"staff_username": staff.username or staff.email, **safe_details},
    )


__all__ = [
    "ALERT_TRANSITIONS",
    "APPROVAL_REQUEST_TYPES",
    "AlertFilters",
    "HandoffError",
    "REQUEST_TRANSITIONS",
    "RequestFilters",
    "alert_activity",
    "alert_dict",
    "approval_required",
    "approve_request",
    "get_alert",
    "get_request",
    "list_alerts",
    "list_requests",
    "request_activity",
    "request_dict",
    "set_alert_severity",
    "set_alert_status",
    "set_request_status",
    "summary_counts",
]
