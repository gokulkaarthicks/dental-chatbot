"""Public clinic information, served per tenant.

Two routes, deliberately different in how they choose a clinic:

``GET /api/clinics`` names no tenant and is not allowed to fail because of one.
It exists so a client can discover the slugs to put in ``X-Clinic``, which means it
must keep working for a request whose header is wrong or absent -- so it reads the
directory with an unbound session and never resolves a tenant at all.

``GET /api/clinic`` answers for exactly the tenant the request resolved. Nothing
here imports clinic constants any more: the row, its policies, its carriers and
its grid come from ``ClinicContext``, so what the UI displays is what the assistant
was told, and a second clinic sees its own values rather than the first one's.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import tenancy
from app.api.deps import CurrentClinic, DbSession
from app.schemas import ClinicResponse, ClinicSummary
from app.services import practice

router = APIRouter(tags=["clinic"])


@router.get(
    "/clinics",
    response_model=list[ClinicSummary],
    summary="Clinic directory",
)
def list_clinics(db: DbSession) -> list[ClinicSummary]:
    """Every active tenant, for choosing an ``X-Clinic`` value."""
    return [ClinicSummary(**context.summary()) for context in tenancy.list_clinics(db)]


@router.get(
    "/clinic",
    response_model=ClinicResponse,
    summary="This tenant's clinic configuration",
)
def get_clinic(clinic: CurrentClinic) -> ClinicResponse:
    """Address, hours, timezone, slot length, and the practice's policy list.

    ``policies`` is the same text the assistant must relay, which makes the
    "answers come from configured data" requirement inspectable in the UI -- for
    whichever clinic the request named.
    """
    payload = clinic.as_public_dict()
    payload["policies"] = [
        {"topic": policy.topic, "question": policy.question, "answer": policy.answer}
        for policy in clinic.policies
    ]
    payload["insurance"] = practice.insurance_overview(clinic)
    payload["self_pay_label"] = clinic.self_pay_label
    return ClinicResponse(**payload)
