#!/usr/bin/env bash
# Shared pre-push gate: lint (auto-fix) + tests.
#
# Used by both the local git hook and ``make check`` so there is exactly one
# definition of "is this safe to push".
#
#   1. ruff --fix   -> fixes anything it safely can, then re-checks.
#   2. ruff check   -> if anything is still unfixable, the push is blocked.
#   3. pytest       -> if any test fails, the push is blocked.
#
# The pre-push hook (scripts/pre-push) calls this, then folds any auto-fix into
# the commit being pushed. Auto-fixable problems therefore never block you;
# only lint errors ruff cannot fix or failing tests do.
#
# Escape hatch for emergencies only:  git push --no-verify

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

# Pick an interpreter that has ruff + pytest installed.
pick_python() {
  local candidate
  for candidate in \
    "$BACKEND_DIR/.venv/bin/python" \
    "$REPO_ROOT/.venv/bin/python" \
    "$(command -v python3 || true)" \
    python; do
    if [ -n "$candidate" ] && "$candidate" -c "import ruff, pytest" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

banner() {
  printf '\n\033[1;36m==> %s\033[0m\n' "$1"
}

fail_block() {
  printf '\n\033[1;31m✖ %s\033[0m\n' "$1"
  exit 1
}

[ -d "$BACKEND_DIR" ] || fail_block "backend/ not found at $BACKEND_DIR"

PY="$(pick_python)" || fail_block \
  "Could not find a Python with ruff + pytest. Install them: python3 -m pip install ruff pytest"

cd "$BACKEND_DIR"

# --- 1/2. lint -------------------------------------------------------------
banner "Lint: ruff --fix"
"$PY" -m ruff check . --fix --quiet || true

banner "Lint: ruff check (must be clean)"
if ! "$PY" -m ruff check .; then
  fail_block "Lint still failing after auto-fix. Fix the errors above (or run: make lint)."
fi

# --- 3. tests --------------------------------------------------------------
banner "Tests: pytest"
if ! "$PY" -m pytest -q; then
  fail_block "Tests failed. Push blocked."
fi

printf '\n\033[1;32m✔ Lint clean and all tests passed.\033[0m\n'
