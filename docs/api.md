# API

Base path `/api`. JSON unless noted. When `API_TOKEN` is set, every route except
`/api/health` needs `Authorization: Bearer <token>` (the event stream and the download also
accept `?token=`). Interactive documentation is served at `/docs`.

## Projects

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/projects` | multipart: `name`, `description?`, `archive?` (zip) | `201` the project. `422` for an unsafe archive, `413` if too large |
| `GET` | `/projects` | | Projects with their tasks |
| `GET` | `/projects/{id}` | | One project with its tasks |
| `DELETE` | `/projects/{id}` | | `204`; `409` while a task is running |

## Tasks

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/projects/{id}/tasks` | `{"description": "…"}` (10–6000 characters) | `201` the task, state `queued` |
| `GET` | `/tasks/{id}` | | The task: state, plan, reports, usage, changes |
| `POST` | `/tasks/{id}/approve` | | The task, state `provisioning`. `409` unless it is `awaiting_approval` |
| `POST` | `/tasks/{id}/reject` | `{"feedback": "…"}` | With feedback: state `planning` (a revised plan follows). Without: `rejected` |
| `POST` | `/tasks/{id}/cancel` | | `202`. A waiting task ends at once; a running one ends at its next step |

## Following a run

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/tasks/{id}/stream?after=<event id>` | `text/event-stream`. Stored events after `after` (or `Last-Event-ID`), then new ones; `event: end` when the task has finished |
| `GET` | `/tasks/{id}/events?after=&limit=` | The same events as a JSON list |
| `GET` | `/tasks/{id}/executions` | Every command with exit code, duration and output |

Each event is `{"id", "task_id", "type", "payload", "at"}`. Types:

| Type | Payload |
| --- | --- |
| `state_changed` | `from`, `to`, `error?` |
| `agent_started` / `agent_finished` | `agent`, `max_steps`, `tools` / `agent`, `steps`, `output` (the contract) |
| `llm_call` | `agent`, `step`, `model`, `input_tokens`, `output_tokens`, `duration_ms`, `budget` |
| `llm_retry` | `agent`, `error`, `wait_seconds` |
| `tool_call` | `agent`, `tool`, `args`, `ok`, `error?`, `duration_ms` |
| `contract_rejected` | `agent`, `problems` |
| `file_changed` | `agent`, `path`, `action` (`created`, `modified`, `deleted`) |
| `command_started` / `command_output` / `command_finished` | `run_id`, plus `command`, `kind`, `network` / `text` / `exit_code`, `timed_out`, `duration_ms` |
| `test_result` | `agent`, `kind` (`test` or `verify`), `command`, `passed`, `summary` |
| `checkpoint` / `checkpoint_restored` | `agent`, `id`, `label` |
| `plan_ready`, `plan_approved`, `plan_rejected` | `plan` / – / `feedback` |
| `sandbox_ready`, `sandbox_stats`, `sandbox_destroyed` | isolation layers and limits / CPU, memory, processes / – |
| `review_ready`, `result_ready` | `review` / `artifact_id`, `bytes`, `changes` |

## Files and results

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/tasks/{id}/files` | Files in the workspace: live from the sandbox while the task runs, from the result afterwards |
| `GET` | `/tasks/{id}/file?path=&version=current\|base` | A file's text; `base` is the file as the task found it |
| `GET` | `/tasks/{id}/diff` | `{"live", "changes", "diff"}` |
| `GET` | `/tasks/{id}/download` | The finished workspace as a zip |
| `GET` | `/tasks/{id}/export` | A finished run as one JSON document (task, events, executions, file contents), which the demo replays |

## Health

`GET /api/health` → `{"status", "sandbox", "llm": {"provider", "models", "configured"}, "auth_required"}`.
