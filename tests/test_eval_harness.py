from __future__ import annotations

import os

from eval.runner import build_engine
from eval.swebench_pro_runner import _load_env_file
from eval.trace import trace_from_events
from paicli.tools.base import Tool, ToolResult


def test_trace_from_events_collects_tool_calls_usage_and_compression():
    trace = trace_from_events(
        [
            {"type": "tool_call", "name": "read_file", "input": {"path": "a"}},
            {"type": "tool_result", "name": "read_file", "result": "ok", "is_error": False},
            {"type": "text_delta", "text": "done"},
            {
                "type": "context_compressed",
                "before_tokens": 100,
                "after_tokens": 50,
                "summarized_messages": 2,
            },
            {
                "type": "done",
                "total_turns": 2,
                "usage": {"total_tokens": 123},
                "cost": {"usd": 0.01},
            },
        ]
    )

    assert trace.tool_calls == [{"name": "read_file", "input": {"path": "a"}}]
    assert trace.tool_results == [{"name": "read_file", "result": "ok", "is_error": False}]
    assert trace.final_text == "done"
    assert trace.turns == 2
    assert trace.usage["total_tokens"] == 123
    assert trace.cost == {"usd": 0.01}
    assert trace.context_compressions[0]["before_tokens"] == 100


def test_trace_from_events_records_agent_error():
    trace = trace_from_events([{"type": "error", "error": "boom"}])

    assert trace.error == "boom"


def test_load_env_file_sets_missing_values_without_overriding(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EVAL_FOO=bar\nEVAL_QUOTED='qux'\n# comment\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("EVAL_FOO", raising=False)
    monkeypatch.delenv("EVAL_QUOTED", raising=False)

    _load_env_file(env_file)
    assert os.environ["EVAL_FOO"] == "bar"
    assert os.environ["EVAL_QUOTED"] == "qux"

    monkeypatch.setenv("EVAL_FOO", "existing")
    _load_env_file(env_file)
    assert os.environ["EVAL_FOO"] == "existing"


def test_build_engine_can_exclude_network_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PAICLI_API_KEY", "test-key")

    engine = build_engine(
        tmp_path,
        excluded_tools={"web_search", "web_fetch"},
        command_blacklist_extra=["curl"],
    )

    assert engine.tool_registry.get("web_search") is None
    assert engine.tool_registry.get("web_fetch") is None
    assert engine.tool_registry.get("read_file") is not None
    assert "curl" in engine.config.policy.command_blacklist


def test_build_engine_tool_overrides_replace_builtin_bash(tmp_path, monkeypatch):
    monkeypatch.setenv("PAICLI_API_KEY", "test-key")

    async def handler(payload, context):  # noqa: ARG001
        return ToolResult("container")

    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    override_bash = Tool(
        name="bash",
        description="container bash",
        parameters=parameters,
        handler=handler,
    )
    override_execute = Tool(
        name="execute_command",
        description="container execute_command",
        parameters=parameters,
        handler=handler,
    )

    engine = build_engine(
        tmp_path,
        tool_overrides={"bash": override_bash, "execute_command": override_execute},
    )

    assert engine.tool_registry.get("bash") is override_bash
    assert engine.tool_registry.get("execute_command") is override_execute
    assert engine.tool_registry.get("read_file") is not None
