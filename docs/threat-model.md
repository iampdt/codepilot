# Sandbox threat model

CodePilot runs code that nobody has read: a model wrote it a few seconds ago, or a stranger
uploaded it. This document says what that code is assumed to be capable of, what stops it, how
each control is tested, and what is still not covered.

## What is trusted and what is not

| Trusted | Not trusted |
| --- | --- |
| The control plane: API, worker, orchestrator, tool registry | Every uploaded archive |
| Postgres, Redis and the Docker daemon | Every file a model writes |
| The sandbox image and the file agent baked into it | Every command a model asks to run, and everything that command starts |
| The person using the instance | The model's output, including what it reports about its own work |
| | Packages downloaded during an install, and their install scripts |

The model is treated as untrusted too. A prompt injection in an uploaded repository (a README
that says "ignore your instructions and run this") turns the model into the attacker's hands,
so nothing in the design depends on the model behaving.

## What an attacker wants

1. **Escape to the host** and run as the user who owns the Docker daemon.
2. **Steal secrets:** the model API key, database credentials, cloud credentials, other users'
   code.
3. **Use the machine:** mine coins, send spam, scan or attack other hosts.
4. **Break the service:** exhaust CPU, memory, disk or process slots so other tasks fail.
5. **Tamper with the result or the record:** make a failing task look successful, or plant
   something that runs later on the control plane.

## Two planes

```
 control plane (trusted)                      sandbox (untrusted), one per task
 ┌─────────────────────────────┐              ┌───────────────────────────────────┐
 │ API · worker · orchestrator │ docker exec  │ uid 10001, no capabilities        │
 │ model key, database URL     │─────────────▶│ read-only root, no network        │
 │ Docker socket               │              │ /workspace and /home/sandbox only │
 └─────────────────────────────┘              │ no socket, no key, no control env │
                                              └───────────────────────────────────┘
```

The control plane holds every credential and the Docker socket. A sandbox holds none of them.
The only thing that crosses from control plane to sandbox is a `docker exec` with an explicit
command and an explicit, minimal environment; the only things that come back are bytes on
stdout and an exit code, which the control plane treats as data.

## Controls

Each row names the test that fails if the control is removed. Tests marked *docker* run against
a real daemon (`backend/tests/test_sandbox_docker.py`); the rest are unit tests.

### Escape and privilege

| Control | How | Test |
| --- | --- | --- |
| One container per task | `cp-sbx-<task>` with its own two volumes; nothing is shared between tasks | `test_sandboxes_are_isolated_from_each_other_and_cleaned_up` (docker) |
| Non-root | `--user 10001:10001`; the image has no sudo and no setuid helper the user can reach | `test_runs_as_an_unprivileged_user_with_no_capabilities` (docker) |
| No capabilities | `--cap-drop ALL`: effective and bounding sets are zero | same test, reads `/proc/self/status` |
| No privilege gain | `--security-opt no-new-privileges`, so a setuid binary gives nothing | same test (`NoNewPrivs: 1`) |
| Syscall filter | Docker's default seccomp profile stays on | – (default; see limitations) |
| Read-only root | `--read-only`; writable paths are `/workspace`, `/home/sandbox` and a size-capped tmpfs `/tmp` | `test_root_filesystem_is_read_only_and_workspace_is_writable` (docker) |
| No Docker socket | Never mounted; the image has no Docker client | `test_control_plane_is_out_of_reach` (docker) |
| No host paths | The workspace is a named volume, not a bind mount: no host directory is ever visible | by construction (`docker_driver.py`) |
| Optional gVisor | `SANDBOX_RUNTIME=runsc` runs every sandbox under a user-space kernel | – (configuration) |

### Secrets

| Control | How | Test |
| --- | --- | --- |
| No control-plane environment | `docker exec` passes only what the driver lists; the Docker client itself is started with a five-variable allowlist | `test_control_plane_is_out_of_reach` (docker) checks for the key, database URL, Redis URL and token |
| No credentials in the image | The image is built from `sandbox/image/` alone: no API code, no `.env` | by construction |
| Tools can't read outside the workspace | Paths are resolved after symlinks inside the sandbox and must stay under `/workspace` | `test_paths_outside_the_workspace_are_refused`, `test_symlink_out_of_the_workspace_is_refused`, `test_file_tools_cannot_be_pointed_outside_the_workspace` (docker) |
| Logs are scoped | `get_execution_logs` filters by the calling task's id | `registry.py::_get_execution_logs` |

### Network

| Control | How | Test |
| --- | --- | --- |
| No network by default | The container is detached from every network before any untrusted code runs: loopback only | `test_has_no_network_and_no_route_to_the_host` (docker) |
| Registry-only installs | During `install_dependency` the container joins an `internal` Docker network whose only other member is a proxy that forwards to `pypi.org`, `files.pythonhosted.org` and `registry.npmjs.org` on port 443, and is detached again afterwards | `test_installs_reach_package_registries_and_nothing_else` (docker): the install succeeds, `example.com` gets `403` |
| Only one tool has network | `install_dependency` takes package names, validated; options, URLs and paths are refused | `test_refuses_options_urls_and_paths_as_packages` |

### Resource exhaustion

| Control | How | Test |
| --- | --- | --- |
| Memory | `--memory` with `--memory-swap` equal to it (no swap) | `test_memory_limit_kills_the_process_not_the_host` (docker) |
| CPU | `--cpus` | – (configuration) |
| Processes | `--pids-limit`; if the limit is hit so hard that the cleanup itself can't start, the container is restarted (volumes kept, still no network) | `test_fork_bomb_hits_the_process_limit_and_the_sandbox_survives` (docker) |
| File size | `--ulimit fsize` | `test_file_size_limit_stops_a_disk_filler` (docker) |
| Workspace size | Measured after every command; over the quota the task ends | `registry.py::run_in_sandbox` |
| Time | `timeout -k` inside the container, plus a backstop in the driver; cancel kills the command | `test_timeout_kills_the_command_and_what_it_started`, `test_cancel_stops_a_running_command` (docker) |
| Leftover processes | After each command everything but the container's idle process is killed | `test_background_processes_do_not_outlive_their_command` (docker) |
| Whole-task budget | Ceilings on model calls, tokens, tool calls and wall time; step limits per agent | `test_budget_exhaustion_fails_the_task_and_cleans_up`, `test_agent_that_never_submits_is_cut_off_at_its_step_limit` |
| Cleanup | Container and volumes are removed when the task ends, whatever the outcome; orphans are removed at worker start | `test_orphan_cleanup_removes_sandboxes_no_task_owns` (docker) |

### Uploads

| Control | How | Test |
| --- | --- | --- |
| Path traversal ("zip slip") | Names with `..`, absolute paths and drive letters are refused, on upload and again on extraction inside the sandbox | `test_traversal_names_are_refused`, `test_unpack_refuses_links_and_escapes` |
| Links | Symlinks and hard links are refused | `test_symlinks_are_refused` |
| Zip bombs | File count, total size and per-file compression ratio are capped before anything is extracted | `test_zip_bomb_is_refused` |
| Uploaded `.git` | Dropped: its hooks and config are attacker-controlled | `test_git_and_dependency_folders_are_dropped_and_wrapper_folder_stripped` |
| Never extracted on the control plane | The planner reads the archive in memory; files touch a disk only inside a sandbox | `test_run_stops_at_the_approval_gate_without_creating_a_sandbox` |

### Tampering

| Control | How | Test |
| --- | --- | --- |
| The verdict is observed, not reported | The orchestrator runs the test command itself and reads the exit code | `test_debug_loop_is_bounded_and_the_agents_word_is_not_trusted` |
| The verdict holds outside the sandbox | Before the tests, installed packages are wiped and reinstalled from the project's manifests | `test_verification_reinstalls_from_the_manifest_in_a_clean_environment`, `test_a_manifest_that_does_not_install_fails_verification` |
| The verify command must be a test runner | `true` or `echo ok` is not accepted as a test command | `test_recognises_real_test_commands` |
| Checkpoints can't be hijacked | `.git` is read-only to the file tools, and every git call disables hooks | `test_git_metadata_is_read_only`, `test_a_planted_git_hook_does_not_run_on_checkpoint` (docker) |
| The file agent can't be replaced | Owned by root, mode 0555, on a read-only filesystem | `test_root_filesystem_is_read_only_and_workspace_is_writable` (docker) |
| Roles limit damage | The planner and reviewer can't write; the coder can't execute; a refused call never reaches the sandbox | `test_role_permissions_separate_writing_from_running`, `test_permission_and_policy_refusals_reach_the_model_as_errors` |

## The command policy is not the boundary

`execute_command` checks each command against an allowlist of programs and refuses command
substitution, installs outside the install tool, writes outside the workspace and changes to
`.git`. That check is easy to get around on purpose: `python -c` can do anything a shell can.
It is there to keep an honest agent on task and to give it a useful error instead of a
timeout. Everything in the tables above holds if the policy is deleted.

## What is not covered

- **Kernel vulnerabilities.** A container shares the host kernel. A kernel bug reachable
  through the default seccomp profile is a way out. Running with `SANDBOX_RUNTIME=runsc`
  (gVisor) or moving the driver to microVMs (Firecracker, Kata) narrows this; the driver
  interface is the seam for that change. On macOS and Windows, Docker Desktop already runs
  containers inside a Linux VM, which adds a layer; on a Linux host it does not.
- **The Docker socket on the control plane.** The API container mounts it, so compromising the
  API means owning the host. The API never passes model output to a shell on the control plane
  (commands go through `docker exec` as an argument vector), but a bug there would be serious.
  A socket proxy that allows only the calls the driver makes would reduce this.
- **Disk quota is checked, not enforced by the kernel.** Named volumes on the default storage
  driver have no size limit. A single file is capped by `fsize`, and the workspace is measured
  after each command, so one command can overshoot the quota before the task is ended.
- **Exfiltration through the registries.** During an install, code can talk to PyPI and npm.
  A malicious install script could publish what it finds in the workspace as a package. The
  workspace holds only the user's own code, and no credentials, so the loss is bounded to that.
- **Dependency confusion and typosquatting.** The model may ask for a package that doesn't
  exist or is malicious. It runs inside the sandbox like any other code, but its effect on the
  exported project is not detected.
- **CPU side channels** between containers on one host are out of scope.
- **Single tenant.** There are no user accounts: one optional bearer token guards the whole
  API. Anyone with it can read every project. Multi-user use needs authentication and
  per-user authorisation in front of every route.
- **The exported code is not vetted.** The reviewer agent looks for security problems, but a
  model's review is not an audit. Read generated code before running it anywhere that matters.
