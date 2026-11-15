#!/usr/bin/env python3
"""File agent that runs inside a task sandbox.

The control plane never opens workspace files itself. Every file operation is one
`docker exec` of this script as the sandbox user: a JSON request on stdin, a JSON reply on
stdout. Paths are resolved here, after symlinks, and must stay inside the workspace, so a
symlink planted by generated code can't point a tool at another part of the container.

Standard library only: the sandbox image must not need anything from the control plane.

Usage:
    agentd.py            JSON request on stdin: {"op": "...", "args": {...}}
    agentd.py unpack     tar stream on stdin, extracted into the workspace
    agentd.py pack       zip of the workspace on stdout
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile

ROOT = os.path.realpath(os.environ.get("CODEPILOT_WORKSPACE", "/workspace"))

# Directories the tools never descend into: version control and dependency or cache trees.
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
}
# Paths the agent may not write or delete: git metadata holds the checkpoints and could hold hooks.
PROTECTED_TOP_LEVEL = {".git"}

MAX_WRITE_BYTES = 1_000_000
MAX_READ_BYTES = 200_000
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_UNPACK_BYTES = 50_000_000
MAX_UNPACK_FILES = 5_000
MAX_PACK_BYTES = 25_000_000


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve(rel: object, *, for_write: bool = False) -> str:
    """Map a workspace-relative path to a real path, or raise if it leaves the workspace."""
    if not isinstance(rel, str) or rel == "":
        raise ToolError("invalid_path", "path must be a non-empty string")
    if "\x00" in rel:
        raise ToolError("invalid_path", "path contains a NUL byte")
    if os.path.isabs(rel):
        raise ToolError("path_outside_workspace", "absolute paths are not allowed")
    # realpath resolves every symlink in the part of the path that exists and normalises the
    # rest, so `a/../../etc` and a symlink to `/etc` both end up outside ROOT and are refused.
    real = os.path.realpath(os.path.join(ROOT, rel))
    if real != ROOT and not real.startswith(ROOT + os.sep):
        raise ToolError("path_outside_workspace", f"{rel!r} resolves outside the workspace")
    if for_write:
        if real == ROOT:
            raise ToolError("invalid_path", "the workspace root itself can't be modified")
        top = os.path.relpath(real, ROOT).split(os.sep)[0]
        if top in PROTECTED_TOP_LEVEL:
            raise ToolError("protected_path", f"{top}/ is managed by CodePilot and is read-only")
    return real


def rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return True


def walk(start: str):
    """Yield (dirpath, dirnames, filenames) below start, without following symlinks."""
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        yield dirpath, dirnames, sorted(filenames)


def op_list_files(args: dict) -> dict:
    start = resolve(args.get("path", "."))
    if not os.path.isdir(start):
        raise ToolError("not_a_directory", f"{args.get('path', '.')!r} is not a directory")
    limit = min(int(args.get("max_entries", 500)), 5000)
    entries: list[dict] = []
    truncated = False
    for dirpath, dirnames, filenames in walk(start):
        for name in dirnames:
            entries.append({"path": rel(os.path.join(dirpath, name)) + "/", "type": "dir"})
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                size = os.lstat(full).st_size
            except OSError:
                continue
            entries.append({"path": rel(full), "type": "file", "size": size})
        if len(entries) >= limit:
            truncated = True
            break
    entries.sort(key=lambda e: e["path"])
    return {"entries": entries[:limit], "truncated": truncated}


def op_read_file(args: dict) -> dict:
    path = resolve(args.get("path"))
    if not os.path.isfile(path):
        raise ToolError("not_found", f"{args.get('path')!r} is not a file")
    if is_binary(path):
        raise ToolError("binary_file", "file looks binary and can't be read as text")
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines(keepends=True)
    total = len(lines)
    start = max(int(args.get("start_line") or 1), 1)
    end = min(int(args.get("end_line") or total), total)
    selected = lines[start - 1 : end]
    content = "".join(selected)
    truncated = False
    if len(content.encode("utf-8")) > MAX_READ_BYTES:
        content = content.encode("utf-8")[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        truncated = True
    return {
        "path": rel(path),
        "content": content,
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "truncated": truncated,
    }


def atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".cp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def op_write_file(args: dict) -> dict:
    path = resolve(args.get("path"), for_write=True)
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("invalid_input", "content must be a string")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError("too_large", f"content is over the {MAX_WRITE_BYTES} byte limit")
    if os.path.isdir(path):
        raise ToolError("is_a_directory", f"{args.get('path')!r} is a directory")
    created = not os.path.exists(path)
    atomic_write(path, data)
    return {"path": rel(path), "bytes": len(data), "created": created}


def op_edit_file(args: dict) -> dict:
    path = resolve(args.get("path"), for_write=True)
    old, new = args.get("old_string"), args.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or old == "":
        raise ToolError("invalid_input", "old_string (non-empty) and new_string are required")
    if old == new:
        raise ToolError("invalid_input", "old_string and new_string are identical")
    if not os.path.isfile(path):
        raise ToolError("not_found", f"{args.get('path')!r} is not a file")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    count = text.count(old)
    if count == 0:
        raise ToolError("no_match", "old_string was not found; read the file and copy it exactly")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(
            "ambiguous_match",
            f"old_string occurs {count} times; add surrounding lines or set replace_all",
        )
    updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
    data = updated.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError("too_large", f"result is over the {MAX_WRITE_BYTES} byte limit")
    atomic_write(path, data)
    return {"path": rel(path), "replacements": count if args.get("replace_all") else 1}


def op_delete_file(args: dict) -> dict:
    path = resolve(args.get("path"), for_write=True)
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
        return {"path": rel(path), "deleted": "file"}
    if os.path.isdir(path):
        shutil.rmtree(path)
        return {"path": rel(path), "deleted": "directory"}
    raise ToolError("not_found", f"{args.get('path')!r} does not exist")


def op_search_code(args: dict) -> dict:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        raise ToolError("invalid_input", "pattern is required")
    try:
        regex = re.compile(pattern if args.get("regex") else re.escape(pattern))
    except re.error as exc:
        raise ToolError("invalid_input", f"bad regular expression: {exc}") from exc
    start = resolve(args.get("path", "."))
    glob = args.get("glob")
    limit = min(int(args.get("max_results", 50)), 200)
    matches: list[dict] = []
    truncated = False
    files = (
        [(os.path.dirname(start), [], [os.path.basename(start)])]
        if os.path.isfile(start)
        else walk(start)
    )
    for dirpath, _dirnames, filenames in files:
        for name in filenames:
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.islink(full) or os.path.getsize(full) > MAX_SEARCH_FILE_BYTES:
                    continue
            except OSError:
                continue
            if is_binary(full):
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                for number, line in enumerate(fh, start=1):
                    if regex.search(line):
                        matches.append(
                            {"path": rel(full), "line": number, "text": line.rstrip()[:300]}
                        )
                        if len(matches) >= limit:
                            truncated = True
                            break
            if truncated:
                break
        if truncated:
            break
    return {"matches": matches, "truncated": truncated}


def op_stat(_args: dict) -> dict:
    """Workspace size, including dependency trees: this is what the disk quota counts."""
    total = 0
    count = 0
    for dirpath, _dirnames, filenames in os.walk(ROOT, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
                count += 1
            except OSError:
                continue
    return {"bytes": total, "files": count}


OPS = {
    "list_files": op_list_files,
    "read_file": op_read_file,
    "write_file": op_write_file,
    "edit_file": op_edit_file,
    "delete_file": op_delete_file,
    "search_code": op_search_code,
    "stat": op_stat,
}


def unpack() -> dict:
    """Extract a tar stream from stdin into the workspace, refusing anything but plain files
    and directories that stay inside it."""
    total = 0
    count = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
        for member in archive:
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if name in ("", "."):
                continue
            if not (member.isfile() or member.isdir()):
                raise ToolError("unsafe_archive", f"{name!r}: links and devices are not allowed")
            target = resolve(name)
            top = os.path.relpath(target, ROOT).split(os.sep)[0]
            if top in PROTECTED_TOP_LEVEL:
                continue  # an uploaded .git directory is dropped: its hooks would be untrusted
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            count += 1
            total += member.size
            if count > MAX_UNPACK_FILES or total > MAX_UNPACK_BYTES:
                raise ToolError("archive_too_large", "archive exceeds the file or size limit")
            source = archive.extractfile(member)
            if source is None:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as out:
                shutil.copyfileobj(source, out)
            os.chmod(target, 0o755 if member.mode & 0o100 else 0o644)
    return {"files": count, "bytes": total}


def pack() -> None:
    """Write a zip of the workspace to stdout, without version control or dependency trees."""
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, _dirnames, filenames in walk(ROOT):
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                total += os.path.getsize(full)
                if total > MAX_PACK_BYTES:
                    raise ToolError("workspace_too_large", "workspace is too large to export")
                archive.write(full, rel(full))
    sys.stdout.buffer.write(buffer.getvalue())


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "request"
    try:
        if mode == "pack":
            pack()
            return 0
        if mode == "unpack":
            result = unpack()
        else:
            request = json.loads(sys.stdin.read() or "{}")
            handler = OPS.get(request.get("op"))
            if handler is None:
                raise ToolError("unknown_op", f"unknown operation {request.get('op')!r}")
            result = handler(request.get("args") or {})
    except ToolError as exc:
        reply = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        if mode == "pack":
            sys.stderr.write(json.dumps(reply))
            return 2
        sys.stdout.write(json.dumps(reply))
        return 0
    except (OSError, ValueError, tarfile.TarError) as exc:
        reply = {"ok": False, "error": {"code": "io_error", "message": str(exc)[:500]}}
        if mode == "pack":
            sys.stderr.write(json.dumps(reply))
            return 2
        sys.stdout.write(json.dumps(reply))
        return 0
    sys.stdout.write(json.dumps({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
