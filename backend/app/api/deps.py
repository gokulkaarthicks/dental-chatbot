"""Request dependencies: tenant resolution, then the credential it authorises.

Two rules shape this module.

**The tenant is resolved first and everything downstream inherits it.**
``CurrentClinic`` binds the request's database session to one ``clinic_id``, so a
handler that receives ``db`` plus a session dependency cannot be looking at a
different tenant than the one it authenticated against. Routes do not remember to
filter; the session is already scoped by the time they run.

**A credential outranks the header.** ``X-Clinic`` only chooses a tenant for
requests that carry no pinned credential yet (starting a conversation, reading
public clinic info). Once a patient or staff session exists it *is* the tenant
authority: the token belongs to exactly one clinic, so a request that presents a
tenant-A token while naming tenant B in the header is rejected with 403 rather
than silently served tenant B's data. Doing it the other way round -- letting the
header win -- would let a leaked token be replayed against any clinic.

Lookups of the credential itself are deliberately *not* tenant filtered: the
stored value is a SHA-256 digest of a 256-bit random token, so it identifies one
clinic's session without disclosing anything about another's. Reading it unscoped
is what lets us answer 403 ``clinic_mismatch`` ("you presented a credential from
the wrong clinic") instead of the misleading 404 that the ORM tenant net alone
would produce.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app import tenancy
from app.db import bind_tenant, get_db
from app.models import PatientSession, StaffSession, StaffUser
from app.services import patient_sessions, staff_auth
from app.services.patient_sessions import PATIENT_TOKEN_HEADER

SESSION_INVALID_DETAIL = (
    "This chat session is no longer valid. Start a new conversation to continue."
)

DbSession = Annotated[Session, Depends(get_db)]

# --------------------------------------------------------------------------- #
# Tenant resolution
# --------------------------------------------------------------------------- #

CLINIC_HEADER = "X-Clinic"

UNKNOWN_CLINIC_DETAIL = "That clinic is not available."
INACTIVE_CLINIC_DETAIL = "This clinic's assistant is temporarily unavailable."
CLINIC_MISMATCH_DETAIL = (
    "This sign-in belongs to a different clinic. Sign out and choose the clinic it "
    "was created for."
)
PATIENT_CLINIC_MISMATCH_DETAIL = (
    "This chat was started with a different clinic. Start a new conversation here."
)


def get_clinic_context(
    db: DbSession,
    x_clinic: Annotated[str | None, Header(alias=CLINIC_HEADER)] = None,
) -> tenancy.ClinicContext:
    """Resolve the request's tenant and bind the session to it.

    With no ``X-Clinic`` header the default clinic is used, which is what keeps
    the existing single-clinic client working unchanged. The header accepts a slug
    or a numeric clinic id; anything else is a 404 rather than a fallback, because
    quietly serving the default tenant for a typo'd slug would show a patient the
    wrong clinic's hours and staff.
    """
    try:
        context = tenancy.resolve_clinic_or_default(db, x_clinic)
    except tenancy.UnknownClinic as exc:
        if exc.inactive:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=INACTIVE_CLINIC_DETAIL,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_CLINIC_DETAIL
        ) from exc
    bind_tenant(db, context.clinic_id)
    return context


CurrentClinic = Annotated[tenancy.ClinicContext, Depends(get_clinic_context)]


# --------------------------------------------------------------------------- #
# Patient sessions
# --------------------------------------------------------------------------- #


def get_patient_session(
    db: DbSession,
    clinic: CurrentClinic,
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
    if record.clinic_id != clinic.clinic_id:
        # The token names a clinic; the header does not get to overrule it.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=PATIENT_CLINIC_MISMATCH_DETAIL
        )
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
    clinic: CurrentClinic,
    mb_staff_session: Annotated[str | None, Cookie(alias=staff_auth.STAFF_SESSION_COOKIE)] = None,
    x_staff_csrf: Annotated[str | None, Header(alias=staff_auth.STAFF_CSRF_HEADER)] = None,
) -> StaffSession:
    record = staff_auth.resolve(db, mb_staff_session)
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=STAFF_UNAUTHORIZED_DETAIL)
    if record.clinic_id != clinic.clinic_id:
        # A staff cookie from another clinic is not "unauthorized" (which would
        # send the dashboard into a re-login loop) -- it is the wrong dashboard.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=CLINIC_MISMATCH_DETAIL
        )
    if request.method not in SAFE_METHODS and not staff_auth.csrf_matches(record, x_staff_csrf):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=STAFF_CSRF_DETAIL)
    staff_auth.touch(db, record)
    return record


def get_staff_user(
    db: DbSession,
    clinic: CurrentClinic,
    record: Annotated[StaffSession, Depends(get_staff_session)],
) -> StaffUser:
    user = db.get(StaffUser, record.staff_user_id)
    if user is None or not user.active:
        # A disabled account loses access immediately, even with a live cookie.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=STAFF_UNAUTHORIZED_DETAIL)
    if user.clinic_id != clinic.clinic_id:
        # Belt and braces: the session row and the account it points at must agree
        # on the tenant, so a hand-edited ``staff_sessions`` row cannot bridge two.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=CLINIC_MISMATCH_DETAIL
        )
    return user


CurrentStaffSession = Annotated[StaffSession, Depends(get_staff_session)]
CurrentStaffUser = Annotated[StaffUser, Depends(get_staff_user)]
