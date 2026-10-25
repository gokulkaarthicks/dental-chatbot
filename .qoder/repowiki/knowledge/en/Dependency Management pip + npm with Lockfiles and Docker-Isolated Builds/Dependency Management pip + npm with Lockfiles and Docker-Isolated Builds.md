---
kind: dependency_management
name: 'Dependency Management: pip + npm with Lockfiles and Docker-Isolated Builds'
category: dependency_management
scope:
    - '**'
source_files:
    - backend/requirements.txt
    - backend/pyproject.toml
    - backend/Dockerfile
    - frontend/package.json
    - frontend/package-lock.json
    - frontend/Dockerfile
    - docker-compose.yml
    - Makefile
---

## Approach

The monorepo manages dependencies for two independent subprojects — a Python FastAPI backend under `backend/` and a React/Vite frontend under `frontend/` — using the language-native package managers. There is no top-level dependency manifest; each subproject pins its own third-party packages.

### Backend (Python)

- **Manifest**: `backend/requirements.txt` declares runtime dependencies with exact versions via `==`:
  - `fastapi==0.115.6`
  - `uvicorn[standard]==0.34.0`
  - `SQLAlchemy==2.0.36`
  - `pydantic==2.10.4`
  - `httpx==0.34.0` (used to call DeepSeek over an OpenAI-compatible HTTP API, per the inline comment)
- **No lockfile** (`requirements.txt` is the single source of truth); there is no `requirements-dev.txt`, `Pipfile`, or `poetry.lock`.
- **Tooling config** lives in `backend/pyproject.toml`, which only configures Ruff linting and pytest options — it does not declare project dependencies.
- **Container build** (`backend/Dockerfile`) copies `requirements.txt` first and runs `pip install --no-cache-dir -r requirements.txt` before copying application code, so dependency layers are cached across builds.
- **Host toolchain**: The Makefile's `lint` target runs `ruff check . --fix && ruff check .` from the `backend/` directory, and `test` runs `pytest -q`. Quality gates on the host require `ruff` and `pytest` installed locally (documented at the bottom of `Makefile`).

### Frontend (Node.js / TypeScript)

- **Manifest**: `frontend/package.json` declares both runtime and dev dependencies with pinned versions:
  - Runtime: `react@18.3.1`, `react-dom@18.3.1`, `lucide-react@^0.469.0` (the only caret-pinned dep).
  - Dev: `vite@5.4.11`, `typescript@5.6.3`, `@vitejs/plugin-react@4.3.4`, plus `@types/node`, `@types/react`, `@types/react-dom`.
- **Lockfile**: `frontend/package-lock.json` is committed alongside `package.json`; the Dockerfile copies both files and runs `npm install --no-audit --no-fund` during the build stage, ensuring deterministic installs inside containers.
- **Build pipeline**: `npm run build` runs `tsc --noEmit` (typecheck) then `vite build`; the resulting static assets are served by nginx in a second-stage image.

### Containerization & Isolation

- `docker-compose.yml` defines two services (`backend`, `frontend`) that build from their respective `Dockerfile`s. The compose file explicitly states: "The only host-side configuration is the `.env` file" — dependencies are resolved inside containers, not on the host.
- The backend container uses `python:3.12-slim` and sets `PIP_NO_CACHE_DIR=1` and `PIP_DISABLE_PIP_VERSION_CHECK=1`.
- The frontend multi-stage Dockerfile installs Node/npm via `apt-get` in the build stage and serves built artifacts from `nginx:1.27-alpine` at runtime.

### Conventions Observed

- Dependencies are declared as exact versions (`==` in `requirements.txt`, direct version strings in `package.json`) rather than ranges, except for `lucide-react` which uses `^0.469.0`.
- No vendoring strategy (no `vendor/` directory, no Poetry/PDM lockfiles, no private PyPI registry configured).
- No private registries or authentication tokens are present in the repository; all packages come from public PyPI and the npm registry.
- Dependency updates are expected to be manual edits to `requirements.txt` / `package.json` followed by committing the regenerated `package-lock.json`.
- The Makefile centralizes developer entry points (`make up`, `make dev`, `make lint`, `make test`) but does not orchestrate dependency installation itself — it assumes the Docker images handle `pip install` / `npm install`.