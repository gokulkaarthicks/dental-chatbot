#!/usr/bin/env bash
# Installs the repo's git hooks into .git/hooks.
#
# Run once per clone:  bash scripts/install-git-hooks.sh
#
# Hooks installed:
#   pre-push -> lint (auto-fix) + pytest; the push is blocked unless both pass.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$(git rev-parse --git-path hooks)"
SRC_DIR="$REPO_ROOT/scripts"

mkdir -p "$HOOKS_DIR"

install_hook() {
  local name="$1"
  local target="$HOOKS_DIR/$name"
  cp "$SRC_DIR/$name" "$target"
  chmod +x "$target"
  printf '  installed %s\n' "$target"
}

printf 'Installing git hooks from scripts/\n'
install_hook pre-push
chmod +x "$SRC_DIR/check-before-push.sh" || true

printf '\nDone. Every "git push" now runs lint (auto-fix) + pytest.\n'
printf 'Bypass once (emergencies only): git push --no-verify\n'
