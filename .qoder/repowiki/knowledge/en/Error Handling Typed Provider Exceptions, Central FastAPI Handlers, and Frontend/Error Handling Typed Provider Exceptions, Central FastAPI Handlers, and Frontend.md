---
kind: error_handling
name: 'Error Handling: Typed Provider Exceptions, Central FastAPI Handlers, and Frontend ApiError'
category: error_handling
scope:
    - '**'
source_files:
    - backend/app/main.py
    - backend/app/ai/deepseek.py
    - backend/app/api/deps.py
    - frontend/src/api/client.ts
---

## Overview

The repository uses a layered error-handling strategy:

- **Backend (FastAPI/Python)**: domain-specific exception classes for the DeepSeek provider, `HTTPException` raised from dependency functions, and centralized Starlette/FastAPI `exception_handler`s in `app/main.py` that translate internal exceptions into stable JSON responses with a machine-readable `error` code plus a human-friendly `detail` string.
- **Frontend (React/TypeScript)**: a single `ApiError` class thrown by the shared `request()` helper in `frontend/src/api/client.ts`, which reads the backend's `detail` field and exposes the HTTP status.

There is no global `try/except` per route; errors propagate up to the central handlers or are caught at the hook/component layer on the frontend.

## Backend -- Exception hierarchy

All provider failures live in `backend/app/ai/deepseek.py` under one base class:

```
class DeepSeekError(Exception):
    user_message = "I'm having trouble reaching the scheduling system right now."

class DeepSeekNotConfigured(DeepSeekError)
class DeepSeekTimeout(DeepSeekError)
class DeepSeekUnavailable(DeepSeekError)
class DeepSeekRateLimited(DeepSeekError)
class DeepSeekBadResponse(DeepSeekError)
```

Each subclass carries a `user_message` intended for the patient-facing reply. The `DeepSeekClient.chat` method wraps retries around transient subclasses (`DeepSeekTimeout`, `DeepSeekUnavailable`, `DeepSeekRateLimited`) with exponential backoff (`RETRY_BASE_DELAY_SECONDS * (2**attempt) + random.uniform(0, 0.25)`), while non-transient `DeepSeekError` subclasses are re-raised immediately.

Network-level `httpx.TimeoutException` / `httpx.HTTPError` are converted into `DeepSeekTimeout` / `DeepSeekUnavailable` via `raise ... from exc`, preserving the original traceback.

## Backend -- Authentication & authorization errors

`backend/app/api/deps.py` raises `fastapi.HTTPException` directly from dependency callables:

- Missing patient session → `401_UNAUTHORIZED` with `SESSION_INVALID_DETAIL`.
- Staff credentials on a patient route → `403_FORBIDDEN`.
- Unknown token → `404_NOT_FOUND` (intentionally not `401`; comment explains this avoids distinguishing expired vs. never existed after a demo reset).
- Missing staff cookie → `401_UNAUTHORIZED`.
- CSRF mismatch on unsafe methods → `403_FORBIDDEN`.
- Inactive/disabled staff user → `401_UNAUTHORIZED`.

These constants (`STAFF_UNAUTHORIZED_DETAIL`, `STAFF_CSRF_DETAIL`, `SESSION_INVALID_DETAIL`) are the single source of truth for those messages.

## Backend -- Central exception handlers

`app/main.py` installs all handlers inside `_install_observability`:

| Exception | Status | `error` code | `detail` |
|---|---:|---|---|
| `StarletteHTTPException` | passthrough | mapped by `_error_code_for_status` | `exc.detail` |
| `OperationalError` (SQLAlchemy) | 503 | `database_unavailable` | "The schedule is busy right now..." |
| `IntegrityError` | 409 | `conflict` | "That change conflicted with an existing record..." |
| `SQLAlchemyError` | 500 | `database_error` | "Something went wrong saving that..." |
| bare `Exception` | 500 | `internal_error` | "Something went wrong on our side..." |

The handler map `_error_code_for_status` covers 400/401/403/404/409/422/429/503; anything else falls through to `http_error`. Every response includes `request_id` from the request-scoped observability context.

Database error bodies deliberately omit SQL text; the comment states it so callers can refetch rather than assume success. The `OperationalError` handler also attaches `Retry-After: 2`.

## Backend -- Request middleware

A raw ASGI `RequestContextMiddleware` (not `BaseHTTPMiddleware`) wraps every request to:

- Inject a `X-Request-Id` header (read from incoming or generated).
- Log `http.request` and `http.request.failed` events via `log_event` with endpoint label, duration, and status.
- Re-raise exceptions so Starlette's handlers still run.

CORS is configured with `allow_credentials=False` and only `X-Patient-Session`, `X-Staff-CSRF`, and `REQUEST_ID_HEADER` allowed/exposed.

## Frontend -- ApiError

`frontend/src/api/client.ts` defines:

```ts
class ApiError extends Error {
  readonly status: number;
}
```

The shared `request<T>()` function:

- Catches network failures and throws `ApiError("Cannot reach the clinic service. Is the backend running?", 0)`.
- On `!response.ok`, tries to parse `{ detail }` from the JSON body and throws `ApiError(detail, response.status)`.
- Returns `undefined` for 204, otherwise parses JSON.

Callers (e.g. `hooks/useApi.ts`) catch `ApiError` and surface `err.message` as UI state; the `status` field is available for conditional handling.

## Conventions observed

- **Provider errors are typed**: every DeepSeek failure goes through a subclass of `DeepSeekError`, allowing callers to distinguish retryable vs. non-retryable conditions without inspecting strings.
- **User-facing messages are separate from log messages**: each `DeepSeekError` subclass has a `user_message` for the patient, while logs contain structured fields (`status_code`, `latency_ms`, `error_type`).
- **No secrets in logs**: comments explicitly forbid logging API keys or prompt contents; DeepSeek error-body parsing extracts only coarse `type`/`code` fields.
- **Authentication errors are raised from dependencies**, not from route handlers, keeping routes focused on business logic.
- **Centralized error-to-JSON mapping**: all HTTP responses share the shape `{ detail, error, request_id }`, produced by the handlers in `main.py`.
- **Transient database errors become 503 with `Retry-After`**; integrity violations become 409 `conflict` so clients can refresh instead of retrying blindly.
- **Frontend normalizes all transport failures into `ApiError`**, giving callers a uniform type regardless of whether the failure was a network error or a server-side HTTP error.