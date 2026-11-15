"""Docker sandbox driver: one locked-down container per task.

The control plane talks to the Docker daemon through the `docker` CLI as a subprocess. The
sandbox never sees the daemon: no socket is mounted into it, and its image has no client.

What a sandbox gets:
  * its own container, with two named volumes (workspace, home) that no other task mounts;
  * an unprivileged user (uid 10001), all capabilities dropped, no-new-privileges;
  * a read-only root filesystem; /tmp is a size-capped tmpfs;
  * memory, CPU and process-count limits, no swap;
  * no network interface except loopback. For a dependency install the container is attached
    to an internal network whose only other member is the registry-allowlist proxy, and
    detached again when the install ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from typing import Any

from app.logging import get_logger
from app.sandbox.base import (
    ExecResult,
    IsolationReport,
    OutputCallback,
    SandboxError,
    SandboxLimits,
    SandboxToolError,
)

log = get_logger("sandbox.docker")

SANDBOX_USER = "10001:10001"
WORKDIR = "/workspace"
AGENTD = ["python3", "-I", "/opt/codepilot/agentd.py"]
LABEL = "codepilot.task"

# Output kept per command: the head for context, the tail for the error.
OUTPUT_HEAD_BYTES = 20_000
OUTPUT_TAIL_BYTES = 100_000

# The docker client gets a minimal environment: none of the control plane's secrets are
# needed to reach the daemon, so none are passed along.
_CLIENT_ENV_KEYS = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")


def _client_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _CLIENT_ENV_KEYS if k in os.environ}


async def _docker(
    *args: str, stdin: bytes | None = None, timeout: float = 60, check: bool = True
) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_client_env(),
        )
    except FileNotFoundError as exc:
        raise SandboxError("docker CLI not found on the control plane") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise SandboxError(f"docker {args[0]} timed out after {timeout}s") from exc
    code = proc.returncode or 0
    if check and code != 0:
        raise SandboxError(f"docker {args[0]} failed: {err.decode(errors='replace')[:400]}")
    return code, out, err


class _OutputBuffer:
    """Keeps the first and last part of a stream and drops the middle."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = OUTPUT_HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            overflow = len(self.tail) - OUTPUT_TAIL_BYTES
            if overflow > 0:
                del self.tail[:overflow]
                self.dropped += overflow

    def text(self) -> tuple[str, bool]:
        head = self.head.decode("utf-8", errors="replace")
        tail = self.tail.decode("utf-8", errors="replace")
        if self.dropped:
            return f"{head}\n… [{self.dropped} bytes omitted] …\n{tail}", True
        return head + tail, False


class DockerSandbox:
    def __init__(
        self,
        task_id: str,
        *,
        network: str,
        proxy_url: str,
        isolation: IsolationReport,
        sweep: bool = True,
    ) -> None:
        self.id = container_name(task_id)
        self.task_id = task_id
        self.isolation = isolation
        self._network = network
        self._proxy_url = proxy_url
        self._network_lock = asyncio.Lock()
        # The worker's handle ends stray processes after each command. A handle the API
        # attaches for viewing must not: it would kill the command the worker is running.
        self._sweep = sweep

    # --- files -----------------------------------------------------------------------------

    async def file_op(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        request = json.dumps({"op": op, "args": args}).encode()
        code, out, err = await _docker(
            "exec", "-i", "-u", SANDBOX_USER, "-w", WORKDIR, self.id, *AGENTD,
            stdin=request, timeout=30, check=False,
        )  # fmt: skip
        if code != 0:
            raise SandboxError(f"file agent failed: {err.decode(errors='replace')[:300]}")
        try:
            reply = json.loads(out)
        except ValueError as exc:
            raise SandboxError("file agent returned invalid JSON") from exc
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise SandboxToolError(error.get("code", "error"), error.get("message", "failed"))
        result: dict[str, Any] = reply["result"]
        return result

    async def unpack(self, tar_bytes: bytes) -> dict[str, Any]:
        code, out, err = await _docker(
            "exec", "-i", "-u", SANDBOX_USER, "-w", WORKDIR, self.id, *AGENTD, "unpack",
            stdin=tar_bytes, timeout=120, check=False,
        )  # fmt: skip
        if code != 0:
            raise SandboxError(f"unpack failed: {err.decode(errors='replace')[:300]}")
        reply = json.loads(out)
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise SandboxToolError(error.get("code", "error"), error.get("message", "failed"))
        result: dict[str, Any] = reply["result"]
        return result

    async def pack(self) -> bytes:
        code, out, err = await _docker(
            "exec", "-u", SANDBOX_USER, "-w", WORKDIR, self.id, *AGENTD, "pack",
            timeout=120, check=False,
        )  # fmt: skip
        if code != 0:
            raise SandboxError(f"pack failed: {err.decode(errors='replace')[:300]}")
        return out

    # --- commands --------------------------------------------------------------------------

    async def exec(
        self,
        command: str,
        *,
        timeout: int,
        network: bool = False,
        env: dict[str, str] | None = None,
        on_output: OutputCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> ExecResult:
        if network:
            async with self._network_lock:
                await _docker("network", "connect", self._network, self.id, check=False)
                try:
                    proxy = {
                        "HTTP_PROXY": self._proxy_url,
                        "HTTPS_PROXY": self._proxy_url,
                        "http_proxy": self._proxy_url,
                        "https_proxy": self._proxy_url,
                    }
                    return await self._exec(
                        command, timeout, {**proxy, **(env or {})}, on_output, cancel
                    )
                finally:
                    await _docker(
                        "network", "disconnect", "-f", self._network, self.id, check=False
                    )
        return await self._exec(command, timeout, env or {}, on_output, cancel)

    async def _exec(
        self,
        command: str,
        timeout: int,
        env: dict[str, str],
        on_output: OutputCallback | None,
        cancel: asyncio.Event | None,
    ) -> ExecResult:
        env_args: list[str] = []
        for key, value in env.items():
            env_args += ["-e", f"{key}={value}"]
        # `timeout` inside the container ends the command even if this client is killed;
        # the outer wait below is the backstop for a command that ignores SIGTERM and SIGKILL.
        argv = [
            "exec", "-u", SANDBOX_USER, "-w", WORKDIR, *env_args, self.id,
            "timeout", "-k", "5", str(timeout), "/bin/sh", "-c", command,
        ]  # fmt: skip
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_client_env(),
        )
        buffer = _OutputBuffer()

        async def pump() -> None:
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(4096):
                buffer.add(chunk)
                if on_output is not None:
                    await on_output(chunk.decode("utf-8", errors="replace"))

        pump_task = asyncio.create_task(pump())
        wait_task = asyncio.create_task(proc.wait())
        cancel_task = asyncio.create_task(cancel.wait()) if cancel is not None else None
        waiters = {wait_task} | ({cancel_task} if cancel_task else set())
        done, _ = await asyncio.wait(
            waiters, timeout=timeout + 15, return_when=asyncio.FIRST_COMPLETED
        )
        timed_out = False
        cancelled = False
        if wait_task not in done:
            cancelled = cancel_task is not None and cancel_task in done
            timed_out = not cancelled
            await self.kill_all()
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        if cancel_task is not None:
            cancel_task.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(pump_task, timeout=5)
        exit_code = proc.returncode if proc.returncode is not None else -1
        # coreutils timeout exits 124 when it had to stop the command (137 after -k's SIGKILL).
        if exit_code in (124, 137) and time.monotonic() - started >= timeout:
            timed_out = True
        # Anything the command left running (a server started with `&`) ends with the command.
        if self._sweep:
            await self.kill_all()
        output, truncated = buffer.text()
        return ExecResult(
            exit_code=exit_code,
            output=output,
            timed_out=timed_out,
            duration_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated,
            cancelled=cancelled,
        )

    async def kill_all(self) -> None:
        """Kill every process in the container except init and the idle process it started.

        `kill -9 -1` would be shorter but also ends the idle process, and with it the
        container. The oldest child of pid 1 is that idle process; everything else goes."""
        sweep = (
            "keep=$(pgrep -o -P 1); for p in $(ps -eo pid=); do "
            '[ "$p" = 1 ] || [ "$p" = "$keep" ] || [ "$p" = "$$" ] || kill -9 "$p" 2>/dev/null; '
            "done; true"
        )
        code, _, _ = await _docker(
            "exec", "-u", SANDBOX_USER, self.id, "/bin/sh", "-c", sweep, timeout=15, check=False
        )
        if code != 0:
            # A sandbox at its process limit can't start the sweep either. Restarting the
            # container ends everything in it; the workspace and home volumes are kept, and
            # it comes back with no network because none is attached.
            log.warning("sandbox_restart", task_id=self.task_id, reason="process sweep failed")
            await _docker("restart", "-t", "0", self.id, timeout=60, check=False)

    # --- lifecycle -------------------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        code, out, _ = await _docker(
            "stats", "--no-stream", "--format", "{{json .}}", self.id, timeout=15, check=False
        )
        if code != 0 or not out.strip():
            return {}
        raw = json.loads(out.decode().strip().splitlines()[0])
        return {
            "cpu_percent": raw.get("CPUPerc", ""),
            "memory": raw.get("MemUsage", ""),
            "memory_percent": raw.get("MemPerc", ""),
            "pids": raw.get("PIDs", ""),
        }

    async def destroy(self) -> None:
        await _docker("rm", "-f", "-v", self.id, timeout=60, check=False)
        await _docker(
            "volume", "rm", "-f", workspace_volume(self.task_id), home_volume(self.task_id),
            timeout=60, check=False,
        )  # fmt: skip
        log.info("sandbox_destroyed", task_id=self.task_id)


def container_name(task_id: str) -> str:
    return f"cp-sbx-{task_id}"


def workspace_volume(task_id: str) -> str:
    return f"cp-ws-{task_id}"


def home_volume(task_id: str) -> str:
    return f"cp-home-{task_id}"


class DockerDriver:
    name = "docker"

    def __init__(self, *, image: str, network: str, proxy_url: str, runtime: str = "") -> None:
        self._image = image
        self._network = network
        self._proxy_url = proxy_url
        self._runtime = runtime

    async def ensure_network(self) -> None:
        """The sandbox network is internal: Docker gives it no route off the host."""
        code, _, _ = await _docker("network", "inspect", self._network, check=False)
        if code != 0:
            await _docker("network", "create", "--internal", self._network)

    async def create(self, task_id: str, limits: SandboxLimits) -> DockerSandbox:
        name = container_name(task_id)
        await self.ensure_network()
        await _docker("rm", "-f", "-v", name, check=False)
        for volume in (workspace_volume(task_id), home_volume(task_id)):
            await _docker("volume", "rm", "-f", volume, check=False)
            await _docker("volume", "create", "--label", f"{LABEL}={task_id}", volume)
        args = [
            "run", "-d", "--name", name, "--hostname", "sandbox",
            "--label", f"{LABEL}={task_id}",
            "--init",
            "--user", SANDBOX_USER,
            "--workdir", WORKDIR,
            "--network", self._network,
            "--read-only",
            "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size={limits.tmp_mb}m",
            "--mount", f"type=volume,src={workspace_volume(task_id)},dst={WORKDIR}",
            "--mount", f"type=volume,src={home_volume(task_id)},dst=/home/sandbox",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(limits.pids),
            "--memory", f"{limits.memory_mb}m",
            "--memory-swap", f"{limits.memory_mb}m",
            "--cpus", str(limits.cpus),
            "--ulimit", "nofile=4096:4096",
            "--ulimit", f"fsize={limits.file_mb * 1024 * 1024}",
        ]  # fmt: skip
        if self._runtime:
            args += ["--runtime", self._runtime]
        args += [self._image, "sleep", "infinity"]
        await _docker(*args, timeout=120)
        # Detach before any untrusted code has run: from here on the container has loopback only.
        await _docker("network", "disconnect", "-f", self._network, name)
        isolation = IsolationReport(
            driver="docker" + (f"+{self._runtime}" if self._runtime else ""),
            layers=[
                "dedicated container and volumes per task",
                "non-root user (uid 10001)",
                "all Linux capabilities dropped",
                "no-new-privileges",
                "read-only root filesystem",
                "default seccomp profile",
                "no network (loopback only); registry-only proxy during installs",
                "no Docker socket, no control-plane environment",
            ],
            limits={
                "memory_mb": limits.memory_mb,
                "cpus": limits.cpus,
                "pids": limits.pids,
                "tmp_mb": limits.tmp_mb,
                "workspace_mb": limits.workspace_mb,
                "file_mb": limits.file_mb,
            },
        )
        log.info("sandbox_created", task_id=task_id, container=name)
        return DockerSandbox(
            task_id, network=self._network, proxy_url=self._proxy_url, isolation=isolation
        )

    async def attach(self, task_id: str) -> DockerSandbox | None:
        code, out, _ = await _docker(
            "inspect", "-f", "{{.State.Running}}", container_name(task_id), check=False
        )
        if code != 0 or out.decode().strip() != "true":
            return None
        return DockerSandbox(
            task_id,
            network=self._network,
            proxy_url=self._proxy_url,
            isolation=IsolationReport(driver="docker"),
            sweep=False,
        )

    async def cleanup(self, keep_task_ids: set[str]) -> int:
        """Remove containers and volumes whose task is no longer running."""
        removed = 0
        _, out, _ = await _docker(
            "ps", "-a", "--filter", f"label={LABEL}", "--format", f'{{{{.Label "{LABEL}"}}}}',
            check=False,
        )  # fmt: skip
        for task_id in {line.strip() for line in out.decode().splitlines() if line.strip()}:
            if task_id not in keep_task_ids:
                await _docker("rm", "-f", "-v", container_name(task_id), check=False)
                removed += 1
        _, out, _ = await _docker(
            "volume", "ls", "--filter", f"label={LABEL}", "--format", f'{{{{.Label "{LABEL}"}}}}',
            check=False,
        )  # fmt: skip
        for task_id in {line.strip() for line in out.decode().splitlines() if line.strip()}:
            if task_id not in keep_task_ids:
                await _docker(
                    "volume", "rm", "-f", workspace_volume(task_id), home_volume(task_id),
                    check=False,
                )  # fmt: skip
        return removed
