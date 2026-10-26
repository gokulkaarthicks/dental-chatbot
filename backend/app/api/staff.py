"""Staff dashboard API (Phase 4).

Authorization lives here, in FastAPI
------------------------------------
Every route except login depends on :data:`app.api.deps.CurrentStaffUser`, which
resolves the HttpOnly session cookie *and* requires a matching CSRF header on any
state-changing request. The React app also checks these things, but only to show
the right screen; deleting the frontend changes nothing about what this module
will answer. A request with no cookie is ``401``; a mutating request without the
CSRF header is ``403``.

Cookie design
-------------
* ``mb_staff_session`` -- HttpOnly, SameSite=Lax, Path=/. Never readable by
  JavaScript, so XSS cannot exfiltrate the session the way it could a
  localStorage token.
* ``mb_staff_csrf`` -- same lifetime, **not** HttpOnly, because the dashboard must
  read it to echo in ``X-Staff-CSRF``. It is useless on its own: the server only
  accepts it paired with the HttpOnly cookie it was issued with.

``Secure`` is applied automatically when the request arrived over HTTPS, so the
cookies work on localhost during development without weakening a real
deployment.

Audit and logging
-----------------
Every mutation records actor, target, and operation; the services write those
rows inside the same transaction as the change. Nothing here logs credentials,
tokens, or patient details.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentClinic, CurrentStaffSession, CurrentStaffUser, DbSession
from app.db import require_clinic_id
from app.models import (
    AlertStatus,
    Conversation,
    ConversationFeedback,
    Message,
    Patient,
    RequestStatus,
    StaffUser,
    utcnow,
)
from app.services import audit, staff_auth, staff_handoff_board, staff_scheduling
from app.services.staff_handoff_board import HandoffError
from app.services.staff_scheduling import AppointmentFilters, StaffActionError
from app.tenancy import ClinicContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/staff", tags=["staff"])

# Cookies are scoped to the proxied origin (Vite dev / nginx in containers), so
# they are same-site with the API and ``Lax`` is enough to stop cross-site posts.
COOKIE_PATH = "/"


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class StaffSessionResponse(BaseModel):
    authenticated: bool
    user: dict | None = None
    csrf_token: str | None = None
    expires_at: str | None = None
    clinic_now: str
    demo_notice: str


class SummaryResponse(BaseModel):
    counts: dict
    generated_at: str


class CreateAppointmentRequest(BaseModel):
    patient_id: int
    provider_id: int
    starts_at: str = Field(description="Clinic-local naive ISO timestamp, e.g. 2026-11-03T09:00")
    appointment_type: str = "checkup"
    reason: str | None = Field(default=None, max_length=500)


class RescheduleRequest(BaseModel):
    starts_at: str
    provider_id: int | None = None
    appointment_type: str | None = None


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AlertStatusRequest(BaseModel):
    status: str = Field(description="open | acknowledged | resolved")
    note: str | None = Field(default=None, max_length=500)


class AlertSeverityRequest(BaseModel):
    severity: str = Field(description="low | normal | high | urgent")


class RequestStatusRequest(BaseModel):
    status: str = Field(description="pending | in_progress | resolved")
    note: str | None = Field(default=None, max_length=500)


class ApprovalRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _set_cookies(response: Response, request: Request, issued: staff_auth.IssuedStaffSession) -> None:
    """Attach the session and CSRF cookies.

    ``max_age`` mirrors the server-side expiry exactly; the server is still the
    authority, so a tampered cookie lifetime grants nothing.
    """
    secure = request.url.scheme == "https"
    max_age = int(staff_auth.SESSION_TTL.total_seconds())
    response.set_cookie(
        staff_auth.STAFF_SESSION_COOKIE,
        issued.token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path=COOKIE_PATH,
    )
    response.set_cookie(
        staff_auth.STAFF_CSRF_COOKIE,
        issued.csrf_token,
        max_age=max_age,
        httponly=False,  # the dashboard must read this one to send the header
        samesite="lax",
        secure=secure,
        path=COOKIE_PATH,
    )


def _clear_cookies(response: Response) -> None:
    for name in (staff_auth.STAFF_SESSION_COOKIE, staff_auth.STAFF_CSRF_COOKIE):
        response.delete_cookie(name, path=COOKIE_PATH)


def _parse_local(clinic: ClinicContext, value: str) -> datetime:
    """Parse a clinic-local naive timestamp, rejecting anything ambiguous."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use an ISO date/time such as 2026-11-03T09:00.",
        ) from exc
    if parsed.tzinfo is not None:
        # The schedule is stored as clinic-local naive time; accepting an offset
        # here would silently mean a different instant.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Times are clinic-local; send them without a timezone offset.",
        )
    if parsed < clinic.config.now() - _GRACE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That time is in the past.",
        )
    return parsed


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use a date such as 2026-11-03.",
        ) from exc


# A booking made "now" for a time that just began should not be rejected for
# clock skew between the two calls.
_GRACE = timedelta(minutes=1)


def _action_error(exc: StaffActionError | HandoffError) -> HTTPException:
    code = getattr(exc, "code", "error")
    status_code = (
        status.HTTP_409_CONFLICT
        if code in {"slot_unavailable", "patient_conflict", "not_booked", "invalid_transition"}
        else status.HTTP_400_BAD_REQUEST
    )
    if code == "unknown_appointment" or code in {"unknown_alert", "unknown_request"}:
        status_code = status.HTTP_404_NOT_FOUND
    if code == "busy":
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(status_code=status_code, detail=getattr(exc, "message", str(exc)))


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


@router.post("/session", response_model=StaffSessionResponse)
async def login(payload: LoginRequest, request: Request, response: Response, db: DbSession, clinic: CurrentClinic) -> dict:
    """Sign in and receive the HttpOnly session cookie.

    Responds identically for unknown usernames, wrong passwords, and disabled
    accounts so the endpoint cannot be used to discover staff usernames.
    """

    def work() -> dict[str, Any]:
        user, problem = staff_auth.authenticate(
            db, username=payload.username, password=payload.password
        )
        if user is None:
            # Commit the failure counter even though nothing else changed.
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=problem or staff_auth.INVALID_CREDENTIALS,
            )
        issued = staff_auth.create_session(db, user=user)
        staff_auth.purge_expired(db)
        audit.record(
            db,
            action="staff.session.login",
            actor_type="staff",
            actor_id=user.id,
            entity_type="staff_user",
            entity_id=user.id,
            details={"role": user.role},
        )
        db.commit()
        return {"user": user, "issued": issued}

    result = await run_in_threadpool(work)
    _set_cookies(response, request, result["issued"])
    user: StaffUser = result["user"]
    return {
        "authenticated": True,
        "user": staff_auth.public_user(user),
        "csrf_token": result["issued"].csrf_token,
        "expires_at": result["issued"].expires_at.isoformat(timespec="seconds"),
        "clinic_now": clinic.config.now().isoformat(timespec="minutes"),
        "demo_notice": "Demonstration login. The root account uses a documented demo password.",
    }


@router.get("/session", response_model=StaffSessionResponse)
async def current_session(
    request: Request, db: DbSession,
    clinic: CurrentClinic, record: CurrentStaffSession, user: CurrentStaffUser
) -> dict:
    """Who am I? Used on every dashboard load to decide login vs. dashboard.

    Deliberately *not* cookie-authenticated-only in a way that leaks anything:
    an anonymous caller gets ``authenticated: false`` and no user data.
    """
    csrf = request.cookies.get(staff_auth.STAFF_CSRF_COOKIE)
    return {
        "authenticated": True,
        "user": staff_auth.public_user(user),
        # Echo the existing CSRF token so a refresh does not force a re-login.
        "csrf_token": csrf if csrf and staff_auth.csrf_matches(record, csrf) else None,
        "expires_at": record.expires_at.isoformat(timespec="seconds"),
        "clinic_now": clinic.config.now().isoformat(timespec="minutes"),
        "demo_notice": "Demonstration login. The root account uses a documented demo password.",
    }


@router.delete("/session")
async def logout(response: Response, db: DbSession, record: CurrentStaffSession) -> dict:
    """Revoke the session server-side, then clear the cookies."""

    def work() -> None:
        staff_auth.revoke(db, record)
        audit.record(
            db,
            action="staff.session.logout",
            actor_type="staff",
            actor_id=record.staff_user_id,
            entity_type="staff_session",
            entity_id=record.id,
        )
        db.commit()

    await run_in_threadpool(work)
    _clear_cookies(response)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Dashboard summary
# --------------------------------------------------------------------------- #


@router.get("/summary", response_model=SummaryResponse)
async def summary(db: DbSession,
    clinic: CurrentClinic, user: CurrentStaffUser) -> dict:
    counts = await run_in_threadpool(staff_handoff_board.summary_counts, db, clinic)
    return {"counts": counts, "generated_at": utcnow().isoformat(timespec="seconds")}


@router.get("/feedback")
async def conversation_feedback(
    db: DbSession,
    user: CurrentStaffUser,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Patient ratings, so staff can read the conversations that went wrong.

    The transcript is already stored; this endpoint surfaces the signal that
    says which transcripts are worth reading. Counts are for the window, the
    list is newest first, and no session token or patient identity is included.
    """
    since = utcnow() - timedelta(days=days)
    counts = dict(
        db.execute(
            select(ConversationFeedback.rating, func.count(ConversationFeedback.id))
            .where(
                ConversationFeedback.created_at >= since,
                ConversationFeedback.clinic_id == require_clinic_id(db),
            )
            .group_by(ConversationFeedback.rating)
        ).all()
    )
    rows = db.execute(
        select(ConversationFeedback, Message.content, Conversation.title)
        .join(Message, Message.id == ConversationFeedback.message_id)
        .join(Conversation, Conversation.id == ConversationFeedback.conversation_id)
        .where(ConversationFeedback.created_at >= since)
        .order_by(ConversationFeedback.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "counts": {
            "up": int(counts.get("up", 0)),
            "down": int(counts.get("down", 0)),
            "total": int(sum(counts.values())),
        },
        "window_days": days,
        "feedback": [
            {
                "id": item.id,
                "rating": item.rating,
                "reason": item.reason,
                "created_at": item.created_at.isoformat(),
                "conversation_title": title,
                "excerpt": (content or "").strip()[:320],
            }
            for item, content, title in rows
        ],
    }


# --------------------------------------------------------------------------- #
# Appointments
# --------------------------------------------------------------------------- #


@router.get("/appointments")
async def list_appointments(
    db: DbSession,
    clinic: CurrentClinic,
    user: CurrentStaffUser,
    day: str | None = Query(default=None, description="YYYY-MM-DD"),
    day_from: str | None = Query(default=None),
    day_to: str | None = Query(default=None),
    provider_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=120),
    upcoming_only: bool = Query(default=False),
    limit: int = Query(default=staff_scheduling.DEFAULT_LIST_LIMIT, ge=1, le=200),
) -> dict:
    filters = AppointmentFilters(
        day=_parse_day(day),
        day_from=_parse_day(day_from),
        day_to=_parse_day(day_to),
        provider_id=provider_id,
        status=status_filter,
        search=search,
        upcoming_only=upcoming_only,
        limit=limit,
    )
    rows = await run_in_threadpool(staff_scheduling.list_appointments, db, clinic, filters)
    return {
        "appointments": [staff_scheduling.appointment_dict(clinic, row) for row in rows],
        "providers": staff_scheduling.provider_options(db),
        "appointment_types": staff_scheduling.appointment_type_options(db),
        "count": len(rows),
    }


@router.get("/appointments/{appointment_id}")
async def appointment_detail(appointment_id: int, db: DbSession,
    clinic: CurrentClinic, user: CurrentStaffUser) -> dict:
    row = await run_in_threadpool(staff_scheduling.get_appointment, db, appointment_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found.")
    history = await run_in_threadpool(staff_scheduling.appointment_history, db, appointment_id)
    return {"appointment": staff_scheduling.appointment_dict(clinic, row), "history": history}


@router.get("/slots")
async def available_slots(
    db: DbSession,
    clinic: CurrentClinic,
    user: CurrentStaffUser,
    day: str = Query(description="YYYY-MM-DD"),
    provider_id: int | None = Query(default=None),
    appointment_type: str = Query(default="checkup"),
) -> dict:
    """Free start times for the booking form. Read-only; holds nothing."""
    target = _parse_day(day)
    if target is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A day is required.")
    try:
        slots = await run_in_threadpool(
            staff_scheduling.available_slots,
            db,
            clinic,
            provider_id=provider_id,
            appointment_type_code=appointment_type,
            day=target,
        )
    except StaffActionError as exc:
        raise _action_error(exc) from exc
    return {"day": target.isoformat(), "slots": slots, "count": len(slots)}


@router.post("/appointments", status_code=status.HTTP_201_CREATED)
async def create_appointment(
    payload: CreateAppointmentRequest, db: DbSession,
    clinic: CurrentClinic, user: CurrentStaffUser
) -> dict:
    starts_at = _parse_local(clinic, payload.starts_at)
    try:
        row = await run_in_threadpool(
            staff_scheduling.create_appointment,
            db,
            clinic,
            staff=user,
            patient_id=payload.patient_id,
            provider_id=payload.provider_id,
            starts_at=starts_at,
            appointment_type_code=payload.appointment_type,
            reason=payload.reason,
        )
    except StaffActionError as exc:
        raise _action_error(exc) from exc
    return {"appointment": staff_scheduling.appointment_dict(clinic, row)}


@router.post("/appointments/{appointment_id}/reschedule")
async def reschedule_appointment(
    appointment_id: int, payload: RescheduleRequest, db: DbSession,
    clinic: CurrentClinic, user: CurrentStaffUser
) -> dict:
    starts_at = _parse_local(clinic, payload.starts_at)
    try:
        row = await run_in_threadpool(
            staff_scheduling.reschedule_appointment,
            db,
            clinic,
            staff=user,
            appointment_id=appointment_id,
            starts_at=starts_at,
            provider_id=payload.provider_id,
            appointment_type_code=payload.appointment_type,
        )
    except StaffActionError as exc:
        raise _action_error(exc) from exc
    return {"appointment": staff_scheduling.appointment_dict(clinic, row)}


@router.post("/appointments/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: int, payload: CancelRequest, db: DbSession,
    clinic: CurrentClinic, user: CurrentStaffUser
) -> dict:
    try:
        row = await run_in_threadpool(
            staff_scheduling.cancel_appointment,
            db,
            staff=user,
            appointment_id=appointment_id,
            reason=payload.reason,
        )
    except StaffActionError as exc:
        raise _action_error(exc) from exc
    return {"appointment": staff_scheduling.appointment_dict(clinic, row)}


@router.post("/appointments/{appointment_id}/complete")
async def complete_appointment(appointment_id: int, db: DbSession,
    clinic: CurrentClinic, user: CurrentStaffUser) -> dict:
    try:
        row = await run_in_threadpool(
            staff_scheduling.complete_appointment, db, staff=user, appointment_id=appointment_id
        )
    except StaffActionError as exc:
        raise _action_error(exc) from exc
    return {"appointment": staff_scheduling.appointment_dict(clinic, row)}


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


@router.get("/alerts")
async def list_alerts(
    db: DbSession,
    user: CurrentStaffUser,
    status_filter: str | None = Query(default=None, alias="status"),
    kind: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=staff_handoff_board.DEFAULT_LIST_LIMIT, ge=1, le=200),
) -> dict:
    filters = staff_handoff_board.AlertFilters(
        status=status_filter, kind=kind, severity=severity, search=search, limit=limit
    )
    rows = await run_in_threadpool(staff_handoff_board.list_alerts, db, filters)
    return {"alerts": [staff_handoff_board.alert_dict(db, row) for row in rows], "count": len(rows)}


@router.get("/alerts/{alert_id}")
async def alert_detail(alert_id: int, db: DbSession, user: CurrentStaffUser) -> dict:
    row = await run_in_threadpool(staff_handoff_board.get_alert, db, alert_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found.")
    activity = await run_in_threadpool(staff_handoff_board.alert_activity, db, alert_id)
    return {"alert": staff_handoff_board.alert_dict(db, row), "activity": activity}


@router.post("/alerts/{alert_id}/status")
async def set_alert_status(
    alert_id: int, payload: AlertStatusRequest, db: DbSession, user: CurrentStaffUser
) -> dict:
    if payload.status not in {item.value for item in AlertStatus}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown alert status."
        )
    try:
        row = await run_in_threadpool(
            staff_handoff_board.set_alert_status,
            db,
            staff=user,
            alert_id=alert_id,
            status=payload.status,
            note=payload.note,
        )
    except HandoffError as exc:
        raise _action_error(exc) from exc
    return {"alert": staff_handoff_board.alert_dict(db, row)}


@router.post("/alerts/{alert_id}/severity")
async def set_alert_severity(
    alert_id: int, payload: AlertSeverityRequest, db: DbSession, user: CurrentStaffUser
) -> dict:
    try:
        row = await run_in_threadpool(
            staff_handoff_board.set_alert_severity,
            db,
            staff=user,
            alert_id=alert_id,
            severity=payload.severity,
        )
    except HandoffError as exc:
        raise _action_error(exc) from exc
    return {"alert": staff_handoff_board.alert_dict(db, row)}


# --------------------------------------------------------------------------- #
# Pending requests
# --------------------------------------------------------------------------- #


@router.get("/requests")
async def list_requests(
    db: DbSession,
    user: CurrentStaffUser,
    status_filter: str | None = Query(default=None, alias="status"),
    request_type: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=staff_handoff_board.DEFAULT_LIST_LIMIT, ge=1, le=200),
) -> dict:
    filters = staff_handoff_board.RequestFilters(
        status=status_filter, request_type=request_type, search=search, limit=limit
    )
    rows = await run_in_threadpool(staff_handoff_board.list_requests, db, filters)
    return {
        "requests": [staff_handoff_board.request_dict(db, row) for row in rows],
        "count": len(rows),
    }


@router.get("/requests/{request_id}")
async def request_detail(request_id: int, db: DbSession, user: CurrentStaffUser) -> dict:
    row = await run_in_threadpool(staff_handoff_board.get_request, db, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    activity = await run_in_threadpool(staff_handoff_board.request_activity, db, request_id)
    return {"request": staff_handoff_board.request_dict(db, row), "activity": activity}


@router.post("/requests/{request_id}/status")
async def set_request_status(
    request_id: int, payload: RequestStatusRequest, db: DbSession, user: CurrentStaffUser
) -> dict:
    if payload.status not in {item.value for item in RequestStatus}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown request status."
        )
    try:
        row = await run_in_threadpool(
            staff_handoff_board.set_request_status,
            db,
            staff=user,
            request_id=request_id,
            status=payload.status,
            note=payload.note,
        )
    except HandoffError as exc:
        raise _action_error(exc) from exc
    return {"request": staff_handoff_board.request_dict(db, row)}


@router.post("/requests/{request_id}/approve")
async def approve_request(
    request_id: int, payload: ApprovalRequest, db: DbSession, user: CurrentStaffUser
) -> dict:
    try:
        row = await run_in_threadpool(
            staff_handoff_board.approve_request,
            db,
            staff=user,
            request_id=request_id,
            note=payload.note,
        )
    except HandoffError as exc:
        raise _action_error(exc) from exc
    return {"request": staff_handoff_board.request_dict(db, row)}


# --------------------------------------------------------------------------- #
# Patients
# --------------------------------------------------------------------------- #


@router.get("/patients/search")
async def search_patients(
    db: DbSession,
    user: CurrentStaffUser,
    q: str = Query(min_length=1, max_length=120),
) -> dict:
    """Patient lookup for the booking form and the patient drawer.

    Restricted to authenticated staff. Matches name or phone, which is how a
    front desk actually finds someone on a call.
    """
    needle = f"%{q.strip().lower()}%"
    rows = list(
        db.scalars(
            select(Patient)
            .where(
                (Patient.full_name.ilike(needle))
                | (Patient.phone.ilike(needle.replace(" ", "")))
            )
            .order_by(Patient.full_name)
            .limit(20)
        )
    )
    return {
        "patients": [
            {
                "id": row.id,
                "full_name": row.full_name,
                "phone": row.phone,
                "date_of_birth": row.date_of_birth.isoformat() if row.date_of_birth else None,
                "insurance_provider": row.insurance_provider,
            }
            for row in rows
        ]
    }


@router.get("/patients/{patient_id}")
async def patient_detail(patient_id: int, db: DbSession,
    clinic: CurrentClinic, user: CurrentStaffUser) -> dict:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found.")
    appointments = await run_in_threadpool(staff_scheduling.list_appointments_by_patient, db, clinic, patient_id)
    return {
        "patient": {
            "id": patient.id,
            "full_name": patient.full_name,
            "phone": patient.phone,
            "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
            "insurance_provider": patient.insurance_provider,
            "insurance_member_id": patient.insurance_member_id,
            "notes": patient.notes,
        },
        "appointments": [staff_scheduling.appointment_dict(clinic, row) for row in appointments],
    }
