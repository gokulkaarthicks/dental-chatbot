# Dental AI Receptionist — developer entry points.
#
# Primary commands:
#   make seed         insert missing fixtures (never deletes records)
#   make reset-demo   stop writers, recreate the demo DB, reseed, restart
#   make up           docker compose up --build (also auto-inits + seeds an empty DB)
#
# Everything runs in containers so no host Python/Node install is required.

SHELL := /bin/bash
COMPOSE := docker compose
COMPOSE_FILE := docker-compose.yml
BACKEND_SERVICE := backend
FRONTEND_SERVICE := frontend

.DEFAULT_GOAL := help
.PHONY: help up down logs ps seed reset-demo shell-db demo-status backend-status api-docs \
        frontend-logs backend-logs rebuild clean-volumes hooks check lint test

help: ## Show available commands
	@echo "Dental AI Receptionist"
	@echo "----------------------"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- lifecycle -------------------------------------------------------------

up: ## Build and start both services (seeds an empty database)
	$(COMPOSE) -f $(COMPOSE_FILE) up --build

down: ## Stop both services (keeps the data volume)
	$(COMPOSE) -f $(COMPOSE_FILE) down

logs: ## Follow logs from both services (backend + frontend prefixes)
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f

ps: ## Show container status
	$(COMPOSE) -f $(COMPOSE_FILE) ps

# --- data ------------------------------------------------------------------

seed: ## Insert missing fixtures without deleting existing records
	@if $(COMPOSE) -f $(COMPOSE_FILE) ps --status running --services 2>/dev/null | grep -qx '$(BACKEND_SERVICE)'; then \
		echo ">> Seeding via the running backend container"; \
		$(COMPOSE) -f $(COMPOSE_FILE) exec -T $(BACKEND_SERVICE) python -m app.cli seed; \
	else \
		echo ">> Backend not running; seeding in a one-off container"; \
		$(COMPOSE) -f $(COMPOSE_FILE) run --rm --no-deps $(BACKEND_SERVICE) python -m app.cli seed; \
	fi

reset-demo: ## Stop DB writers, recreate the demo database, reseed, restart services
	@echo ">> Stopping database writers"
	$(COMPOSE) -f $(COMPOSE_FILE) stop $(BACKEND_SERVICE) $(FRONTEND_SERVICE)
	@echo ">> Recreating the demo database (new reset generation)"
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm --no-deps $(BACKEND_SERVICE) python -m app.cli reset-demo
	@echo ">> Restarting services"
	$(COMPOSE) -f $(COMPOSE_FILE) up -d $(BACKEND_SERVICE)
	$(COMPOSE) -f $(COMPOSE_FILE) up -d $(FRONTEND_SERVICE)
	@echo ">> Done. Existing conversation IDs, staff sessions, and pending proposals are invalid."

demo-status: ## Print the safe fixture summary as JSON
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(BACKEND_SERVICE) python -m app.cli demo-status

shell-db: ## Open a sqlite3 prompt on the clinic database
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(BACKEND_SERVICE) sqlite3 /app/data/clinic.db

# --- diagnostics -----------------------------------------------------------

backend-status: ## Show backend health (requires curl on the host)
	@curl -fsS http://localhost:8000/api/health || echo "backend not reachable"

api-docs: ## Print API documentation URLs
	@echo "Swagger UI : http://localhost:8000/docs (or http://localhost:8080/docs)"
	@echo "OpenAPI    : http://localhost:8000/openapi.json"

backend-logs: ## Follow backend logs only
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f $(BACKEND_SERVICE)

frontend-logs: ## Follow frontend logs only
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f $(FRONTEND_SERVICE)

rebuild:
	$(COMPOSE) -f $(COMPOSE_FILE) build --no-cache

clean-volumes: ## DESTRUCTIVE: remove containers and the clinic data volume
	$(COMPOSE) -f $(COMPOSE_FILE) down -v

# --- quality gates ---------------------------------------------------------

hooks: ## Install the pre-push git hook (lint auto-fix + pytest) for this clone
	@bash scripts/install-git-hooks.sh

check: ## Run the same gate as git push: lint (auto-fix) + pytest
	@bash scripts/check-before-push.sh

lint: ## Auto-fix and report backend lint issues
	@cd backend && ruff check . --fix && ruff check .

test: ## Run backend tests
	@cd backend && pytest -q

# Quality gates are defined once above (hooks / check / lint / test) and run
# on the host, so they need ruff + pytest installed locally.
