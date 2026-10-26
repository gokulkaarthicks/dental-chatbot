---
kind: build_system
name: Docker Compose Monorepo Build & Pre-Push Quality Gates
category: build_system
scope:
    - '**'
source_files:
    - Makefile
    - docker-compose.yml
    - docker-compose.dev.yml
    - backend/Dockerfile
    - frontend/Dockerfile
    - scripts/check-before-push.sh
    - scripts/install-git-hooks.sh
---

## System Overview

The project is a two-service monorepo (FastAPI backend + Vite/React frontend) orchestrated entirely through **Docker Compose** with a Makefile as the developer entry point. There is no CI pipeline file in the repository; quality gates are enforced locally via a **git pre-push hook** that runs the same checks as `make check`.

## Key Files and Responsibilities

- `Makefile` -- single source of truth for developer commands: `up`, `dev`, `seed`, `reset-demo`, `rebuild`, `clean-volumes`, `lint`, `test`, `check`, `hooks`.
- `docker-compose.yml` -- production-style stack: builds both services from their per-directory Dockerfiles, exposes ports 8000 (backend) and 8080 (frontend), persists SQLite on named volume `clinic-data`, healthchecks the backend `/api/health`, and makes `frontend` depend on `backend` being healthy.
- `docker-compose.dev.yml` -- development overlay that bind-mounts `./backend/app`, `./backend/tests`, `./backend/server.py`, `./backend/pyproject.toml` read-only into the backend container and replaces its command with `uvicorn --reload --reload-dir /app/app --reload-dir /app/tests`; replaces the frontend with a Debian-based Vite dev server on port 5173 with `VITE_API_PROXY_TARGET=http://backend:8000` and `CHOKIDAR_USEPOLLING=true`.
- `backend/Dockerfile` -- multi-stage not used; single stage `python:3.12-slim`, copies `requirements.txt` then `app ./` and `server.py`, declares `/app/data` as a volume, runs `server.py`.
- `frontend/Dockerfile` -- two-stage build: `debian:bookworm-slim` build stage installs node/npm, runs `npm install` then `npm run build`; runtime stage is `nginx:1.27-alpine` serving `/app/dist` via a custom `nginx.conf`.
- `scripts/check-before-push.sh` -- shared gate script invoked by both `make check` and the git hook; runs `ruff check . --fix --quiet` then `ruff check .` then `pytest -q`.
- `scripts/install-git-hooks.sh` -- copies `scripts/pre-push` into `.git/hooks/pre-push` and makes it executable.
- `scripts/pre-push` -- the actual git hook that calls `check-before-push.sh`.

## Architecture and Conventions

### Two-stack compose model
The base `docker-compose.yml` defines production images; `docker-compose.dev.yml` is layered on top via `docker compose -f docker-compose.yml -f docker-compose.dev.yml`. Compose's overlay semantics replace the backend command and volumes, override the frontend image to a minimal Debian shell running the Vite dev server, and use `!override` to replace the published port from `:8080` to `:5173`.

### Hot-reload contract
`make dev` is documented as replacing `make up`: it bind-mounts the working tree so edits are visible without rebuilds. The Makefile comment explicitly warns that running both stacks side-by-side would serve stale code on old ports, so they are mutually exclusive. Rebuilds are only needed when `backend/requirements.txt` or `frontend/package.json` change.

### Data persistence
SQLite lives at `/app/data/clinic.db` inside a named volume `dental-receptionist-clinic-data`. `docker compose down` preserves it; only `make reset-demo` recreates the database (stops writers, runs `app.cli reset-demo`, restarts services). `make clean-volumes` is marked DESTRUCTIVE.

### Environment injection
The comment in `docker-compose.yml` states the API key is injected into the *backend* container only via `env_file: .env` -- it is never passed to the frontend build or bundle.

### Frontend proxying
In dev mode, the frontend container sets `VITE_API_PROXY_TARGET=http://backend:8000` so the Vite dev server proxies `/api` requests to the backend service name over the compose network.

### Quality gates
There is one definition of "is this safe to push": `scripts/check-before-push.sh`. It is invoked by:
- `make check`
- the installed `pre-push` git hook

The gate sequence is fixed: `ruff --fix` (auto-fix anything safe, exit code ignored), then `ruff check` (must be clean), then `pytest` (must pass). Auto-fixable problems do not block the push; unfixable lint errors or failing tests do. An escape hatch exists: `git push --no-verify`.

The hook installer (`scripts/install-git-hooks.sh`) copies `scripts/pre-push` into `.git/hooks/pre-push` and ensures `check-before-push.sh` is executable.

### Interpreter discovery
`check-before-push.sh` searches for an interpreter with ruff + pytest in this order: `backend/.venv/bin/python`, `repo/.venv/bin/python`, `python3`, `python`. If none is found, it fails with instructions to install ruff and pytest.

## Conventions and Constraints

- All developer workflows go through `make`; the Makefile comment states "Everything runs in containers so no host Python/Node install is required" (though quality gates require local ruff/pytest).
- Backend uses `python:3.12-slim` as the base image.
- Frontend is built with Vite (`npm run build`) and served by nginx 1.27 alpine.
- Backend dependency pinning is via `requirements.txt` (copied before `COPY app ./app` to leverage Docker layer caching); frontend dependencies via `package.json` + `package-lock.json`.
- The backend healthcheck polls `http://127.0.0.1:8000/api/health` every 10s with 6 retries and a 20s start period; frontend depends on `service_healthy`.
- Both services use `restart: unless-stopped`.
- The pre-push gate is the sole enforcement mechanism for lint/test; there is no CI configuration in the repository.
- `make lint` runs `ruff check . --fix && ruff check .` directly in the backend directory (not inside a container), meaning the host must have ruff installed for this target.
- `make test` runs `pytest -q` directly in the backend directory, requiring a local Python environment with pytest.