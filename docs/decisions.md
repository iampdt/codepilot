# Decisions

Why CodePilot is built the way it is. Each entry records the choice, the reason, and what it
replaced. All dated 2026-11-15 unless noted.

## Orchestration

| # | Decision | Why | Rejected alternative |
| --- | --- | --- | --- |
| O1 | **An explicit state machine** drives every task: a table of allowed transitions, one handler per state | The lifecycle is the product. A table can be read, tested and drawn; "nothing runs before approval" and "every loop ends" become properties of the table | A supervisor agent that decides what happens next |
| O2 | **Agents never talk to each other.** The orchestrator builds each brief from stored data; an agent ends by submitting a typed contract | Free-form agent chat has no bound and no schema, and one agent's mistake becomes the next one's premise | A shared conversation, or agents calling agents |
| O3 | **One agent loop, five configurations** (role, prompt, step limit, contract) | The loop is where bounds, budget and cancellation are enforced: one place to get right | Five hand-written agents |
| O4 | **Structured output through a submit tool**, validated by Pydantic; invalid results go back to the agent | Works on every provider that has tool calling; the error message tells the model what to fix | Provider-specific JSON modes |
| O5 | **The model must call a tool every turn** (tool mode `ANY`/`required`) | Prose is never a valid answer, so there is no "the model just chatted" failure to handle | Parsing free text for intent |
| O6 | **The orchestrator verifies; agents only report.** `verifying` runs the tests and reads the exit code | A model that says "all tests pass" is not evidence | Trusting the executor's report |
| O7 | **The verify command must be a known test runner** | Otherwise the cheapest way to pass is to nominate `true` | Running whatever command the agent names |
| O8 | **State changes are compare-and-set** | A cancel from the API and a transition from the worker can race; the loser must notice | Read, then write |
| O9 | **Task state lives in Postgres; a run can stop and be picked up** at the approval gate | Waiting on a person holds no worker, model call or container | A worker blocked on approval |
| O10 | **A task found mid-run after a crash is failed, not resumed** | Its sandbox and the agent's conversation are gone; resuming would mean guessing | Checkpointing agent conversations |
| O11 | **Roles are permissions in code**, checked per call | A prompt that says "do not write files" is a request; a permission table is a rule | Giving each agent only a tool list in its prompt |
| O12 | **The planner reads the archive, not a sandbox** | No container exists and nothing can execute until a person approves a plan | Creating the sandbox first |
| O13 | **One review round at most**, and only for `blocking` findings | Reviewers always find something; an uncapped loop never ends and each round costs a full coding pass | Loop until the reviewer approves |
| O14 | **Hard ceilings per task** on model calls, tokens, tool calls and wall time, charged before each call | A stuck agent must cost a known maximum | Per-agent limits only |
| O15 | **Old tool arguments and results are stubbed out** of the context | Resending every file on every turn makes cost quadratic; free tiers count tokens per minute | Sending the full history |
| O16 | **Verification starts from a clean install** of the project's manifests; the executor may install only what is declared | In an early run the tests failed on a missing package, the executor installed it by name, and the tests then passed although `requirements.txt` never listed it: the exported project was broken and nothing noticed | Verifying in whatever environment the agents left behind |
| O17 | **The result is stored before the task is marked finished** | A client that sees `completed` must find the archive and diff already there; the first version set the state first and exported after | Export in a `finally` after the terminal state |

## Sandbox

| # | Decision | Why | Rejected alternative |
| --- | --- | --- | --- |
| S1 | **One Docker container per task**, destroyed at the end | Clean state per task; nothing to leak between tasks | A pool of reused containers |
| S2 | **The control plane drives Docker through the CLI** as an async subprocess | Streaming output, stdin and kill are native to asyncio subprocesses; the SDK's exec sockets are awkward to stream and cancel | The Docker SDK for Python |
| S3 | **Named volumes, not bind mounts** | No host path is ever visible in a sandbox, and the control plane can itself run in a container | Mounting a host directory |
| S4 | **File tools run inside the sandbox** (`agentd.py`), not on the control plane | Symlinks are resolved where they live; the control plane never opens an untrusted file | Reading the volume from the host |
| S5 | **No network; attach to a registry-only proxy for installs, then detach** | Most code needs packages; almost none needs the internet. `docker network connect/disconnect` works on a running container | Network always on with a firewall; or no installs at all |
| S6 | **The command allowlist is defence in depth**, documented as not the boundary | `python -c` defeats any allowlist; pretending otherwise would be the real vulnerability | Relying on command filtering |
| S7 | **Checkpoints are git commits** in the workspace, with hooks disabled on every call | Diffs, rollback and per-file history for free; hooks are the one way git runs code | Copying the workspace per checkpoint |
| S8 | **Leftover processes are killed after every command** | A server started with `&` must not outlive its command or keep working between steps | Letting background processes run |
| S9 | **A sandbox that can't run its own cleanup is restarted** | At the process limit `docker exec` fails too; a restart ends everything and keeps the volumes | Raising the limit; or failing the task |
| S10 | **A driver interface** (`SandboxDriver`, `Sandbox`) | gVisor is a flag today; Firecracker or a remote sandbox would be a new driver, with no change to agents | Docker calls spread through the code |
| S11 | **Archives are stored in Postgres** | Results survive on hosts with no persistent disk; one thing to back up | Object storage (worth it past ~1 GB) |

## Platform

| # | Decision | Why | Rejected alternative |
| --- | --- | --- | --- |
| P1 | **Free-tier models: Gemini, with a fallback chain**, and an OpenAI-compatible adapter | The project must cost nothing to run. Free tiers fail often, so the router treats failure as normal | A single paid model |
| P2 | **Gemini over REST**, not the SDK | The model's turn must be sent back exactly as received; raw JSON makes that a one-liner and keeps the dependency list short | `google-genai` |
| P3 | **Server-Sent Events**, with Postgres as the record and Redis as the wake-up | One-way stream, automatic reconnect with `Last-Event-ID`, works through any proxy | WebSockets |
| P4 | **The UI is a reducer over the event log** | Live view, reconnect and the recorded demo are the same code path | Separate REST polling per panel |
| P5 | **The public demo replays recorded runs** | The sandbox needs a Docker host and no free tier offers one. Weakening isolation to fit a host would undo the point of the project | Running generated code as plain processes on a PaaS |
| P6 | **A Redis list as the queue**, worker in the API process by default | One process to run; the worker can be split out with one flag | Celery |
| P7 | **Single tenant**, one optional bearer token | It is a tool you run for yourself; accounts are a separate project | Building auth first |
