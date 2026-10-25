"""Structured logging for the API.

Why JSON lines
--------------
``docker compose logs -f`` shows whatever the process writes. One JSON object per
line stays greppable by hand (``... | grep booking``) and parseable by a log
collector later, without adding a dependency or a logging service.

What every API log line carries
-------------------------------
``request_id``  -- a per-request id, also returned as ``X-Request-ID`` so a
                   screenshot or support ticket can be tied to a log line.
``endpoint``    -- method + route template (``POST /api/scheduling/.../{id}``),
                   not the raw path, so ids do not fragment the log.
``duration_ms`` -- wall time for the request.

Event helpers
-------------
The wrappers below give the operational events from the phase brief a single
shape: availability searches, proposal creation, booking commit, idempotency
hits, conflict rejections, staff alerts, and DeepSeek status. They never take a
whole patient message -- callers pass counts, codes, and ids.

What is never logged
--------------------
API keys (only presence), password hashes, session tokens, full dates of birth,
patient names, phone numbers, or free-text patient messages. ``_scrub`` is the
final safety net for the structured ``extra`` payload; log *messages* are written
by us and are not built from patient input.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import time
import uuid
from typing import Any

REQUEST_ID_HEADER = "X-Request-ID"

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

# Keys whose values must never reach a log line, matched case-insensitively.
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "password",
        "password_hash",
        "token",
        "session_token",
        "csrf",
        "csrf_token",
        "date_of_birth",
        "dob",
        "full_name",
        "patient_name",
        "phone",
        "message",
        "patient_text",
        "content",
        "arguments",
        "prompt",
    }
)

# An ISO date is enough to identify a day; the full DOB is private.
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# Anything that looks like a phone number in a stray string.
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def set_request_id(value: str) -> contextvars.Token:
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()


def _scrub(value: Any) -> Any:
    """Recursively remove sensitive values from a structured payload."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = _scrub(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return _PHONE.sub("[phone]", _ISO_DATE.sub("[date]", value))
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per record. Never emits a traceback verbatim."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or record.getMessage(),
        }

        request_id = getattr(record, "request_id", None) or current_request_id()
        if request_id:
            payload["request_id"] = request_id

        for key in ("endpoint", "status", "duration_ms", "method"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        extra = getattr(record, "fields", None)
        if isinstance(extra, dict) and extra:
            payload["fields"] = _scrub(extra)

        if record.exc_info:
            # Type and message only: a traceback can embed local variables that
            # hold patient data.
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            payload["error"] = exc_type
            payload["error_detail"] = _scrub(str(record.exc_info[1])[:300])

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter once, on the root logger.

    Idempotent so the CLI, the app factory, and a re-import all agree.
    """
    root = logging.getLogger()
    if any(getattr(handler, "_dental_json", False) for handler in root.handlers):
        root.setLevel(level)
        return

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler._dental_json = True  # type: ignore[attr-defined]

    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; route them through ours so access logs
    # share the request id and JSON shape.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        target.handlers.clear()
        target.propagate = True

    # httpx logs every outbound URL at INFO; the volume is noise next to our own
    # provider events.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger,
    event: str,
    message: str | None = None,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit one structured event with scrubbed fields."""
    logger.log(level, message or event, extra={"event": event, "fields": fields})


# --------------------------------------------------------------------------- #
# Domain events (thin wrappers so call sites read the same everywhere)
# --------------------------------------------------------------------------- #


def availability_search(logger: logging.Logger, **fields: Any) -> None:
    log_event(logger, "availability.search", **fields)


def proposal_created(logger: logging.Logger, **fields: Any) -> None:
    log_event(logger, "proposal.created", **fields)


def booking_commit(logger: logging.Logger, **fields: Any) -> None:
    log_event(logger, "booking.commit", **fields)


def idempotency_hit(logger: logging.Logger, **fields: Any) -> None:
    log_event(logger, "idempotency.hit", **fields)


def conflict_rejected(logger: logging.Logger, reason: str, **fields: Any) -> None:
    log_event(logger, "booking.conflict_rejected", reason=reason, level=logging.WARNING, **fields)


def tool_executed(logger: logging.Logger, **fields: Any) -> None:
    log_event(logger, "tool.executed", **fields)


def deepseek_status(logger: logging.Logger, **fields: Any) -> None:
    log_event(logger, "deepseek.request", **fields)


def staff_alert_created(logger: logging.Logger, **fields: Any) -> None:
    log_event(logger, "staff_alert.created", level=logging.WARNING, **fields)


__all__ = [
    "REQUEST_ID_HEADER",
    "JsonFormatter",
    "availability_search",
    "booking_commit",
    "configure_logging",
    "conflict_rejected",
    "current_request_id",
    "deepseek_status",
    "idempotency_hit",
    "log_event",
    "new_request_id",
    "proposal_created",
    "reset_request_id",
    "set_request_id",
    "staff_alert_created",
    "tool_executed",
]
