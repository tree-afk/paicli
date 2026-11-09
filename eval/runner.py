from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from eval.trace import CaseTrace, trace_from_events
from paicli.agent import QueryEngine
from paicli.config import load_config
from paicli.llm import create_llm_client
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import Tool


async def run_prompt(prompt: str, agent: Any) -> CaseTrace:
    started = time.perf_counter()
    events: list[dict[str, Any]] = []
    error: str | None = None
    try:
        async for event in agent.ask(prompt):
            events.append(event)
    except Exception as exc:  # noqa: BLE001 - evaluation must record agent failures
        error = str(exc)
    trace = trace_from_events(events)
    trace.duration_s = round(time.perf_counter() - started, 3)
    if error and not trace.error:
        trace.error = error
    return trace


def build_engine(
    cwd: str | Path,
    *,
    compression: bool = True,
    provider: str | None = None,
    model: str | None = None,
    max_turns: int = 20,
    excluded_tools: set[str] | None = None,
    command_blacklist_extra: list[str] | None = None,
    tool_overrides: dict[str, Tool] | None = None,
) -> QueryEngine:
    llm_overrides: dict[str, Any] = {"temperature": 0.0}
    if provider:
        llm_overrides["provider"] = provider
    if model:
        llm_overrides["model"] = model
    config = load_config(
        project_root=cwd,
        overrides={
            "llm": llm_overrides,
            "policy": {"hitl_mode": "never"},
            "features": {"context_compression": compression, "memory": False},
            "memory": {"long_term_enabled": False},
        },
    )
    if command_blacklist_extra:
        config.policy.command_blacklist.extend(command_blacklist_extra)
    excluded = excluded_tools or set()
    registry = ToolRegistry()
    registry.register_all([tool for tool in get_builtin_tools() if tool.name not in excluded])
    for tool in (tool_overrides or {}).values():
        registry.register(tool)
    return QueryEngine(
        llm_client=create_llm_client(config.llm),
        tool_registry=registry,
        config=config,
        cwd=str(cwd),
        max_turns=max_turns,
    )
