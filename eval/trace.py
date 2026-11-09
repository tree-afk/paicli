from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CaseTrace:
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    turns: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    context_compressions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0


def trace_from_events(events: list[dict[str, Any]]) -> CaseTrace:
    trace = CaseTrace()
    text_parts: list[str] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "text_delta":
            text_parts.append(str(event.get("text") or ""))
        elif event_type == "tool_call":
            trace.tool_calls.append(
                {"name": str(event.get("name") or ""), "input": event.get("input")}
            )
        elif event_type == "tool_result":
            trace.tool_results.append(
                {
                    "name": str(event.get("name") or ""),
                    "result": event.get("result"),
                    "is_error": bool(event.get("is_error")),
                }
            )
        elif event_type == "context_compressed":
            trace.context_compressions.append(dict(event))
        elif event_type == "done":
            trace.turns = int(event.get("total_turns") or 0)
            trace.usage = dict(event.get("usage") or {})
            trace.cost = dict(event.get("cost") or {})
        elif event_type == "error":
            trace.error = str(event.get("error") or "agent error")
    trace.final_text = "".join(text_parts)
    return trace
