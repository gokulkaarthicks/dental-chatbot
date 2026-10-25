---
kind: logging_system
name: Structured JSON Logging with Request Tracing via stdlib logging
category: logging_system
scope:
    - '**'
source_files:
    - backend/app/observability.py
    - backend/app/main.py
    - backend/app/bootstrap.py
    - backend/app/cli.py
    - backend/app/db.py
    - backend/app/migrations.py
    - backend/app/agent/runner.py
    - backend/app/agent/tools.py
    - backend/app/ai/deepseek.py
    - backend/app/api/chat.py
    - backend/app/api/health.py
    - backend/app/api/scheduling.py
---

# Logging System

## Approach

The backend uses Python's built-in `logging` module — no third-party logger framework (no structlog, loguru, or Sentry SDK). A custom `JsonFormatter` in `backend/app/observability.py` serializes every record as one JSON object per line and installs it on the root logger. The design goal, stated in the module docstring, is that `docker compose logs -f` output stays greppable by hand and parseable by a downstream log collector without adding an external dependency.

## Key Files

- `backend/app/observability.py` — single source of truth for logging configuration, the JSON formatter, request-id context, sensitive-field scrubbing, and domain event helpers.
- `backend/app/main.py` — FastAPI factory that calls `configure_logging()`, installs ASGI middleware for per-request tracing, and defines exception handlers that emit structured events.
- `backend/app/bootstrap.py`, `backend/app/cli.py`, `backend/app/db.py`, `backend/app/migrations.py`, `backend/app/agent/*.py`, `backend/app/ai/deepseek.py`, `backend/app/api/*.py` — consumers that create a module-level `logger = logging.getLogger(__name__)` and call either `logger.info/warning/error(...)` directly or the domain-event helpers from `observability`.

## Architecture

### Initialization

`create_app()` in `main.py` calls `configure_logging(level=logging.INFO)`. That function:

1. Installs a single `StreamHandler` with `JsonFormatter` on the root logger (idempotent via a `_dental_json` marker attribute).
2. Clears uvicorn's default handlers and re-adds them so access logs flow through the same JSON pipeline.
3. Sets `httpx` to `WARNING` to suppress noisy outbound URL logs.
4. Sets the root level to `INFO` by default.

### Per-request context

A `contextvars.ContextVar` named `request_id` carries a UUID-derived string through async request handling. The ASGI middleware in `main.py`:

- Reads `X-Request-ID` from the incoming headers (up to 64 chars).
- Generates a new id if absent (`new_request_id()` returns `uuid.uuid4().hex[:16]`).
- Injects the same value back into the response header `X-Request-ID`.
- Resets the context var in a `finally` block.

Every log emitted during the request automatically picks up this id because `JsonFormatter.format()` reads both `record.request_id` and `current_request_id()`.

### Structured fields

Callers attach extra fields via two paths:

1. **Domain-event helpers** in `observability.py`: `availability_search`, `proposal_created`, `booking_commit`, `idempotency_hit`, `conflict_rejected`, `tool_executed`, `deepseek_status`, `staff_alert_created`. Each wraps `log_event(logger, "<event>", ...)` which sets `extra={"event": ..., "fields": kwargs}`.
2. **Direct `logger.log(...)` calls** with `extra={"endpoint": ..., "method": ..., "status": ..., "duration_ms": ...}` — used by the request middleware and exception handlers.

`JsonFormatter` serializes these as top-level keys plus a nested `fields` dict. The documented per-line shape is:

| Field | Meaning |
|---|---|
| `ts` | ISO timestamp with millisecond precision, UTC |
| `level` | Log level name |
| `logger` | Logger name |
| `event` | Event name or message |
| `request_id` | Correlation id (when present) |
| `endpoint` | Method + route template |
| `method` | HTTP method |
| `status` | HTTP status code |
| `duration_ms` | Wall time in ms |
| `fields` | Scrubbed caller-supplied payload |
| `error` / `error_detail` | Exception type and first 300 chars of message (tracebacks are deliberately omitted) |

### Sensitive data scrubbing

`_SENSITIVE_KEYS` is a frozenset of field names whose values must never reach a log line: `api_key`, `authorization`, `password`, `password_hash`, `token`, `session_token`, `csrf`, `csrf_token`, `date_of_birth`, `dob`, `full_name`, `patient_name`, `phone`, `message`, `patient_text`, `content`, `arguments`, `prompt`. Values under those keys are replaced with `[redacted]` recursively.

Additionally, `_scrub` applies regex replacements to string values:
- ISO dates (`\b\d{4}-\d{2}-\d{2}\b`) → `[date]`
- Phone-number-like strings → `[phone]`

The module docstring explicitly enumerates what is never logged: API keys (only presence), password hashes, session tokens, full DOBs, patient names, phone numbers, or free-text patient messages.

### Domain event taxonomy

Events follow a dotted namespace convention:

- `availability.search`
- `proposal.created`
- `booking.commit`
- `idempotency.hit`
- `booking.conflict_rejected` (always WARNING)
- `tool.executed`
- `deepseek.request`
- `staff_alert.created` (always WARNING)
- `http.request` / `http.request.failed` / `http.request.unhandled`
- `db.operational_error` / `db.integrity_error` / `db.error`

Level policy observed in the code:
- Normal operational events use `INFO`.
- HTTP responses with status ≥ 400 use `WARNING`.
- `conflict_rejected` and `staff_alert_created` are hard-coded to `WARNING`.
- Exceptions and unhandled errors use `ERROR`.

### Consumer pattern

Modules import `logging` and create `logger = logging.getLogger(__name__)`. For cross-cutting domain events they instead call the helpers from `app.observability`, e.g. `from app.observability import booking_commit; booking_commit(logger, ...)`.

## Conventions and Constraints

- Every log line is a single JSON object produced by `JsonFormatter`; there is no plain-text fallback handler.
- Logs are written to stdout via `StreamHandler`; no file sink is configured in application code.
- The root logger is configured once in `create_app()` and is idempotent — repeated imports do not duplicate handlers.
- Uvicorn's own loggers (`uvicorn`, `uvicorn.error`, `uvicorn.access`) are wired through the same JSON formatter so access logs share the request id.
- `httpx` is silenced at INFO to avoid leaking outbound URLs at normal verbosity.
- Tracebacks are intentionally never emitted verbatim; only `exc_type` and a truncated `error_detail` are serialized, because tracebacks can embed local variables containing patient data.
- The `X-Request-ID` header is propagated end-to-end: read from the request, stored in a `ContextVar`, attached to every log line, and returned in the response header.
- All structured payloads go through `_scrub`, which redacts keys in `_SENSITIVE_KEYS` and masks dates and phone numbers inside string values.
- Endpoint labels in logs use the route template (e.g. `POST /api/scheduling/{id}`) rather than the raw path, to prevent log cardinality explosion from dynamic ids.