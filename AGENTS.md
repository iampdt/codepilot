# AGENTS.md

Working notes for anyone (or any coding agent) changing CodePilot. Read
`docs/architecture.md` and `docs/threat-model.md` before touching the sandbox.

## What it is

An autonomous coding agent: five agents on a state machine plan, write, run and debug code inside
a per-task Docker sandbox (non-root, read-only, no network; package installs go through a
registry proxy with a three-host allowlist).
FastAPI backend with a worker (`backend/`), React frontend (`frontend/`), sandbox image
(`sandbox/`).

## Layout

| Path | Holds |
| --- | --- |
| `backend/app/orchestrator` | The state machine and the agents' turns |
| `backend/app/agents`, `tools`, `llm` | Agent prompts, typed tools, the model client |
| `backend/app/sandbox` | Container lifecycle and limits |
| `backend/app/api`, `worker.py` | HTTP API and the background worker |
| `backend/tests` | Tests, including real-Docker sandbox and attack tests |
| `frontend/` | The app and the public replay demo (`public/runs/*.json`) |
| `scripts/check.sh` | Every check, also run by `.githooks/pre-push` |

## Commands

```sh
scripts/check.sh            # everything: services, mypy, ruff, pytest (incl. Docker), frontend
scripts/check.sh --fast     # skips the slow Docker tests
docker compose up -d postgres redis egress-proxy   # to run the app locally
cd frontend && npm run deploy:demo                  # publish the replay demo to Cloudflare
```

Setup (Python 3.12 venv in `backend/.venv`, the `codepilot_test` database) is in
`docs/development.md`. `core.hooksPath` should point at `.githooks` so pushes run the checks.

## Rules

- Never weaken the sandbox to make a feature work: no network outside installs, no root, no
  writable host mounts.
  A change to `backend/app/sandbox` needs an attack test.
- The public demo only replays real recorded runs. Never publish a scripted run as a recording.
- Tool calls are typed and validated (`backend/app/tools/registry.py`); commands the model asks for
  run only inside the sandbox.
- Docs are the project's own engineering docs: keep `docs/` current with each change.
