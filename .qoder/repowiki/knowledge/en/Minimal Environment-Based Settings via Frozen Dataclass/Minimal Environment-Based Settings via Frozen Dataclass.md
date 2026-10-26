---
kind: configuration_system
name: Minimal Environment-Based Settings via Frozen Dataclass
category: configuration_system
scope:
    - '**'
source_files:
    - backend/app/config.py
    - .env.example
    - docker-compose.yml
    - backend/app/main.py
---

## Approach

The backend uses a **minimal, code-first settings model** built on Python's `dataclasses` and `functools.lru_cache`. There is no external config library (no pydantic-settings, python-dotenv, or YAML/TOML config files). All runtime configuration is loaded from environment variables with a tiny in-house `.env` parser.

## Key Files

- `backend/app/config.py` -- single source of truth for application settings
- `.env.example` -- template documenting the three supported env vars
- `docker-compose.yml` -- injects `.env` into the backend container only
- `backend/app/main.py` -- consumes settings at startup (`_log_configuration`) and wires CORS/health

## Architecture & Conventions

### Settings shape

`Settings` is a `@dataclass(frozen=True)` with four fields:

| Field | Source | Default |
|---|---|---|
| `deepseek_api_key` | `DEEPSEEK_API_KEY` | empty string |
| `deepseek_model` | `DEEPSEEK_MODEL` | `"deepseek-flash"` |
| `deepseek_base_url` | `DEEPSEEK_BASE_URL` | `"https://api.deepseek.com"` |
| `database_path` | hard-coded | `backend/data/clinic.db` |

There are exactly **three** environment variables the project intentionally supports; everything else has safe defaults in code so "no extra configuration can drift between environments" (docstring in `config.py`).

### Loading order

`load_settings()` resolves values through a local `get(name, default)` helper that implements this precedence:

1. Process `os.environ` (non-empty)
2. Parsed `.env` file at `<repo-root>/.env`
3. Hard-coded default inside `load_settings()`

The `.env` parser (`_read_env_file`) is deliberately minimal: it skips blank lines, comments, lines without `=`, strips surrounding quotes, and handles an optional `export ` prefix. It does not depend on `python-dotenv` so containers behave identically whether or not a `.env` file was mounted.

### Caching

`get_settings()` wraps `load_settings()` in `@lru_cache(maxsize=1)`, so the parsed settings object is constructed once per process and reused everywhere.

### Secret handling

The DeepSeek API key is read only inside this process. The docstring states it must never be sent to the browser, written to logs, or committed to git. At startup, `main._log_configuration()` logs `settings.deepseek_model` and `settings.deepseek_configured` (a boolean property), but never the key value itself.

### Database path

`DEFAULT_DATABASE_PATH = BACKEND_DIR / "data" / "clinic.db"` resolves to `backend/data/clinic.db` on the host (git-ignored) and to `/app/data` inside the Docker volume named `clinic-data` declared in `docker-compose.yml`. The `database_url` property produces `sqlite+pysqlite:///...` for SQLAlchemy.

### Containerization boundary

`docker-compose.yml` passes `.env` exclusively to the `backend` service via `env_file: - .env`; the frontend container receives no secrets. The compose file's top-level comment documents this as "the only host-side configuration".

## Conventions Observed

- New configuration requires adding one field to the frozen `Settings` dataclass and one entry in `load_settings()` -- there is no schema validation layer beyond the dataclass itself.
- Secrets are modeled as plain strings on `Settings`; callers access them directly (e.g. `deepseek_api_key`) rather than through a secret accessor.
- Boolean capability flags (e.g. `deepseek_configured`) are exposed as `@property` methods instead of raw env checks scattered across modules.
- The `.env` file lives at the repository root, not under `backend/`, because `BACKEND_DIR.parent` is used to locate it.
- No feature-flag framework is used; conditional behavior is driven by presence/non-presence of env values checked via `Settings` properties.
- Frontend configuration is separate: the React/Vite app reads runtime values from the FastAPI `/api/clinic` endpoint rather than from env vars baked into the build.