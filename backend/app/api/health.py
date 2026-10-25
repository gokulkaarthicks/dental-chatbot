"""Health endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.db import get_db
from app.schemas import HealthResponse
from app.services import generations
from app.services.seed import database_is_seeded
from app.timeutil import clinic_now_iso_with_offset, local_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Application and database health",
)
def health(session: Session = Depends(get_db)) -> HealthResponse:
    """Liveness plus a cheap database probe.

    Returns 200 even when degraded so orchestrators can distinguish "process up
    but DB broken" from "process down".
    """
    database = "ok"
    schema_version: int | None = None
    generation: str | None = None
    seeded = False

    try:
        session.execute(text("SELECT 1"))
        schema_version = generations.get_schema_version(session)
        generation = generations.get_generation(session)
        seeded = database_is_seeded(session)
    except Exception:  # noqa: BLE001 - report, never raise from a health check
        logger.exception("Health check database probe failed")
        database = "unavailable"

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        timestamp=local_now_iso(),
        clinic_now=clinic_now_iso_with_offset(),
        database=database,
        schema_version=schema_version,
        reset_generation=generation,
        seeded=seeded,
    )
