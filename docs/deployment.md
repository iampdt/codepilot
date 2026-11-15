# Deployment

CodePilot has two deployable shapes, and they are different on purpose.

| | What it is | Where it runs | Cost |
| --- | --- | --- | --- |
| **The system** | API, worker, Postgres, Redis, registry proxy, a sandbox container per task | Any machine with Docker: a laptop, a VM | Free on your own machine or a free-tier VM |
| **The public demo** | The web app alone, replaying recorded runs | Cloudflare Workers static assets | Free |

## Why the demo is a replay

A sandbox is a container the control plane starts through a Docker daemon. Free hosting tiers
(Render, Vercel, Cloudflare, Hugging Face Spaces) run your code *inside* a container and give
it no daemon, so there is nothing to start a sandbox with. The only way to run tasks there
would be to execute generated code as ordinary processes next to the API, beside its
credentials. That is the one thing this project exists to not do.

So the public site replays real runs. A finished task is exported as one JSON document (its
event log and file contents); the demo feeds those events through the same reducer and the
same components as the live app. Nothing on the site executes a visitor's input, there is no
model key to drain, and it loads instantly.

## The public demo (Cloudflare Workers)

```bash
cd frontend
npx wrangler login          # once
npm run deploy:demo         # type check, tests, demo build, wrangler deploy
```

`wrangler.jsonc` publishes `dist/` as static assets with single-page-application fallback.
There is no Worker script. To add a recording, see "Recording a run for the demo" in
[development.md](development.md).

## The system on one machine

```bash
git clone https://github.com/iampdt/codepilot && cd codepilot
cp .env.example .env        # set GEMINI_API_KEY; set API_TOKEN if the machine is reachable by others
docker compose --profile build build sandbox-image
docker compose --profile app up -d --build
```

The web app is on port 5173 and the API on 8000, both bound to `127.0.0.1`. To reach them from
elsewhere, put a TLS-terminating reverse proxy in front (Caddy, or a Cloudflare Tunnel) and
set `API_TOKEN`; do not publish the ports directly.

### On a free VM

Any always-free VM with 1 GB of memory or more works (Oracle Cloud's Always Free ARM instance
has 4 cores and 24 GB; Google Cloud's `e2-micro` is enough for one task at a time with
`SANDBOX_MEMORY_MB=512` and `WORKER_CONCURRENCY=1`). Install Docker, then follow the steps
above. The sandbox image builds for both `amd64` and `arm64`.

On a Linux host there is no VM between containers and the kernel, as there is with Docker
Desktop. For anything beyond personal use, install gVisor and set `SANDBOX_RUNTIME=runsc`
(see [threat-model.md](threat-model.md)).

### Settings worth knowing

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_TOKEN` | empty | When set, every request needs it |
| `RUN_WORKER_IN_API` | `true` | Set `false` and run `python -m app.worker_main` to scale workers separately |
| `WORKER_CONCURRENCY` | `2` | Tasks, and therefore sandboxes, running at once |
| `SANDBOX_RUNTIME` | empty | `runsc` for gVisor |
| `SANDBOX_MEMORY_MB`, `SANDBOX_CPUS`, `SANDBOX_PIDS`, `SANDBOX_WORKSPACE_MB` | 1024, 1.0, 256, 512 | Per-sandbox limits |
| `MAX_LLM_CALLS`, `MAX_TOTAL_TOKENS`, `MAX_TOOL_CALLS`, `MAX_WALL_SECONDS` | 90, 2,000,000, 250, 1800 | Per-task budget |
| `MAX_DEBUG_ITERATIONS`, `MAX_REVIEW_ROUNDS`, `MAX_PLAN_REVISIONS` | 3, 1, 3 | Loop bounds |

### Free-tier model limits

Gemini's free tier limits requests per minute and per day, per model. The router moves down
the chain in `LLM_MODELS` when a model is limited and waits when all of them are; a run slows
down rather than failing. Quota is counted per Google Cloud project, so give CodePilot its own
key in its own project if the same account serves another application.
