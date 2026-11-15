"""Command policy: what an agent may ask the sandbox to run.

This is defence in depth, not the security boundary. The container is the boundary: a command
that slips past this check still runs as an unprivileged user, with no capabilities, no
network and a read-only root filesystem. The policy exists to keep the agent on task (build,
run and test the project) and to refuse the obviously wrong before it costs a container exec.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

MAX_COMMAND_LENGTH = 2000

# Programs an agent may run. Anything else is refused with a message naming the list.
ALLOWED_PROGRAMS = frozenset(
    {
        # python
        "python", "python3", "pip", "pip3", "pytest", "uvicorn", "alembic", "ruff", "mypy",
        "black", "flake8", "coverage", "django-admin", "flask",
        # node
        "node", "npm", "npx", "tsc", "jest", "vitest",
        # inspection and small file utilities
        "ls", "cat", "head", "tail", "wc", "find", "grep", "rg", "echo", "printf", "pwd", "cd",
        "mkdir", "touch", "cp", "mv", "rm", "sed", "awk", "sort", "uniq", "diff", "cut", "tr",
        "xargs", "env", "export", "true", "false", "test", "[", "sleep", "kill", "wait",
        "timeout", "which", "basename", "dirname", "date", "git",
    }
)  # fmt: skip

# git is for looking, not for rewriting history: checkpoints go through their own tools.
ALLOWED_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "ls-files", "blame"})

# Dependency installs go through install_dependency, which is the only tool given registry
# access. Refusing them here gives the model a useful message instead of a network timeout.
INSTALL_PATTERNS = (
    re.compile(r"^(pip|pip3)\s+install\b"),
    re.compile(r"^python3?\s+-m\s+pip\s+install\b"),
    re.compile(r"^npm\s+(install|i|ci|add)\b"),
)

FORBIDDEN_SUBSTRINGS = ("$(", "`", "<(", ">(", "\x00")

# What counts as running tests, for the tools and for the orchestrator's own verification.
TEST_COMMAND_PATTERNS = (
    re.compile(r"^(python3?\s+-m\s+)?pytest\b"),
    re.compile(r"^python3?\s+-m\s+unittest\b"),
    re.compile(r"^npm\s+(run\s+)?test\b"),
    re.compile(r"^npx\s+(vitest|jest)\b"),
    re.compile(r"^node\s+--test\b"),
    re.compile(r"^(vitest|jest)\b"),
)

_OPERATORS = {"&&", "||", ";", "|", "&"}
_REDIRECTS = {">", ">>", "<", "2>", "2>>", "&>", "2>&1", ">&2", "1>&2"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


def _deny(reason: str) -> PolicyDecision:
    return PolicyDecision(False, reason)


def _split(command: str) -> list[list[str]] | None:
    """Split a shell line into simple commands on control operators. None if it can't be parsed."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    simple: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPERATORS:
            simple.append([])
        else:
            simple[-1].append(token)
    return [s for s in simple if s]


def _path_escapes(token: str) -> bool:
    if token.startswith("/"):
        return not (
            token == "/workspace"
            or token.startswith(("/workspace/", "/tmp/", "/dev/null", "/home/sandbox/"))
        )
    return ".." in token.split("/")


def check_command(command: str, *, allow_install: bool = False) -> PolicyDecision:
    command = command.strip()
    if not command:
        return _deny("command is empty")
    if len(command) > MAX_COMMAND_LENGTH:
        return _deny(f"command is longer than {MAX_COMMAND_LENGTH} characters")
    if "\n" in command or "\r" in command:
        return _deny("multi-line commands are not allowed; run one command per call")
    for needle in FORBIDDEN_SUBSTRINGS:
        if needle in command:
            return _deny("command or process substitution is not allowed")
    simple_commands = _split(command)
    if simple_commands is None:
        return _deny("command could not be parsed (unbalanced quotes?)")
    if not simple_commands:
        return _deny("command is empty")

    for tokens in simple_commands:
        words = list(tokens)
        while words and _ASSIGNMENT.match(words[0]):
            words.pop(0)
        if not words:
            continue
        program = words[0]
        if program.startswith("./"):
            if _path_escapes(program):
                return _deny(f"{program!r} points outside the workspace")
        elif program not in ALLOWED_PROGRAMS:
            return _deny(
                f"{program!r} is not an allowed program. Allowed: python, pip (via "
                "install_dependency), pytest, node, npm, npx and basic file utilities"
            )
        if program == "git" and (len(words) < 2 or words[1] not in ALLOWED_GIT_SUBCOMMANDS):
            return _deny(
                "only read-only git commands are allowed (status, diff, log, show); "
                "use create_checkpoint and restore_checkpoint to save or roll back work"
            )
        joined = " ".join(words)
        if not allow_install and any(p.match(joined) for p in INSTALL_PATTERNS):
            return _deny("install packages with the install_dependency tool, not a shell command")
        # Redirect targets and plain path arguments must stay inside the sandbox's writable areas.
        for index, word in enumerate(words[1:], start=1):
            previous = words[index - 1]
            if previous in _REDIRECTS and _path_escapes(word):
                return _deny(f"redirect target {word!r} is outside the workspace")
            if program in {"rm", "mv", "cp", "touch", "mkdir", "sed"} and _path_escapes(word):
                return _deny(f"{program} may only touch paths inside the workspace, not {word!r}")
            if program in {"rm", "mv", "sed"} and (word == ".git" or word.startswith(".git/")):
                return _deny(".git is managed by CodePilot and can't be modified")
    return PolicyDecision(True)


def is_test_command(command: str) -> bool:
    """True if every part of the command is a test run (optionally after `cd <dir>`)."""
    parts = _split(command.strip())
    if not parts:
        return False
    runs = 0
    for tokens in parts:
        words = list(tokens)
        while words and _ASSIGNMENT.match(words[0]):
            words.pop(0)
        if not words or words[0] == "cd":
            continue
        if not any(p.match(" ".join(words)) for p in TEST_COMMAND_PATTERNS):
            return False
        runs += 1
    return runs > 0


_PACKAGE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._\-/\[\],<>=!~^@]*$")


def check_package_spec(spec: str) -> PolicyDecision:
    """A dependency name with an optional version, and nothing that pip or npm reads as an
    option, a URL or a local path."""
    if not spec or len(spec) > 120:
        return _deny("package name is empty or too long")
    if spec.startswith("-") or "://" in spec or spec.startswith((".", "/")) or " " in spec:
        return _deny(f"{spec!r}: only registry package names are allowed (no options, URLs, paths)")
    if not _PACKAGE.match(spec):
        return _deny(f"{spec!r} is not a valid package name")
    return PolicyDecision(True)
