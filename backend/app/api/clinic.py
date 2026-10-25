"""Public clinic information."""

from __future__ import annotations

from fastapi import APIRouter

from app.clinic import CLINIC, PRACTICE_POLICIES, SELF_PAY_LABEL
from app.schemas import ClinicResponse
from app.services import practice

router = APIRouter(tags=["clinic"])


@router.get(
    "/clinic",
    response_model=ClinicResponse,
    summary="Fictional clinic configuration",
)
def get_clinic() -> ClinicResponse:
    """Address, hours, timezone, slot length, and the practice's policy list.

    ``policies`` is the same text the assistant must relay, which makes the
    "answers come from configured data" requirement inspectable in the UI.
    """
    payload = CLINIC.as_public_dict()
    payload["policies"] = [
        {"topic": policy.topic, "question": policy.question, "answer": policy.answer}
        for policy in PRACTICE_POLICIES
    ]
    payload["insurance"] = practice.insurance_overview()
    payload["self_pay_label"] = SELF_PAY_LABEL
    return ClinicResponse(**payload)
