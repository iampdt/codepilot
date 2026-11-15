"""Codebase archives: validating an upload, converting it for the sandbox, and reading it.

The planner inspects a repository before the user has approved anything. It does that against
the archive, through `ArchiveWorkspace`, so no container exists and no project code can run
until a plan is approved.
"""

from __future__ import annotations

import fnmatch
import io
import re
import tarfile
import zipfile
from pathlib import PurePosixPath
from typing import Any

from app.sandbox.base import SandboxToolError

MAX_FILES = 5_000
MAX_TOTAL_BYTES = 50_000_000
MAX_RATIO = 200  # uncompressed/compressed above this is treated as a zip bomb
MAX_READ_BYTES = 200_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}


class ArchiveError(ValueError):
    pass


def _safe_name(name: str) -> str | None:
    """A normalised relative path, or None for an entry to skip. Raises on a traversal."""
    if "\x00" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ArchiveError(f"unsafe path in archive: {name!r}")
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".")]
    if ".." in parts:
        raise ArchiveError(f"unsafe path in archive: {name!r}")
    if not parts or any(p in SKIP_DIRS for p in parts) or parts[0] == "__MACOSX":
        return None
    return "/".join(parts)


def validate_zip(data: bytes) -> dict[str, int]:
    """Check an uploaded zip without extracting it. Raises ArchiveError if it is unsafe."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError("not a valid zip file") from exc
    files = 0
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        # The upper 16 bits of external_attr hold the Unix mode; 0o120000 marks a symlink.
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ArchiveError(f"symbolic links are not allowed: {info.filename!r}")
        if _safe_name(info.filename) is None:
            continue
        files += 1
        total += info.file_size
        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
            raise ArchiveError(f"suspicious compression ratio: {info.filename!r}")
        if files > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ArchiveError("archive has too many files or is too large when extracted")
    return {"files": files, "bytes": total}


def _strip_common_root(names: list[str]) -> str:
    """`project/…` as the only top-level entry is a wrapper folder; return it to strip."""
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        return next(iter(tops)) + "/"
    return ""


def read_zip(data: bytes) -> dict[str, bytes]:
    """All files of a validated zip, as {relative path: bytes}."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    entries: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = _safe_name(info.filename)
        if name is not None:
            entries[name] = info
    prefix = _strip_common_root(list(entries))
    return {name[len(prefix) :]: archive.read(info) for name, info in entries.items()}


def zip_to_tar(data: bytes) -> bytes:
    """Re-pack a zip as the tar stream the sandbox's file agent extracts."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in sorted(read_zip(data).items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def empty_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    return buffer.getvalue()


class ArchiveWorkspace:
    """Read-only file tools over an archive held in memory."""

    def __init__(self, data: bytes | None) -> None:
        self.files = read_zip(data) if data else {}

    async def file_op(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = {
            "list_files": self._list,
            "read_file": self._read,
            "search_code": self._search,
        }.get(op)
        if handler is None:
            raise SandboxToolError(
                "read_only", f"{op} is not available before the plan is approved"
            )
        return handler(args)

    @staticmethod
    def _clean(path: str) -> str:
        if path.startswith("/") or ".." in path.split("/"):
            raise SandboxToolError("path_outside_workspace", f"{path!r} is outside the workspace")
        parts = [p for p in path.split("/") if p not in ("", ".")]
        return "/".join(parts)

    def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        prefix = self._clean(str(args.get("path", ".")))
        prefix = prefix + "/" if prefix else ""
        limit = min(int(args.get("max_entries", 500)), 5000)
        entries = [
            {"path": name, "type": "file", "size": len(content)}
            for name, content in sorted(self.files.items())
            if name.startswith(prefix)
        ]
        return {"entries": entries[:limit], "truncated": len(entries) > limit}

    def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._clean(str(args.get("path", "")))
        if path not in self.files:
            raise SandboxToolError("not_found", f"{path!r} is not a file")
        raw = self.files[path]
        if b"\x00" in raw[:8192]:
            raise SandboxToolError("binary_file", "file looks binary and can't be read as text")
        lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
        start = max(int(args.get("start_line") or 1), 1)
        end = min(int(args.get("end_line") or len(lines)), len(lines))
        content = "".join(lines[start - 1 : end])
        return {
            "path": path,
            "content": content[:MAX_READ_BYTES],
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "truncated": len(content) > MAX_READ_BYTES,
        }

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = str(args.get("pattern", ""))
        if not pattern:
            raise SandboxToolError("invalid_input", "pattern is required")
        try:
            regex = re.compile(pattern if args.get("regex") else re.escape(pattern))
        except re.error as exc:
            raise SandboxToolError("invalid_input", f"bad regular expression: {exc}") from exc
        glob = args.get("glob")
        limit = min(int(args.get("max_results", 50)), 200)
        matches: list[dict[str, Any]] = []
        for name, raw in sorted(self.files.items()):
            if glob and not fnmatch.fnmatch(name.rsplit("/", 1)[-1], glob):
                continue
            if b"\x00" in raw[:8192] or len(raw) > 1_000_000:
                continue
            for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    matches.append({"path": name, "line": number, "text": line[:300]})
                    if len(matches) >= limit:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}
