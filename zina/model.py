"""In-memory trace model: Session > Turn > Span > (ApiCall | ToolCall).

A *span* is the unit of attribution. Every API call and tool call made after a
`Skill` invocation, until the end of the turn, is attributed to that skill.
An `Agent` call opens a child span whose events live in a separate transcript.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def est_tokens(text: str) -> int:
    """Cheap token estimate (chars / 4). Good enough to rank, not to bill."""
    return (len(text) + 3) // 4


@dataclass
class ApiCall:
    id: str
    ts: datetime
    model: str
    input_tokens: int
    cache_read: int
    cache_creation: int
    output_tokens: int
    span_id: str = ""
    stop_reason: str = ""

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read + self.cache_creation

    @property
    def uncached(self) -> int:
        return self.input_tokens + self.cache_creation


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    started_at: datetime
    ended_at: Optional[datetime] = None
    result_chars: int = 0
    is_error: bool = False
    span_id: str = ""
    child_agent_id: str = ""  # set for Agent calls
    skill_name: str = ""  # set for Skill calls
    # From hooks, when available (more exact than transcript timestamps).
    hook_duration_ms: Optional[int] = None

    @property
    def duration_ms(self) -> int:
        if self.hook_duration_ms is not None:
            return self.hook_duration_ms
        if self.ended_at is None:
            return 0
        return int((self.ended_at - self.started_at).total_seconds() * 1000)

    @property
    def result_tokens(self) -> int:
        return (self.result_chars + 3) // 4

    def summary(self) -> str:
        i = self.input
        for key in ("skill", "command", "file_path", "pattern", "description", "query", "prompt", "subagent_type"):
            if key in i and isinstance(i[key], str):
                return i[key][:100]
        return ""


@dataclass
class Attachment:
    """Context injected by the harness (system-reminders, env snapshots...)."""
    ts: datetime
    type: str
    chars: int
    span_id: str = ""


@dataclass
class Span:
    id: str
    kind: str  # "turn" | "skill" | "agent"
    name: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    parent_id: str = ""
    turn_id: str = ""
    agent_id: str = ""
    depth: int = 0

    @property
    def duration_ms(self) -> int:
        if self.ended_at is None:
            return 0
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


@dataclass
class Turn:
    id: str
    index: int
    prompt: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    spans: list[Span] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        if self.ended_at is None:
            return 0
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


@dataclass
class Session:
    id: str
    transcript_path: str
    project: str = ""
    version: str = ""
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    cost_usd: float = 0.0
    models: set = field(default_factory=set)
    turns: list[Turn] = field(default_factory=list)
    spans: dict[str, Span] = field(default_factory=dict)
    api_calls: list[ApiCall] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    system_prompt_chars: int = 0

    # ---- aggregates -------------------------------------------------
    def tokens(self) -> dict:
        t = {"input": 0, "cache_read": 0, "cache_creation": 0, "output": 0}
        for c in self.api_calls:
            t["input"] += c.input_tokens
            t["cache_read"] += c.cache_read
            t["cache_creation"] += c.cache_creation
            t["output"] += c.output_tokens
        t["total_input"] = t["input"] + t["cache_read"] + t["cache_creation"]
        return t

    def span_stats(self) -> list[dict]:
        """Per-span rollup: tokens, tool calls, duration."""
        by: dict[str, dict] = {}
        for s in self.spans.values():
            by[s.id] = {
                "id": s.id, "kind": s.kind, "name": s.name, "turn_id": s.turn_id,
                "parent_id": s.parent_id, "depth": s.depth, "agent_id": s.agent_id,
                "started_at": s.started_at.isoformat(),
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "duration_ms": s.duration_ms,
                "api_calls": 0, "input_tokens": 0, "cache_read": 0,
                "cache_creation": 0, "output_tokens": 0,
                "tool_calls": 0, "tool_result_tokens": 0, "tool_ms": 0,
                "tools": {},
            }
        for c in self.api_calls:
            r = by.get(c.span_id)
            if not r:
                continue
            r["api_calls"] += 1
            r["input_tokens"] += c.input_tokens
            r["cache_read"] += c.cache_read
            r["cache_creation"] += c.cache_creation
            r["output_tokens"] += c.output_tokens
        for t in self.tool_calls:
            r = by.get(t.span_id)
            if not r:
                continue
            r["tool_calls"] += 1
            r["tool_result_tokens"] += t.result_tokens
            r["tool_ms"] += t.duration_ms
            r["tools"][t.name] = r["tools"].get(t.name, 0) + 1
        return list(by.values())
