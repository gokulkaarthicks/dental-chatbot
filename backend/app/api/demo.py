"""Demo status endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentClinic, DbSession
from app.schemas import DemoStatusResponse
from app.services.demo import build_demo_status

router = APIRouter(tags=["demo"])


@router.get(
    "/demo/status",
    response_model=DemoStatusResponse,
    summary="Safe fixture counts and reset generation",
)
def demo_status(session: DbSession, clinic: CurrentClinic) -> DemoStatusResponse:
    """Aggregate counts for one clinic. No patient identifiers are returned.

    The counts are all tenant rows (slots, appointments, conversations), so the
    tenant has to be resolved before they mean anything: an unbound session would
    answer with every clinic's fixtures summed together.
    """
    return DemoStatusResponse(**build_demo_status(session, clinic))
