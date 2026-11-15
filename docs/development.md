# Development

## Requirements

Docker, Python 3.12, Node 22 or newer, and a Gemini API key (free, no card:
<https://aistudio.google.com/apikey>).

## Run it

```bash
cp .env.example .env                 # then set GEMINI_API_KEY

# Postgres, Redis and the registry proxy; and the image every sandbox runs
docker compose up -d postgres redis egress-proxy
docker compose --profile build build sandbox-image

# API and worker
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8000

# Web app, in another terminal
cd frontend && npm install && npm run dev      # http://localhost:5173
```

Or everything in containers: `docker compose --profile build build sandbox-image && docker
compose --profile app up --build`, then open <http://localhost:5173>.

## Checks

```bash
cd backend
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/mypy                         # strict
.venv/bin/python -m pytest             # needs the compose Postgres and Redis

cd frontend && npm run check           # types, tests, demo build
```

The backend tests create their tables in a `codepilot_test` database (create it once: `docker
exec codepilot-postgres-1 psql -U codepilot -c "CREATE DATABASE codepilot_test"`) and use
Redis database 1.

| Tests | What they need | What they cover |
| --- | --- | --- |
| `test_policy`, `test_archive`, `test_core` | nothing | Command policy, upload validation, budget, transition table, model router, context compaction |
| `test_agentd` | nothing | The file agent, run against a temporary directory |
| `test_orchestrator` | Postgres, Redis | Whole runs through the state machine with a scripted model |
| `test_api` | Postgres, Redis | The HTTP API |
| `test_sandbox_docker` | Docker, the sandbox image, the proxy | The real sandbox: what generated code might try, and what happens instead. Skipped when Docker or the image is missing; `-m "not docker"` deselects them |

The orchestrator tests use a scripted model and `LocalSandbox` (`tests/support.py`), which
runs the real file agent against a temporary directory. That class exists only in the tests:
the application has no code path that executes anything outside a container.

## Using a different model

Any OpenAI-compatible endpoint works:

```bash
LLM_PROVIDER=openai_compat
OPENAI_COMPAT_BASE_URL=https://api.groq.com/openai/v1    # or http://localhost:11434/v1 for Ollama
OPENAI_COMPAT_API_KEY=…
LLM_MODELS=llama-3.3-70b-versatile
```

The model must support tool calling.

## Recording a run for the demo

A finished task can be exported and added to the public demo:

```bash
curl -s localhost:8000/api/tasks/<task id>/export > frontend/public/runs/<name>.json
```

Then add an entry for it to `frontend/public/runs/index.json`.

## Adding a tool

1. In `backend/app/tools/registry.py`, write an input model (a `ToolInput` subclass) and an
   `async def handler(ctx, args) -> dict`.
2. Add a `Tool(...)` entry to `TOOLS` with the permission it needs.
3. If it needs a new permission, add it to `Permission` and to the roles that should hold it
   in `ROLE_PERMISSIONS`.

Validation, permission checks, budget, timeout and the event record come from `invoke`.
