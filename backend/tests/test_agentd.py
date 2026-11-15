"""The file agent that runs inside the sandbox, exercised directly against a directory."""

import io
import os
import tarfile
import zipfile
from collections.abc import AsyncIterator

import pytest

from app.sandbox.base import SandboxToolError
from tests.support import LocalSandbox


@pytest.fixture
async def box() -> AsyncIterator[LocalSandbox]:
    sandbox = LocalSandbox("agentd")
    yield sandbox
    await sandbox.destroy()


async def test_write_read_edit_delete_round_trip(box: LocalSandbox) -> None:
    created = await box.file_op("write_file", {"path": "pkg/mod.py", "content": "a = 1\nb = 2\n"})
    assert created == {"path": "pkg/mod.py", "bytes": 12, "created": True}
    await box.file_op(
        "edit_file", {"path": "pkg/mod.py", "old_string": "a = 1", "new_string": "a = 10"}
    )
    read = await box.file_op("read_file", {"path": "pkg/mod.py", "start_line": 1, "end_line": 1})
    assert read["content"] == "a = 10\n" and read["total_lines"] == 2
    await box.file_op("delete_file", {"path": "pkg"})
    assert (await box.file_op("list_files", {}))["entries"] == []


@pytest.mark.parametrize(
    "path", ["../outside.txt", "a/../../outside.txt", "/etc/passwd", "", "a\x00b"]
)
async def test_paths_outside_the_workspace_are_refused(box: LocalSandbox, path: str) -> None:
    for op, args in [
        ("read_file", {"path": path}),
        ("write_file", {"path": path, "content": "x"}),
        ("delete_file", {"path": path}),
    ]:
        with pytest.raises(SandboxToolError) as refused:
            await box.file_op(op, args)
        assert refused.value.code in {"path_outside_workspace", "invalid_path"}


async def test_symlink_out_of_the_workspace_is_refused(box: LocalSandbox) -> None:
    os.symlink("/etc", box.root / "escape")
    with pytest.raises(SandboxToolError) as refused:
        await box.file_op("read_file", {"path": "escape/hosts"})
    assert refused.value.code == "path_outside_workspace"
    with pytest.raises(SandboxToolError):
        await box.file_op("write_file", {"path": "escape/x", "content": "x"})


async def test_git_metadata_is_read_only(box: LocalSandbox) -> None:
    for path in [".git/hooks/pre-commit", ".git/config", ".git"]:
        with pytest.raises(SandboxToolError) as refused:
            await box.file_op("write_file", {"path": path, "content": "x"})
        assert refused.value.code in {"protected_path", "is_a_directory"}
    with pytest.raises(SandboxToolError):
        await box.file_op("delete_file", {"path": "."})


async def test_edit_requires_one_exact_match(box: LocalSandbox) -> None:
    await box.file_op("write_file", {"path": "f.py", "content": "x = 1\nx = 1\n"})
    with pytest.raises(SandboxToolError) as ambiguous:
        await box.file_op(
            "edit_file", {"path": "f.py", "old_string": "x = 1", "new_string": "x = 2"}
        )
    assert ambiguous.value.code == "ambiguous_match"
    with pytest.raises(SandboxToolError) as missing:
        await box.file_op("edit_file", {"path": "f.py", "old_string": "nope", "new_string": "y"})
    assert missing.value.code == "no_match"
    result = await box.file_op(
        "edit_file",
        {"path": "f.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True},
    )
    assert result["replacements"] == 2


async def test_oversized_write_and_binary_read_are_refused(box: LocalSandbox) -> None:
    with pytest.raises(SandboxToolError) as large:
        await box.file_op("write_file", {"path": "big.txt", "content": "x" * 1_000_001})
    assert large.value.code == "too_large"
    (box.root / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(SandboxToolError) as binary:
        await box.file_op("read_file", {"path": "blob.bin"})
    assert binary.value.code == "binary_file"


async def test_search_and_listing_skip_dependency_folders(box: LocalSandbox) -> None:
    await box.file_op("write_file", {"path": "src/a.py", "content": "needle = 1\n"})
    await box.file_op("write_file", {"path": "node_modules/x/a.js", "content": "needle\n"})
    found = await box.file_op("search_code", {"pattern": "needle"})
    assert [m["path"] for m in found["matches"]] == ["src/a.py"]
    listed = [e["path"] for e in (await box.file_op("list_files", {}))["entries"]]
    assert listed == ["src/", "src/a.py"]


def _tar(members: list[tarfile.TarInfo], contents: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for member in members:
            data = contents.get(member.name, b"")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


async def test_unpack_extracts_files_and_drops_uploaded_git_folder(box: LocalSandbox) -> None:
    members = [tarfile.TarInfo("./app/main.py"), tarfile.TarInfo(".gitignore"),
               tarfile.TarInfo(".git/hooks/pre-commit")]  # fmt: skip
    data = _tar(members, {"./app/main.py": b"print(1)\n", ".gitignore": b"*.pyc\n",
                          ".git/hooks/pre-commit": b"evil"})  # fmt: skip
    assert (await box.unpack(data))["files"] == 2
    assert (box.root / "app/main.py").read_text() == "print(1)\n"
    assert (box.root / ".gitignore").exists()
    assert not (box.root / ".git").exists()


@pytest.mark.parametrize("kind", ["symlink", "traversal", "absolute"])
async def test_unpack_refuses_links_and_escapes(box: LocalSandbox, kind: str) -> None:
    member = tarfile.TarInfo("link" if kind == "symlink" else
                             "../escape.txt" if kind == "traversal" else "/tmp/abs.txt")  # fmt: skip
    if kind == "symlink":
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
    import json

    reply = json.loads(await box._agentd("unpack", stdin=_tar([member], {member.name: b"x"})))
    assert reply["ok"] is False
    assert not (box.root.parent / "escape.txt").exists()


async def test_pack_leaves_out_git_and_dependencies(box: LocalSandbox) -> None:
    await box.file_op("write_file", {"path": "a.py", "content": "x"})
    (box.root / ".git").mkdir()
    (box.root / ".git/config").write_text("x")
    (box.root / "node_modules").mkdir()
    (box.root / "node_modules/m.js").write_text("x")
    names = zipfile.ZipFile(io.BytesIO(await box.pack())).namelist()
    assert names == ["a.py"]
