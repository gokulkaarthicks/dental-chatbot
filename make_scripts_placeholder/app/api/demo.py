"""Demo status endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import DemoStatusResponse
from app.services.demo import build_demo_status

router = APIRouter(tags=["demo"])


@router.get(
    "/demo/status",
    response_model=DemoStatusResponse,
    summary="Safe fixture counts and reset generation",
)
def demo_status(session: Session = Depends(get_db)) -> DemoStatusResponse:
    """Aggregate counts only. No patient identifiers are ever returned here."""
    return DemoStatusResponse(**build_demo_status(session))
