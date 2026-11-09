from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from paicli.policy import CommandGuard
from paicli.policy.command_guard import CommandPolicyError
from paicli.tools import get_builtin_tools
from paicli.tools.base import Tool, ToolContext, ToolResult, object_schema

MAX_OUTPUT_CHARS = 20_000
# _run_agent caps a whole run at 1200s, so no single command may outlive it.
MAX_COMMAND_TIMEOUT = 1_200.0
_WALL_SLACK = 60.0

CONTAINER_ROOT = "/app"
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
WRAPPERS = {"sudo", "doas", "env", "command", "nohup", "time", "timeout", "nice", "stdbuf", "exec"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
NUMERIC = re.compile(r"[\d.]+[smhd]?")

FILE_TOOL_NAMES = (
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "glob",
    "glob_files",
    "grep",
    "grep_code",
    "directory_tree",
    "get_file_info",
)
_GLOB_TOOLS = frozenset({"glob", "glob_files"})
GIT_READ_ONLY_REMOTE = frozenset({"show", "get-url"})

UNREACHABLE_HINT = (
    "The task container has no network and no copy of the upstream repository, so hidden "
    "tests, the gold patch and future commits are unreachable. Do not search outside /app; "
    "fix the issue with the code that is there and finish."
)
HUNTING_HINT_AFTER = 3
NETWORK_EXECUTABLES = frozenset({"curl", "wget", "nc", "ncat", "ping", "ssh", "scp", "sftp"})
HUNTING_PIP_SUBCOMMANDS = frozenset({"cache", "show", "index", "download", "list"})
HUNTING_GIT_SUBCOMMANDS = frozenset({"cat-file", "fsck", "show-ref", "count-objects", "ls-remote"})
HUNTING_GIT_HISTORY = frozenset({"log", "rev-list", "branch", "tag"})
OUTSIDE_READERS = frozenset({"find", "grep", "rg", "ls", "cat"})


def _segments(command: str) -> list[str]:
    return [segment for segment in SEGMENT_SPLIT.split(command) if segment.strip()]


def _unwrap(segment: str) -> list[str]:
    """Strip variable assignments and wrapper commands from a shell segment."""
    try:
        tokens = shlex.split(segment, comments=False)
    except ValueError:
        tokens = segment.split()
    while tokens:
        if ASSIGNMENT.fullmatch(tokens[0]):
            tokens = tokens[1:]
            continue
        if tokens[0] in WRAPPERS:
            tokens = tokens[1:]
            while tokens and (tokens[0].startswith("-") or NUMERIC.fullmatch(tokens[0])):
                tokens = tokens[1:]
            continue
        break
    return tokens


def _blacklist_rules(blacklist: list[str]) -> tuple[set[str], set[str], set[str], set[str]]:
    """Turn `cmd` / `git sub` style entries into executable-level rules.

    Entries are matched against the command word, not the whole command line:
    `grep ssh.py` must stay legal while `ssh host` stays blocked. URL entries are
    dropped because the task container already runs with --network=none.
    """
    executables: set[str] = set()
    git_subcommands: set[str] = set()
    pip_install: set[str] = set()
    uv_subcommands: set[str] = set()
    for entry in blacklist:
        parts = entry.split()
        if not parts or "://" in entry:
            continue
        if len(parts) == 1:
            executables.add(parts[0])
        elif parts[0] == "git":
            git_subcommands.add(parts[1])
        elif parts[0] in {"pip", "pip3"}:
            pip_install.update({"pip", "pip3"} if parts[1] == "install" else set())
        elif parts[0] == "uv":
            uv_subcommands.add(parts[1])
    return executables, git_subcommands, pip_install, uv_subcommands


def _violation(command: str, blacklist: list[str]) -> str | None:
    executables, git_subcommands, pip_install, uv_subcommands = _blacklist_rules(blacklist)
    for segment in _segments(command):
        tokens = _unwrap(segment)
        if not tokens:
            continue
        head = tokens[0]
        if head in executables:
            return head
        if head == "git":
            violation = _git_violation(tokens, git_subcommands)
            if violation:
                return violation
        if head in pip_install and "install" in tokens[1:]:
            return "pip install"
        if (
            head.startswith("python")
            and "pip" in tokens[1:]
            and tokens[tokens.index("pip") + 1 : tokens.index("pip") + 2] == ["install"]
        ):
            return "pip install"
        if head == "uv" and len(tokens) > 1 and tokens[1] in uv_subcommands:
            return f"uv {tokens[1]}"
    return None


def _git_violation(tokens: list[str], git_subcommands: set[str]) -> str | None:
    """Match the git subcommand, skipping global flags such as -C <dir> and -c k=v."""
    index = 1
    while index + 1 < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in {"-C", "-c"} else 1
    if index >= len(tokens) or tokens[index] not in git_subcommands:
        return None
    subcommand = tokens[index]
    if subcommand != "remote":
        return f"git {subcommand}"
    action = next((token for token in tokens[index + 1 :] if not token.startswith("-")), None)
    if action is None or action in GIT_READ_ONLY_REMOTE:
        return None
    return f"git remote {action}"


def _validate_command(command: str, blacklist: list[str]) -> None:
    CommandGuard().validate(command)
    violation = _violation(command, blacklist)
    if violation:
        raise CommandPolicyError(f"command rejected by policy: {violation}")


def _outside_app(token: str) -> bool:
    """True for absolute paths that leave the bind-mounted repository."""
    if not token.startswith("/"):
        return False
    normalized = str(PurePosixPath(token))
    return normalized != CONTAINER_ROOT and not normalized.startswith(CONTAINER_ROOT + "/")


def _git_hunting(tokens: list[str]) -> str | None:
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in {"-C", "-c"} else 1
    if index >= len(tokens):
        return None
    subcommand = tokens[index]
    if subcommand in HUNTING_GIT_SUBCOMMANDS:
        return f"git {subcommand}"
    if subcommand in HUNTING_GIT_HISTORY:
        rest = tokens[index + 1 :]
        if any(token in {"--all", "-a"} for token in rest):
            return f"git {subcommand} --all"
    return None


def _hunting_kind(command: str) -> str | None:
    """Label commands that look outside /app for the hidden tests or the fix.

    These rejections used to read `command rejected by policy: curl`, which invites
    the model to retry with a different spelling. The label feeds a per-instance
    counter so repeated attempts can be answered with an explicit stop hint.
    """
    for segment in _segments(command):
        tokens = _unwrap(segment)
        if not tokens:
            continue
        head = tokens[0]
        if head in NETWORK_EXECUTABLES:
            return head
        if head == "git":
            kind = _git_hunting(tokens)
            if kind:
                return kind
        if head in {"pip", "pip3"} and len(tokens) > 1 and tokens[1] in HUNTING_PIP_SUBCOMMANDS:
            return f"pip {tokens[1]}"
        if head in OUTSIDE_READERS and any(_outside_app(token) for token in tokens[1:]):
            return f"{head} outside /app"
    return None


def _hunting_note(attempts: int) -> str:
    if attempts < HUNTING_HINT_AFTER:
        return ""
    return (
        f"\n\nHidden-test hunting attempt {attempts}: nothing outside /app contains the "
        "hidden tests or the fix, so this search cannot succeed. Finish the fix with the "
        "code in /app."
    )


def container_path(value: str) -> str:
    """Map an in-container absolute path onto the bind-mounted workspace."""
    if value == CONTAINER_ROOT:
        return "."
    prefix = CONTAINER_ROOT + "/"
    if value.startswith(prefix):
        return str(PurePosixPath(value[len(prefix) :]))
    return value


def create_file_tool_overrides() -> dict[str, Tool]:
    """Accept /app/... paths in file tools, which resolve against the same bind mount.

    bash sees the repository at /app, but the file tools resolve paths against
    the host working directory. Without this the agent's absolute paths are
    rejected as escaping the workspace even though they name the same files.
    """

    def wrapped(tool: Tool) -> Tool:
        key = "pattern" if tool.name in _GLOB_TOOLS else "path"

        async def handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
            normalized = dict(payload)
            value = normalized.get(key)
            if isinstance(value, str):
                normalized[key] = container_path(value)
            return await tool.handler(normalized, context)

        return replace(tool, handler=handler)

    return {
        tool.name: wrapped(tool) for tool in get_builtin_tools() if tool.name in FILE_TOOL_NAMES
    }


def create_container_bash_tools(container_name: str) -> dict[str, Tool]:
    """Return bash aliases that execute inside a running task container."""
    hunting_attempts = 0

    async def _container_bash(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        nonlocal hunting_attempts
        command = str(payload["command"])
        hunting = _hunting_kind(command)
        if hunting:
            hunting_attempts += 1
        if context.config.policy.command_guard_enabled:
            try:
                _validate_command(command, context.config.policy.command_blacklist)
            except CommandPolicyError as exc:
                attempts = hunting_attempts if hunting else 0
                raise CommandPolicyError(
                    f"{exc}\n\n{UNREACHABLE_HINT}{_hunting_note(attempts)}"
                ) from None
        timeout = min(
            float(payload.get("timeout") or context.config.tools.timeout), MAX_COMMAND_TIMEOUT
        )
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            container_name,
            "bash",
            "-lc",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(f"Command timed out after {timeout:.0f}s", is_error=True)
        output = (stdout + stderr).decode("utf-8", errors="replace")
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
        if hunting:
            output += _hunting_note(hunting_attempts)
        return ToolResult(
            output or f"(exit {proc.returncode}, no output)",
            is_error=proc.returncode != 0,
        )

    parameters = object_schema(
        {
            "command": {"type": "string", "description": "Shell command"},
            "timeout": {"type": "number", "description": "Timeout seconds"},
        },
        ["command"],
    )
    return {
        "bash": Tool(
            name="bash",
            description="Execute a shell command inside the task container.",
            parameters=parameters,
            required_keys=["command"],
            handler=_container_bash,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            timeout=MAX_COMMAND_TIMEOUT + _WALL_SLACK,
        ),
        "execute_command": Tool(
            name="execute_command",
            description="Alias of bash. Execute a shell command inside the task container.",
            parameters=parameters,
            required_keys=["command"],
            handler=_container_bash,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            timeout=MAX_COMMAND_TIMEOUT + _WALL_SLACK,
        ),
    }


def create_container_tools(container_name: str) -> dict[str, Tool]:
    """Everything the agent needs to work on the bind-mounted repository."""
    return {**create_file_tool_overrides(), **create_container_bash_tools(container_name)}
