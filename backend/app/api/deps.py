"""Patient session dependency.

Only the ``X-Patient-Session`` header is consulted. A conversation ID in the URL
is never sufficient, and staff credentials (a separate table with its own
header) are rejected outright here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PatientSession, StaffSession, StaffUser
from app.services import patient_sessions, staff_auth
from app.services.patient_sessions import PATIENT_TOKEN_HEADER

SESSION_INVALID_DETAIL = (
    "This chat session is no longer valid. Start a new conversation to continue."
)

DbSession = Annotated[Session, Depends(get_db)]


def get_patient_session(
    db: DbSession,
    x_patient_session: Annotated[str | None, Header(alias=PATIENT_TOKEN_HEADER)] = None,
    x_staff_session: Annotated[str | None, Header(alias="X-Staff-Session")] = None,
) -> PatientSession:
    if x_staff_session:
        # Staff credentials are never valid on a patient route.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staff sessions cannot be used to access patient conversations.",
        )
    if not x_patient_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing patient session. Start a conversation first.",
        )
    record = patient_sessions.find_by_token(db, x_patient_session)
    if record is None:
        # 404 not 401: after a demo reset the credential is simply unknown, and
        # we do not want to distinguish "expired" from "never existed".
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=SESSION_INVALID_DETAIL)
    return record


CurrentPatientSession = Annotated[PatientSession, Depends(get_patient_session)]


# --------------------------------------------------------------------------- #
# Staff authentication (Phase 4)
# --------------------------------------------------------------------------- #
#
# Authorization is enforced here, in FastAPI, not in React. The browser-only
# checks exist for a good user experience; deleting them changes nothing about
# what the API will answer.
#
# Every protected route depends on ``require_staff``. It resolves the session
# from the HttpOnly cookie, then requires a matching CSRF header *except* on
# safe methods, so a cross-site page cannot drive a staff action with the
# browser's ambient cookie.

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

STAFF_UNAUTHORIZED_DETAIL = "Sign in to the staff dashboard to continue."
STAFF_CSRF_DETAIL = "This request could not be verified. Reload the dashboard and try again."


def get_staff_session(
    db: DbSession,
    request: Request,
    mb_staff_session: Annotated[str | None, Cookie(alias=staff_auth.STAFF_SESSION_COOKIE)] = None,
    x_staff_csrf: Annotated[str | None, Header(alias=staff_auth.STAFF_CSRF_HEADER)] = None,
) -> StaffSession:
    record = staff_auth.resolve(db, mb_staff_session)
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=STAFF_UNAUTHORIZED_DETAIL)
    if request.method not in SAFE_METHODS and not staff_auth.csrf_matches(record, x_staff_csrf):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=STAFF_CSRF_DETAIL)
    staff_auth.touch(db, record)
    return record


def get_staff_user(db: DbSession, record: Annotated[StaffSession, Depends(get_staff_session)]) -> StaffUser:
    user = db.get(StaffUser, record.staff_user_id)
    if user is None or not user.active:
        # A disabled account loses access immediately, even with a live cookie.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=STAFF_UNAUTHORIZED_DETAIL)
    return user


CurrentStaffSession = Annotated[StaffSession, Depends(get_staff_session)]
CurrentStaffUser = Annotated[StaffUser, Depends(get_staff_user)]
