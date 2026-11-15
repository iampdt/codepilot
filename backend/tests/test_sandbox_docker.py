"""The real sandbox, against a Docker daemon. Each test is something generated code might
try, and what must happen instead. Needs the sandbox image and the egress proxy:

    docker compose up -d egress-proxy && docker compose --profile build build sandbox-image
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncIterator

import pytest

from app.sandbox import git
from app.sandbox.base import SandboxLimits, SandboxToolError
from app.sandbox.docker_driver import DockerDriver, DockerSandbox, container_name, workspace_volume


def _available() -> bool:
    if shutil.which("docker") is None:
        return False
    image = subprocess.run(
        ["docker", "image", "inspect", "codepilot-sandbox:latest"], capture_output=True
    )
    return image.returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _available(), reason="no Docker or sandbox image"),
]

LIMITS = SandboxLimits(memory_mb=256, cpus=1.0, pids=64, tmp_mb=64, workspace_mb=64, file_mb=8)


@pytest.fixture(scope="module")
def driver() -> DockerDriver:
    return DockerDriver(image="codepilot-sandbox:latest", network="codepilot-sandbox",
                        proxy_url="http://egress-proxy:3128")  # fmt: skip


@pytest.fixture(scope="module")
async def box(driver: DockerDriver) -> AsyncIterator[DockerSandbox]:
    sandbox = await driver.create("pytest-sbx", LIMITS)
    yield sandbox
    await sandbox.destroy()


async def run(
    box: DockerSandbox, command: str, timeout: int = 20, network: bool = False
) -> tuple[int, str]:
    result = await box.exec(command, timeout=timeout, network=network)
    return result.exit_code, result.output.strip()


async def test_runs_as_an_unprivileged_user_with_no_capabilities(box: DockerSandbox) -> None:
    assert (await run(box, "id -u"))[1] == "10001"
    status = (await run(box, "grep -E 'CapEff|CapBnd|NoNewPrivs' /proc/self/status"))[1]
    assert "CapEff:\t0000000000000000" in status and "CapBnd:\t0000000000000000" in status
    assert "NoNewPrivs:\t1" in status


async def test_root_filesystem_is_read_only_and_workspace_is_writable(box: DockerSandbox) -> None:
    for target in ["/usr/bin/x", "/etc/x", "/opt/codepilot/agentd.py", "/x"]:
        code, output = await run(box, f"touch {target}")
        assert code != 0 and ("Read-only" in output or "Permission denied" in output), target
    assert (await run(box, "touch /workspace/ok /tmp/ok /home/sandbox/ok"))[0] == 0


async def test_has_no_network_and_no_route_to_the_host(box: DockerSandbox) -> None:
    interfaces = (await run(box, "ls /sys/class/net"))[1].split()
    assert "eth0" not in interfaces and "lo" in interfaces
    probe = ("python -c \"import socket; s=socket.socket(); s.settimeout(3); "
             "s.connect(('{host}', {port}))\"")  # fmt: skip
    for host, port in [("1.1.1.1", 443), ("172.17.0.1", 2375), ("host.docker.internal", 80)]:
        assert (await run(box, probe.format(host=host, port=port)))[0] != 0


async def test_control_plane_is_out_of_reach(box: DockerSandbox) -> None:
    assert (await run(box, "ls /var/run/docker.sock"))[0] != 0
    assert (await run(box, "which docker"))[0] != 0
    environment = (await run(box, "env"))[1]
    for name in ["GEMINI_API_KEY", "DATABASE_URL", "REDIS_URL", "API_TOKEN", "AWS_", "DOCKER_HOST"]:
        assert name not in environment


async def test_installs_reach_package_registries_and_nothing_else(box: DockerSandbox) -> None:
    code, output = await run(
        box, "pip install --no-input itsdangerous==2.2.0", timeout=120, network=True
    )
    assert code == 0, output
    fetch = "python -c \"import urllib.request as u; u.urlopen('https://example.com', timeout=8)\""
    code, output = await run(box, fetch, network=True)
    assert code != 0 and "403" in output  # the proxy refused it
    # After the install the container is detached again.
    assert "eth0" not in (await run(box, "ls /sys/class/net"))[1]
    assert (await run(box, "python -c 'import itsdangerous'"))[0] == 0


async def test_timeout_kills_the_command_and_what_it_started(box: DockerSandbox) -> None:
    result = await box.exec("sleep 300 & sleep 300", timeout=2)
    assert result.timed_out and result.duration_ms < 15_000
    assert (await run(box, "pgrep -c sleep"))[1] == "1"  # only the container's idle process


async def test_background_processes_do_not_outlive_their_command(box: DockerSandbox) -> None:
    assert (await run(box, "python -m http.server 8123 > /tmp/s.log 2>&1 & sleep 1; echo started"))[
        0
    ] == 0
    assert (await run(box, "pgrep -f '[h]ttp.server'"))[0] != 0


async def test_fork_bomb_hits_the_process_limit_and_the_sandbox_survives(
    box: DockerSandbox,
) -> None:
    bomb = "python -c \"import os\nfor _ in range(500):\n    os.fork() or os._exit(__import__('time').sleep(5) or 0)\""
    result = await box.exec(bomb, timeout=20)
    assert result.exit_code != 0 and "Resource temporarily unavailable" in result.output
    assert (await run(box, "echo alive"))[1] == "alive"
    # Recovering from the limit restarts the container; it must come back without a network.
    assert "eth0" not in (await run(box, "ls /sys/class/net"))[1]


async def test_memory_limit_kills_the_process_not_the_host(box: DockerSandbox) -> None:
    result = await box.exec('python -c "x = bytearray(600 * 1024 * 1024)"', timeout=30)
    assert result.exit_code != 0
    assert (await run(box, "echo alive"))[1] == "alive"


async def test_file_size_limit_stops_a_disk_filler(box: DockerSandbox) -> None:
    code, _ = await run(box, "python -c \"open('/workspace/big','wb').write(b'0' * 20_000_000)\"")
    assert code != 0
    size = int((await run(box, "stat -c %s /workspace/big"))[1])
    assert size <= 8 * 1024 * 1024
    await run(box, "rm -f /workspace/big")


async def test_file_tools_cannot_be_pointed_outside_the_workspace(box: DockerSandbox) -> None:
    await run(box, "ln -s /etc /workspace/etc-link; ln -s /home/sandbox /workspace/home-link")
    for op, args in [
        ("read_file", {"path": "etc-link/passwd"}),
        ("write_file", {"path": "home-link/.bashrc", "content": "x"}),
        ("read_file", {"path": "../../etc/passwd"}),
        ("write_file", {"path": ".git/hooks/pre-commit", "content": "x"}),
    ]:
        with pytest.raises(SandboxToolError):
            await box.file_op(op, args)
    await run(box, "rm /workspace/etc-link /workspace/home-link")


async def test_a_planted_git_hook_does_not_run_on_checkpoint(box: DockerSandbox) -> None:
    await git.init_repo(box)
    await run(
        box,
        "printf '#!/bin/sh\\ntouch /workspace/HOOK_RAN\\n' > .git/hooks/pre-commit; chmod +x .git/hooks/pre-commit",
    )
    await box.file_op("write_file", {"path": "a.py", "content": "x = 1\n"})
    await git.checkpoint(box, "with hook planted")
    assert (await run(box, "test -e /workspace/HOOK_RAN"))[0] != 0


async def test_checkpoint_restore_and_diff(box: DockerSandbox) -> None:
    base = await git.base_commit(box)
    await box.file_op("write_file", {"path": "keep.py", "content": "v1\n"})
    saved = await git.checkpoint(box, "v1")
    await box.file_op("write_file", {"path": "keep.py", "content": "v2\n"})
    await box.file_op("write_file", {"path": "junk.py", "content": "x\n"})
    await git.restore(box, saved)
    assert (await box.file_op("read_file", {"path": "keep.py"}))["content"] == "v1\n"
    assert "junk.py" not in [c["path"] for c in await git.changes(box, base)]
    assert "+v1" in await git.diff(box, base, viewer=True)


async def test_sandboxes_are_isolated_from_each_other_and_cleaned_up(
    driver: DockerDriver, box: DockerSandbox
) -> None:
    await box.file_op("write_file", {"path": "secret.txt", "content": "task one\n"})
    other = await driver.create("pytest-sbx-2", LIMITS)
    try:
        assert (await other.file_op("list_files", {}))["entries"] == []
        assert (await run(other, "ls /workspace"))[1] == ""
    finally:
        await other.destroy()
    listed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True
    ).stdout
    volumes = subprocess.run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"], capture_output=True, text=True
    ).stdout
    assert (
        container_name("pytest-sbx-2") not in listed
        and workspace_volume("pytest-sbx-2") not in volumes
    )


async def test_orphan_cleanup_removes_sandboxes_no_task_owns(driver: DockerDriver) -> None:
    await driver.create("pytest-orphan", LIMITS)
    assert await driver.attach("pytest-orphan") is not None
    removed = await driver.cleanup(keep_task_ids={"pytest-sbx"})
    assert removed >= 1 and await driver.attach("pytest-orphan") is None
    assert await driver.attach("pytest-sbx") is not None


async def test_cancel_stops_a_running_command(box: DockerSandbox) -> None:
    cancel = asyncio.Event()
    job = asyncio.create_task(box.exec("sleep 120", timeout=120, cancel=cancel))
    await asyncio.sleep(1)
    cancel.set()
    result = await asyncio.wait_for(job, timeout=20)
    assert result.cancelled and not result.timed_out
