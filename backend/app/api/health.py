"""Health endpoint.

Every number here describes *one* tenant. Under tenancy "is the app healthy" is
not a single answer: tenant A can be fully seeded while tenant B is empty, and
"what time is it at the clinic" differs by timezone. The ``X-Clinic`` header
picks which practice is being asked about, falling back to the default clinic.

Liveness is still never refused: if the tenant cannot be resolved (a typo'd
slug, or a database with no clinic rows yet) the probe reports what it knows and
leaves the tenant-specific fields null rather than returning an error an
orchestrator would read as "process down".
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__, tenancy
from app.api.deps import CLINIC_HEADER, INACTIVE_CLINIC_DETAIL
from app.db import get_db
from app.schemas import HealthResponse
from app.services import generations
from app.services.seed import database_is_seeded
from app.timeutil import utc_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Application and database health",
)
def health(
    session: Session = Depends(get_db),
    x_clinic: Annotated[str | None, Header(alias=CLINIC_HEADER)] = None,
) -> HealthResponse:
    """Liveness plus a cheap database probe.

    Returns 200 even when degraded so orchestrators can distinguish "process up
    but DB broken" from "process down".
    """
    database = "ok"
    schema_version: int | None = None
    generation: str | None = None
    seeded = False
    clinic: tenancy.ClinicContext | None = None

    try:
        session.execute(text("SELECT 1"))
        try:
            clinic = tenancy.resolve_clinic_or_default(session, x_clinic)
        except tenancy.UnknownClinic as exc:
            if exc.inactive:
                # An explicitly named inactive clinic is a real answer, not a
                # degraded one: the process is fine, that tenant is closed.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=INACTIVE_CLINIC_DETAIL,
                ) from exc
            logger.warning("Health probe named an unknown clinic: %s", x_clinic)
        if clinic is not None:
            schema_version = generations.get_schema_version(session)
            generation = generations.get_generation(session, clinic_id=clinic.clinic_id)
            seeded = database_is_seeded(session, clinic.clinic_id)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - report, never raise from a health check
        logger.exception("Health check database probe failed")
        database = "unavailable"

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        timestamp=utc_now_iso(),
        clinic_now=clinic.config.now_iso_with_offset() if clinic else None,
        clinic_slug=clinic.slug if clinic else None,
        database=database,
        schema_version=schema_version,
        reset_generation=generation,
        seeded=seeded,
    )
