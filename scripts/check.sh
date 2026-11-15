#!/usr/bin/env bash
# Every check the project has, in one command: format, lint, types, the backend tests
# (including the ones that attack a real sandbox) and the frontend checks.
#
#   scripts/check.sh          run everything
#   scripts/check.sh --fast   skip the Docker sandbox tests
#
# .githooks/pre-push runs this before every push (enable once with
# `git config core.hooksPath .githooks`).
set -euo pipefail

cd "$(dirname "$0")/.."

fast=0
[[ "${1:-}" == "--fast" ]] && fast=1

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

if [[ ! -x backend/.venv/bin/python ]]; then
  echo "backend/.venv is missing. See docs/development.md." >&2
  exit 1
fi

step "Services"
docker compose up -d --wait postgres redis
docker compose exec -T postgres psql -U codepilot -tAc \
  "SELECT 1 FROM pg_database WHERE datname = 'codepilot_test'" | grep -q 1 ||
  docker compose exec -T postgres psql -U codepilot -c "CREATE DATABASE codepilot_test"

pytest_args=(-q)
if ((fast)); then
  pytest_args+=(-m "not docker")
else
  # The sandbox tests need the real image and the real registry proxy.
  docker compose --profile build build -q sandbox-image
  docker compose up -d egress-proxy
fi

step "Backend"
(
  cd backend
  .venv/bin/ruff format --check .
  .venv/bin/ruff check .
  .venv/bin/mypy
  .venv/bin/python -m pytest "${pytest_args[@]}"
)

step "Frontend"
(
  cd frontend
  [[ -d node_modules ]] || npm ci
  npm run check
)

printf '\n\033[1;32mAll checks passed.\033[0m\n'
