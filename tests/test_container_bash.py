from __future__ import annotations

import asyncio

import pytest

from eval.container_bash import (
    MAX_COMMAND_TIMEOUT,
    create_container_bash_tools,
    create_container_tools,
)
from eval.swebench_pro_runner import SWEBENCH_BLOCKED_COMMANDS
from paicli.config import load_config
from paicli.policy.command_guard import CommandPolicyError
from paicli.policy.path_guard import PathPolicyError
from paicli.tools.base import ToolContext


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        delay: float = 0.0,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.delay = delay
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _context(tmp_path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path), config=load_config(project_root=tmp_path))


def _install_process(monkeypatch, process: _FakeProcess) -> dict:
    record: dict = {}

    async def fake_exec(*args, **kwargs):
        record["args"] = args
        record["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return record


def _run(payload: dict, context: ToolContext):
    tools = create_container_bash_tools("paicli-bash-test")
    return asyncio.run(tools["bash"].handler(payload, context))


def test_create_container_bash_tools_exposes_bash_and_alias(tmp_path):
    tools = create_container_bash_tools("paicli-bash-test")

    assert set(tools) == {"bash", "execute_command"}
    assert tools["bash"].name == "bash"
    assert tools["execute_command"].name == "execute_command"
    assert tools["bash"].handler is tools["execute_command"].handler
    for tool in tools.values():
        assert tool.danger_level == "high"
        assert tool.requires_approval is True
        assert tool.is_read_only is False
        assert tool.is_concurrency_safe is False
        assert tool.parameters["required"] == ["command"]
        assert "timeout" in tool.parameters["properties"]


def test_container_bash_executes_via_docker_exec(tmp_path, monkeypatch):
    process = _FakeProcess(stdout=b"hello\n")
    record = _install_process(monkeypatch, process)

    result = _run({"command": "echo hello"}, _context(tmp_path))

    assert record["args"] == (
        "docker",
        "exec",
        "-i",
        "paicli-bash-test",
        "bash",
        "-lc",
        "echo hello",
    )
    assert "cwd" not in record["kwargs"]
    assert result.content == "hello\n"
    assert result.is_error is False


def test_container_bash_marks_nonzero_exit_as_error(tmp_path, monkeypatch):
    process = _FakeProcess(stderr=b"boom\n", returncode=2)
    _install_process(monkeypatch, process)

    result = _run({"command": "false"}, _context(tmp_path))

    assert result.is_error is True
    assert "boom" in result.content


def test_container_bash_truncates_long_output(tmp_path, monkeypatch):
    process = _FakeProcess(stdout=b"x" * 25_000)
    _install_process(monkeypatch, process)

    result = _run({"command": "cat big"}, _context(tmp_path))

    assert len(result.content) == 20_000 + len("\n... [truncated]")
    assert result.content.endswith("\n... [truncated]")


def test_container_bash_timeout_kills_process(tmp_path, monkeypatch):
    process = _FakeProcess(delay=1.0)
    _install_process(monkeypatch, process)

    result = _run({"command": "sleep 10", "timeout": 0.01}, _context(tmp_path))

    assert result.is_error is True
    assert "timed out" in result.content.lower()
    assert process.killed is True


def test_container_bash_tool_wall_exceeds_per_call_timeout():
    tools = create_container_bash_tools("paicli-bash-test")

    for tool in tools.values():
        assert tool.timeout > MAX_COMMAND_TIMEOUT


def test_container_bash_clamps_excessive_timeout(tmp_path, monkeypatch):
    process = _FakeProcess(stdout=b"ok\n")
    _install_process(monkeypatch, process)
    seen: dict = {}
    real_wait_for = asyncio.wait_for

    async def spy(awaitable, timeout=None):
        seen["timeout"] = timeout
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy)

    result = _run({"command": "echo ok", "timeout": 99_999}, _context(tmp_path))

    assert result.content == "ok\n"
    assert seen["timeout"] == MAX_COMMAND_TIMEOUT


def test_container_bash_enforces_command_guard(tmp_path, monkeypatch):
    process = _FakeProcess()
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = ["curl"]

    with pytest.raises(CommandPolicyError, match="curl"):
        _run({"command": "curl https://example.com"}, context)


def from_swebench_blacklist() -> list[str]:
    return list(SWEBENCH_BLOCKED_COMMANDS)


def test_guard_allows_paths_that_contain_a_blocked_word(tmp_path, monkeypatch):
    # `grep ... lib/ansible/plugins/connection/ssh.py` is a substring match for
    # "ssh", and rejecting it costs the agent real exploration turns.
    process = _FakeProcess(stdout=b"ok\n")
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    result = _run(
        {"command": 'grep -n "class Connection" lib/ansible/plugins/connection/ssh.py'},
        context,
    )

    assert result.is_error is False
    assert result.content == "ok\n"


def test_guard_blocks_the_executable_behind_a_wrapper(tmp_path, monkeypatch):
    process = _FakeProcess()
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    with pytest.raises(CommandPolicyError, match="curl"):
        _run({"command": "timeout 20 curl -sS https://example.com"}, context)


def test_guard_blocks_wrapped_git_network_subcommands(tmp_path, monkeypatch):
    process = _FakeProcess()
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    with pytest.raises(CommandPolicyError, match="git fetch"):
        _run({"command": "cd /app && GIT_PAGER=cat git fetch --all"}, context)


def test_guard_allows_urls_inside_file_content(tmp_path, monkeypatch):
    # Writing an upstream license header containing https:// is normal work.
    process = _FakeProcess(stdout=b"written\n")
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    result = _run(
        {
            "command": (
                "cat > lib/x.py <<'EOF'\n"
                "# GNU GPL v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)\n"
                "EOF"
            )
        },
        context,
    )

    assert result.is_error is False


def test_guard_blocks_filesystem_wide_hunts(tmp_path, monkeypatch):
    # `find /` is how the agent hunts for a future copy of the code instead of
    # reasoning about the repository; the task container has no other source.
    process = _FakeProcess()
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    with pytest.raises(CommandPolicyError, match="destructive"):
        _run({"command": "find / -maxdepth 3 -name '*.pyc' 2>/dev/null | head"}, context)


def test_guard_still_blocks_destructive_rm(tmp_path, monkeypatch):
    process = _FakeProcess()
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    with pytest.raises(CommandPolicyError, match="destructive"):
        _run({"command": "rm -rf /"}, context)


def test_guard_blocks_pip_install(tmp_path, monkeypatch):
    process = _FakeProcess()
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    with pytest.raises(CommandPolicyError, match="pip install"):
        _run({"command": "python -m pip install requests"}, context)


def _file_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "lib" / "ansible" / "plugins").mkdir(parents=True)
    (repo / "lib" / "ansible" / "plugins" / "winrm.py").write_text(
        "HAS_PEXPECT = False\n", encoding="utf-8"
    )
    return repo


def _file_context(repo) -> ToolContext:
    return ToolContext(cwd=str(repo), config=load_config(project_root=repo))


def _file_tools():
    return create_container_tools("paicli-bash-test")


def test_container_tools_cover_file_tools_and_bash(tmp_path):
    tools = _file_tools()

    assert {"bash", "execute_command", "read_file", "write_file", "edit_file", "grep"} <= set(tools)


def test_read_file_accepts_container_absolute_path(tmp_path):
    repo = _file_repo(tmp_path)
    result = asyncio.run(
        _file_tools()["read_file"].handler(
            {"path": "/app/lib/ansible/plugins/winrm.py"}, _file_context(repo)
        )
    )

    assert result.is_error is False
    assert "HAS_PEXPECT" in result.content


def test_edit_file_accepts_container_absolute_path(tmp_path):
    repo = _file_repo(tmp_path)
    result = asyncio.run(
        _file_tools()["edit_file"].handler(
            {
                "path": "/app/lib/ansible/plugins/winrm.py",
                "old_text": "HAS_PEXPECT = False",
                "new_text": "HAS_PEXPECT = True",
            },
            _file_context(repo),
        )
    )

    assert result.is_error is False
    assert (repo / "lib/ansible/plugins/winrm.py").read_text(encoding="utf-8") == (
        "HAS_PEXPECT = True\n"
    )


def test_container_path_traversal_is_still_rejected(tmp_path):
    # The executor renders raised tool errors as "Tool ... execution error".
    repo = _file_repo(tmp_path)
    with pytest.raises(PathPolicyError, match="escapes workspace"):
        asyncio.run(
            _file_tools()["read_file"].handler({"path": "/app/../secret.txt"}, _file_context(repo))
        )


def test_grep_accepts_container_absolute_directory(tmp_path):
    repo = _file_repo(tmp_path)
    result = asyncio.run(
        _file_tools()["grep"].handler(
            {"pattern": "HAS_PEXPECT", "path": "/app/lib"}, _file_context(repo)
        )
    )

    assert result.is_error is False
    assert "winrm.py" in result.content


def test_guard_allows_read_only_git_remote_queries(tmp_path, monkeypatch):
    process = _FakeProcess(stdout=b"origin\n")
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    commands = ("git remote -v", "cd /app && git remote show origin", "git remote get-url origin")
    for command in commands:
        result = _run({"command": command}, context)
        assert result.is_error is False


def test_guard_blocks_git_remote_mutation(tmp_path, monkeypatch):
    process = _FakeProcess()
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    with pytest.raises(CommandPolicyError, match="git remote add"):
        _run({"command": "git remote add upstream https://example.com/r.git"}, context)


def _tool_run(tools, command: str, context: ToolContext):
    return asyncio.run(tools["bash"].handler({"command": command}, context))


def test_rejection_says_hidden_tests_are_unreachable(tmp_path, monkeypatch):
    process = _FakeProcess()
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    with pytest.raises(CommandPolicyError) as excinfo:
        _run({"command": "curl -sS https://example.com/fix.patch"}, context)

    message = str(excinfo.value)
    assert "no network" in message
    assert "unreachable" in message
    assert "finish" in message


def test_third_hunting_attempt_adds_a_stop_hint(tmp_path, monkeypatch):
    process = _FakeProcess(stdout=b"ok\n")
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()
    tools = create_container_bash_tools("paicli-bash-test")

    first = _tool_run(tools, "git log --all --oneline | head", context)
    second = _tool_run(tools, "pip show ansible-core", context)
    third = _tool_run(tools, "git cat-file -t 8127abbc298cabf04aaa89a478fc5e5e3432a6fc", context)

    assert "hunting attempt" not in first.content
    assert "hunting attempt" not in second.content
    assert "hunting attempt 3" in third.content
    assert third.is_error is False


def test_rejected_hunting_attempts_also_count(tmp_path, monkeypatch):
    process = _FakeProcess()
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()
    tools = create_container_bash_tools("paicli-bash-test")
    payload = {"command": "find / -name '*.patch' 2>/dev/null"}

    for _ in range(2):
        with pytest.raises(CommandPolicyError, match="no network"):
            asyncio.run(tools["bash"].handler(dict(payload), context))
    with pytest.raises(CommandPolicyError, match="hunting attempt 3"):
        asyncio.run(tools["bash"].handler(dict(payload), context))


def test_ssh_filename_is_not_counted_as_hunting(tmp_path, monkeypatch):
    # `ssh.py` is a filename, not a network attempt; counting it would fire the
    # stop hint while the agent is still reading the code it must fix.
    process = _FakeProcess(stdout=b"ok\n")
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()
    tools = create_container_bash_tools("paicli-bash-test")

    for _ in range(4):
        result = _tool_run(
            tools, "grep -n 'class Connection' lib/ansible/plugins/connection/ssh.py", context
        )
        assert "hunting attempt" not in result.content


def test_hunting_counter_does_not_leak_between_instances(tmp_path, monkeypatch):
    process = _FakeProcess(stdout=b"ok\n")
    _install_process(monkeypatch, process)
    context = _context(tmp_path)
    context.config.policy.command_blacklist = from_swebench_blacklist()

    spent = create_container_bash_tools("paicli-bash-test")
    for _ in range(3):
        _tool_run(spent, "git log --all --oneline", context)

    fresh = create_container_bash_tools("paicli-bash-other")
    result = _tool_run(fresh, "git log --all --oneline", context)

    assert "hunting attempt" not in result.content
