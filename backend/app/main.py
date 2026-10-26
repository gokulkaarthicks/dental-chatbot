"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import chat, clinic, demo, health, scheduling, staff
from app.bootstrap import bootstrap_database
from app.config import get_settings
from app.observability import (
    REQUEST_ID_HEADER,
    configure_logging,
    current_request_id,
    log_event,
    new_request_id,
    reset_request_id,
    set_request_id,
)

logger = logging.getLogger("app.request")

API_PREFIX = "/api"


def _log_configuration() -> None:
    """Log only whether the model key is present -- never the value."""
    settings = get_settings()
    logger.info(
        "DeepSeek model=%s configured=%s (key value is never logged)",
        settings.deepseek_model,
        settings.deepseek_configured,
    )


def _install_observability(application: FastAPI) -> None:
    """Request id, timing, and structured error responses.

    Implemented as raw ASGI middleware rather than ``BaseHTTPMiddleware`` so it
    also observes exceptions raised inside other middleware and still logs the
    duration. Errors are captured *before* being re-raised to Starlette's
    handlers, which means a database failure is logged once, with a request id,
    but never leaks its SQL text to the client.
    """

    class RequestContextMiddleware:
        def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
            self.app = app

        async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            incoming = ""
            for name, value in scope.get("headers") or []:
                if name == REQUEST_ID_HEADER.lower().encode():
                    incoming = value.decode("latin-1")[:64]
                    break
            request_id = incoming or new_request_id()
            token = set_request_id(request_id)
            started = time.monotonic()
            status_holder = {"status": 500}

            async def send_wrapper(message) -> None:  # type: ignore[no-untyped-def]
                if message["type"] == "http.response.start":
                    status_holder["status"] = message["status"]
                    headers = list(message.get("headers") or [])
                    headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                    message["headers"] = headers
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                log_event(
                    logger,
                    "http.request.failed",
                    endpoint=_endpoint_label(scope),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status=status_holder["status"],
                    level=logging.ERROR,
                )
                raise
            finally:
                duration_ms = int((time.monotonic() - started) * 1000)
                status = status_holder["status"]
                log_event(
                    logger,
                    "http.request",
                    endpoint=_endpoint_label(scope),
                    method=scope.get("method"),
                    status=status,
                    duration_ms=duration_ms,
                    level=logging.WARNING if status >= 400 else logging.INFO,
                )
                reset_request_id(token)

    application.add_middleware(RequestContextMiddleware)

    @application.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Keep FastAPI's shape (`detail`) but add a stable code and the request
        # id, so a client can report the exact failing call.
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.detail,
                "error": _error_code_for_status(exc.status_code),
                "request_id": current_request_id(),
            },
            headers=getattr(exc, "headers", None),
        )

    @application.exception_handler(OperationalError)
    async def _db_busy(_: Request, exc: OperationalError) -> JSONResponse:
        # Locks and lost connections are transient: say so, and do not echo SQL.
        log_event(
            logger,
            "db.operational_error",
            endpoint="database",
            level=logging.ERROR,
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": "The schedule is busy right now. Please try again in a moment.",
                "error": "database_unavailable",
                "request_id": current_request_id(),
            },
            headers={"Retry-After": "2"},
        )

    @application.exception_handler(IntegrityError)
    async def _db_integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        # A constraint fired. That is the guard working, not a crash: report a
        # conflict so the caller can refetch rather than assume success.
        log_event(logger, "db.integrity_error", endpoint="database", level=logging.WARNING)
        return JSONResponse(
            status_code=409,
            content={
                "detail": "That change conflicted with an existing record. Refresh and try again.",
                "error": "conflict",
                "request_id": current_request_id(),
            },
        )

    @application.exception_handler(SQLAlchemyError)
    async def _db_generic(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        log_event(logger, "db.error", endpoint="database", level=logging.ERROR)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Something went wrong saving that. Please try again.",
                "error": "database_error",
                "request_id": current_request_id(),
            },
        )

    @application.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", extra={"event": "http.request.unhandled"})
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Something went wrong on our side. Please try again.",
                "error": "internal_error",
                "request_id": current_request_id(),
            },
        )


def _endpoint_label(scope: dict) -> str:
    """Method + route template, so ids do not fragment the log."""
    route = scope.get("route")
    path = getattr(route, "path", None) or scope.get("path", "")
    return f"{scope.get('method', '')} {path}"


def _error_code_for_status(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "rate_limited",
        503: "unavailable",
    }.get(status_code, "http_error")


def create_app(*, run_bootstrap: bool = True) -> FastAPI:
    configure_logging()

    application = FastAPI(
        title="Dental AI Receptionist API",
        version=__version__,
        description=(
            "Backend for the dental-practice receptionist demo. Every clinic is a "
            "tenant: ``X-Clinic`` selects one (slug or id), and a request that names "
            "none gets the default. Call ``GET /api/clinics`` for the available slugs. "
            "All clinic data is fictional.\n\n"
            "Phase 1 exposes health, the clinic directory, and demo status. "
            "Phase 2 adds the patient chat: opaque patient sessions, a DeepSeek-backed "
            "receptionist with validated tools, and registration/verification."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    _install_observability(application)

    # Dev-only origins: Vite on 5173, the containerised frontend on 8080.
    # The session token travels in a header (never a cookie), so credentials
    # stay disabled and only the explicit headers below are allowed.
    #
    # ``X-Clinic`` is on the list because tenant selection is a header, not a
    # cookie: a browser must be able to name the clinic it is talking to without
    # the server guessing from ambient state.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8080"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "X-Clinic",
            "X-Patient-Session",
            "X-Staff-CSRF",
            REQUEST_ID_HEADER,
        ],
        expose_headers=["X-Reset-Generation", REQUEST_ID_HEADER],
    )

    application.include_router(health.router, prefix=API_PREFIX)
    application.include_router(clinic.router, prefix=API_PREFIX)
    application.include_router(demo.router, prefix=API_PREFIX)
    application.include_router(chat.router, prefix=API_PREFIX)
    application.include_router(scheduling.router, prefix=API_PREFIX)
    application.include_router(staff.router, prefix=API_PREFIX)

    @application.on_event("startup")
    def _startup() -> None:  # pragma: no cover - exercised by containers
        _log_configuration()
        if run_bootstrap:
            bootstrap_database()

    return application


app = create_app()
