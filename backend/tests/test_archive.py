import io
import stat
import tarfile
import zipfile

import pytest

from app.sandbox.archive import ArchiveError, ArchiveWorkspace, read_zip, validate_zip, zip_to_tar
from app.sandbox.base import SandboxToolError


def make_zip(files: dict[str, bytes | str], symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def test_valid_zip_is_counted() -> None:
    assert validate_zip(make_zip({"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})) == {
        "files": 2,
        "bytes": 12,
    }


@pytest.mark.parametrize("name", ["../evil.py", "a/../../evil.py", "/etc/passwd", "C:\\x.txt"])
def test_traversal_names_are_refused(name: str) -> None:
    with pytest.raises(ArchiveError):
        validate_zip(make_zip({name: "x"}))


def test_symlinks_are_refused() -> None:
    with pytest.raises(ArchiveError, match="symbolic"):
        validate_zip(make_zip({"a.py": "x"}, symlink="link"))


def test_zip_bomb_is_refused() -> None:
    with pytest.raises(ArchiveError, match="ratio"):
        validate_zip(make_zip({"bomb.txt": b"0" * 5_000_000}))


def test_not_a_zip_is_refused() -> None:
    with pytest.raises(ArchiveError):
        validate_zip(b"definitely not a zip")


def test_git_and_dependency_folders_are_dropped_and_wrapper_folder_stripped() -> None:
    data = make_zip(
        {
            "proj/app.py": "print(1)\n",
            "proj/.git/hooks/pre-commit": "#!/bin/sh\ncurl evil\n",
            "proj/node_modules/x/index.js": "x",
            "proj/.env.example": "A=1\n",
        }
    )
    assert sorted(read_zip(data)) == [".env.example", "app.py"]


def test_tar_conversion_keeps_paths_and_content() -> None:
    tar = tarfile.open(fileobj=io.BytesIO(zip_to_tar(make_zip({"a/b.py": "hello", "c.py": "x"}))))
    assert tar.getnames() == ["a/b.py", "c.py"]
    assert tar.extractfile("a/b.py").read() == b"hello"  # type: ignore[union-attr]


async def test_archive_workspace_reads_and_searches_but_never_writes() -> None:
    workspace = ArchiveWorkspace(
        make_zip({"src/app.py": "def main():\n    return 42\n", ".env": "A=1"})
    )
    listing = await workspace.file_op("list_files", {"path": "."})
    assert [e["path"] for e in listing["entries"]] == [".env", "src/app.py"]
    assert (await workspace.file_op("read_file", {"path": "src/app.py"}))["total_lines"] == 2
    found = await workspace.file_op("search_code", {"pattern": "return"})
    assert found["matches"][0]["line"] == 2
    with pytest.raises(SandboxToolError, match="outside"):
        await workspace.file_op("read_file", {"path": "../x"})
    with pytest.raises(SandboxToolError) as refused:
        await workspace.file_op("write_file", {"path": "x", "content": "y"})
    assert refused.value.code == "read_only"
