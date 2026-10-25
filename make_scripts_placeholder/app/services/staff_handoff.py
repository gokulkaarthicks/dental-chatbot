"""Staff handoff: alerts the front desk, and pending requests for follow-up.

Honesty rule
------------
Creating an alert proves only that *the system recorded* it. Nothing in this
module can verify that a human read it, so every returned status says exactly
that, and the wording the assistant may use is derived from ``status`` rather
than invented in a prompt.

Idempotency
-----------
Each alert/request carries a ``dedupe_key`` scoped to the conversation and
subject. Re-reporting the same emergency reuses the open row and bumps
``report_count``; if the new report adds detail (symptoms worsening), the
message is refreshed so staff see the update instead of a stale summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AlertKind,
    AlertStatus,
    PendingRequest,
    RequestStatus,
    RequestType,
    StaffAlert,
    utcnow,
)
from app.observability import log_event

logger = logging.getLogger(__name__)

# Wording is derived from status so the assistant cannot overclaim.
_STATUS_WORDING = {
    AlertStatus.OPEN.value: "recorded for the front desk",
    AlertStatus.ACKNOWLEDGED.value: "acknowledged by clinic staff",
    AlertStatus.RESOLVED.value: "resolved by clinic staff",
}
_REQUEST_WORDING = {
    RequestStatus.PENDING.value: "queued for the front desk",
    RequestStatus.IN_PROGRESS.value: "being handled by clinic staff",
    RequestStatus.RESOLVED.value: "resolved by clinic staff",
}


@dataclass
class HandoffResult:
    kind: str
    record_id: int
    dedupe_key: str
    status: str
    severity: str
    message: str
    created: bool
    report_count: int
    staff_notified: bool = False
    # Set when the record carries the caller's own name/number for a callback
    # (an unverified caller). Never implies identity was verified.
    caller_contact: dict[str, str | None] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            # Always False: the system cannot verify a human saw this.
            "staff_personally_notified": self.staff_notified,
            "recorded": True,
            "created": self.created,
            "report_count": self.report_count,
            "severity": self.severity,
            "summary": _STATUS_WORDING.get(self.status, self.status),
            "message": self.message,
        }


def dedupe_key(*parts: object) -> str:
    return ":".join("" if part is None else str(part) for part in parts)


CALL_BACK_MARKER = " | Call back: "


def _with_call_back(message: str, name: str | None, phone: str | None) -> str:
    """The summary staff read, with the caller's call-back line appended once."""
    if not (name or phone):
        return message
    base = message.split(CALL_BACK_MARKER)[0]
    return f"{base}{CALL_BACK_MARKER}{name or 'name not given'}, {phone or 'no number'}"


def raise_alert(
    session: Session,
    *,
    conversation_id: int,
    patient_id: int | None,
    kind: AlertKind | str,
    message: str,
    severity: str = "normal",
    subject: str | None = None,
    update_message_if_changed: bool = True,
    caller_name: str | None = None,
    caller_phone: str | None = None,
) -> HandoffResult:
    """Create or update a staff alert. Caller owns the transaction.

    ``caller_name``/``caller_phone`` are for a caller who is not a verified
    patient: they go onto the alert so the front desk can call back, and they
    never set ``patient_id``.
    """
    key = dedupe_key(kind, conversation_id, subject)
    existing = session.scalar(
        select(StaffAlert)
        .where(
            StaffAlert.dedupe_key == key,
            StaffAlert.status.in_([AlertStatus.OPEN.value, AlertStatus.ACKNOWLEDGED.value]),
        )
        .order_by(StaffAlert.id.desc())
    )
    now = utcnow()
    kind_value = kind.value if isinstance(kind, AlertKind) else str(kind)

    if existing is not None:
        # Worsening detail must reach staff, but a repeat of the same text must
        # not inflate the count or spam an update.
        wanted = _with_call_back(message, caller_name, caller_phone)
        changed = wanted.strip() != existing.message.strip()
        if changed and update_message_if_changed:
            existing.message = wanted
            existing.severity = _max_severity(existing.severity, severity)
        existing.report_count = (existing.report_count or 1) + (1 if changed else 0)
        existing.last_reported_at = now
        if patient_id and existing.patient_id is None:
            existing.patient_id = patient_id
        # Contact details fill in, never blank out, an existing caller record.
        if caller_name and not existing.caller_name:
            existing.caller_name = caller_name
        if caller_phone and not existing.caller_phone:
            existing.caller_phone = caller_phone
        session.flush()
        return HandoffResult(
            kind=kind_value,
            record_id=existing.id,
            dedupe_key=key,
            status=existing.status,
            severity=existing.severity,
            message=existing.message,
            created=False,
            report_count=existing.report_count,
            caller_contact=(
                {"name": existing.caller_name, "phone": existing.caller_phone}
                if (existing.caller_name or existing.caller_phone)
                else None
            ),
        )

    alert = StaffAlert(
        conversation_id=conversation_id,
        patient_id=patient_id,
        kind=kind_value,
        # ``message`` may have been computed before the caller was known (a retry
        # after contact_details_required), so the call back is added here, before
        # the insert, where the row staff actually read is built.
        message=_with_call_back(message, caller_name, caller_phone),
        status=AlertStatus.OPEN.value,
        dedupe_key=key,
        report_count=1,
        last_reported_at=now,
        caller_name=caller_name,
        caller_phone=caller_phone,
    )
    session.add(alert)
    session.flush()
    # Kind and severity identify the escalation; the free-text concern is patient
    # information and stays in the database, not the log.
    log_event(
        logger,
        "staff_alert.created",
        alert_id=alert.id,
        kind=kind_value,
        severity=alert.severity,
        conversation_id=conversation_id,
        level=logging.WARNING,
    )
    return HandoffResult(
        kind=kind_value,
        record_id=alert.id,
        dedupe_key=key,
        status=alert.status,
        severity=alert.severity,
        message=alert.message,
        created=True,
        report_count=1,
    )


def raise_request(
    session: Session,
    *,
    conversation_id: int,
    patient_id: int | None,
    request_type: RequestType | str,
    details: str,
    subject: str | None = None,
) -> HandoffResult:
    """Create or update a pending request (callback, billing, clinical question)."""
    key = dedupe_key("request", request_type, conversation_id, subject)
    existing = session.scalar(
        select(PendingRequest)
        .where(
            PendingRequest.dedupe_key == key,
            PendingRequest.status.in_(
                [RequestStatus.PENDING.value, RequestStatus.IN_PROGRESS.value]
            ),
        )
        .order_by(PendingRequest.id.desc())
    )
    type_value = request_type.value if isinstance(request_type, RequestType) else str(request_type)

    if existing is not None:
        existing.details = details
        if patient_id and existing.patient_id is None:
            existing.patient_id = patient_id
        session.flush()
        return HandoffResult(
            kind=type_value,
            record_id=existing.id,
            dedupe_key=key,
            status=existing.status,
            severity="normal",
            message=existing.details,
            created=False,
            report_count=1,
        )

    request = PendingRequest(
        conversation_id=conversation_id,
        patient_id=patient_id,
        request_type=type_value,
        status=RequestStatus.PENDING.value,
        details=details,
        dedupe_key=key,
    )
    session.add(request)
    session.flush()
    return HandoffResult(
        kind=type_value,
        record_id=request.id,
        dedupe_key=key,
        status=request.status,
        severity="normal",
        message=request.details,
        created=True,
        report_count=1,
    )


def request_status(session: Session, *, conversation_id: int) -> list[dict[str, Any]]:
    """Everything this conversation has waiting on staff, newest first."""
    alerts = session.scalars(
        select(StaffAlert)
        .where(StaffAlert.conversation_id == conversation_id)
        .order_by(StaffAlert.id.desc())
    )
    requests = session.scalars(
        select(PendingRequest)
        .where(PendingRequest.conversation_id == conversation_id)
        .order_by(PendingRequest.id.desc())
    )
    entries: list[dict[str, Any]] = []
    for alert in alerts:
        entries.append(
            {
                "type": "alert",
                "kind": alert.kind,
                "severity": alert.severity,
                "status": alert.status,
                "summary": _STATUS_WORDING.get(alert.status, alert.status),
                "message": alert.message,
                "report_count": alert.report_count,
                "caller_name": alert.caller_name,
                "caller_phone": alert.caller_phone,
                "created_at": alert.created_at.isoformat(timespec="seconds")
                if alert.created_at
                else None,
            }
        )
    for request in requests:
        entries.append(
            {
                "type": "request",
                "kind": request.request_type,
                "severity": "normal",
                "status": request.status,
                "summary": _REQUEST_WORDING.get(request.status, request.status),
                "message": request.details,
                "report_count": 1,
                "created_at": request.created_at.isoformat(timespec="seconds")
                if request.created_at
                else None,
            }
        )
    return entries


_SEVERITY_ORDER = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


def _max_severity(left: str, right: str) -> str:
    return left if _SEVERITY_ORDER.get(left, 1) >= _SEVERITY_ORDER.get(right, 1) else right


__all__ = [
    "HandoffResult",
    "dedupe_key",
    "raise_alert",
    "raise_request",
    "request_status",
]
