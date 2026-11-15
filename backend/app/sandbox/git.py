"""Checkpoints and diffs, kept in a git repository inside the sandbox workspace.

These commands are fixed strings issued by the control plane, not by an agent, so they don't
go through the command policy. Hooks are disabled on every call: generated code can write to
`.git/hooks` with a shell command, and a hook must never run as part of a checkpoint.
"""

from __future__ import annotations

import re
from typing import Any

from app.sandbox.base import Sandbox, SandboxError

GIT = (
    "git -c core.hooksPath=/dev/null -c user.name=CodePilot "
    "-c user.email=codepilot@localhost -c advice.detachedHead=false -c core.quotePath=false"
)
# Kept in .git/info/exclude so the project's own .gitignore is left alone.
EXCLUDES = "\n".join(
    ["node_modules/", "__pycache__/", "*.pyc", ".pytest_cache/", ".venv/", "venv/",
     ".mypy_cache/", ".ruff_cache/", "*.egg-info/", ".coverage", "dist/", "build/"]
)  # fmt: skip
SHA = re.compile(r"^[0-9a-f]{7,40}$")
MAX_DIFF_CHARS = 400_000


async def _run(sandbox: Sandbox, command: str, env: dict[str, str] | None = None) -> str:
    result = await sandbox.exec(command, timeout=60, env=env or None)
    if result.exit_code != 0:
        raise SandboxError(f"git failed ({result.exit_code}): {result.output[-400:]}")
    return result.output


async def init_repo(sandbox: Sandbox) -> str:
    """Create the repository and commit the starting state. Returns the base commit."""
    await _run(
        sandbox,
        f"{GIT} init -q -b main . && mkdir -p .git/info && printf '%s\\n' \"$CP_EXCLUDES\" "
        f"> .git/info/exclude && {GIT} add -A && {GIT} commit -q --allow-empty -m base",
        env={"CP_EXCLUDES": EXCLUDES},
    )
    return (await _run(sandbox, f"{GIT} rev-parse HEAD")).strip()


async def base_commit(sandbox: Sandbox) -> str:
    """The commit `init_repo` made: the root of the history."""
    return (await _run(sandbox, f"{GIT} rev-list --max-parents=0 HEAD")).strip().splitlines()[0]


async def checkpoint(sandbox: Sandbox, label: str) -> str:
    await _run(
        sandbox,
        f'{GIT} add -A && {GIT} commit -q --allow-empty -m "checkpoint: $CP_LABEL"',
        env={"CP_LABEL": label[:80]},
    )
    return (await _run(sandbox, f"{GIT} rev-parse HEAD")).strip()


async def restore(sandbox: Sandbox, sha: str) -> None:
    if not SHA.match(sha):
        raise SandboxError("invalid checkpoint id")
    await _run(sandbox, f"{GIT} reset -q --hard {sha} && {GIT} clean -fdq")


# A viewer (the API showing a live diff) stages into its own index file, so it never takes
# the lock on the repository's index while the worker is making a checkpoint.
VIEW_INDEX = {"GIT_INDEX_FILE": "/tmp/codepilot-view-index"}  # noqa: S108 - inside the sandbox


async def diff(
    sandbox: Sandbox,
    base: str,
    path: str | None = None,
    stat: bool = False,
    viewer: bool = False,
) -> str:
    """Everything that changed since `base`, including files that are new and uncommitted."""
    if not SHA.match(base):
        raise SandboxError("invalid base commit")
    flags = "--stat" if stat else ""
    env = {**(VIEW_INDEX if viewer else {}), **({"CP_PATH": path} if path else {})}
    target = '-- "$CP_PATH"' if path else ""
    output = await _run(
        sandbox, f"{GIT} add -A && {GIT} diff --cached {flags} {base} {target}", env=env
    )
    return output[:MAX_DIFF_CHARS]


async def changes(sandbox: Sandbox, base: str, viewer: bool = False) -> list[dict[str, Any]]:
    """Changed files since `base` with added/removed line counts."""
    if not SHA.match(base):
        raise SandboxError("invalid base commit")
    env = VIEW_INDEX if viewer else None
    status = await _run(
        sandbox, f"{GIT} add -A && {GIT} diff --cached --name-status {base}", env=env
    )
    numstat = await _run(sandbox, f"{GIT} diff --cached --numstat {base}", env=env)
    counts: dict[str, tuple[int, int]] = {}
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            added = int(parts[0]) if parts[0].isdigit() else 0
            removed = int(parts[1]) if parts[1].isdigit() else 0
            counts[parts[2]] = (added, removed)
    names = {"A": "added", "M": "modified", "D": "deleted"}
    result = []
    for line in status.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            path = parts[-1]
            added, removed = counts.get(path, (0, 0))
            result.append(
                {
                    "path": path,
                    "status": names.get(parts[0][0], "modified"),
                    "added": added,
                    "removed": removed,
                }
            )
    return result


async def show(sandbox: Sandbox, commit: str, path: str) -> str | None:
    """A file's content at a commit, or None if it didn't exist there."""
    if not SHA.match(commit):
        raise SandboxError("invalid commit")
    result = await sandbox.exec(
        f'{GIT} show {commit}:"$CP_PATH"', timeout=30, env={"CP_PATH": path}
    )
    return result.output if result.exit_code == 0 else None
