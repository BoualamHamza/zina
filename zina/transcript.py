"""Parse Claude Code transcript JSONL files into the trace model.

Layout on disk (Claude Code 2.x):

    ~/.claude/projects/<slug>/<session-id>.jsonl
    ~/.claude/projects/<slug>/<session-id>/subagents/agent-<agent-id>.jsonl

`<slug>` is the project directory path with `/` replaced by `-`.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .model import ApiCall, Attachment, Session, Span, ToolCall, Turn, est_tokens

AGENT_ID_RE = re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def projects_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def project_slug(project_dir: str | os.PathLike) -> str:
    p = str(Path(project_dir).resolve())
    return p.replace("/", "-").replace("\\", "-")


def list_sessions(project_dir: Optional[str] = None) -> list[Path]:
    """Return session transcript paths, newest first."""
    root = projects_root()
    if project_dir:
        dirs = [root / project_slug(project_dir)]
    else:
        dirs = [d for d in root.iterdir() if d.is_dir()] if root.exists() else []
    out: list[Path] = []
    for d in dirs:
        if d.exists():
            out.extend(p for p in d.glob("*.jsonl") if p.is_file())
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def iter_entries(path: Path) -> Iterable[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _content_text(content) -> str:
    """Flatten a message content field (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "tool_result":
            parts.append(_content_text(b.get("content")))
        elif t in ("tool_use",):
            parts.append(json.dumps(b.get("input", {})))
        elif t == "image":
            parts.append(" " * 1500)  # rough image cost
    return "".join(parts)


def _rendered_chars(entry: dict) -> int:
    r = entry.get("rendered")
    if isinstance(r, list):
        return sum(len(x.get("content", "")) for x in r if isinstance(x, dict))
    att = entry.get("attachment", {})
    if att.get("type") == "prompt_snapshot":
        return 0  # system prompt is counted separately
    return len(json.dumps(att))


class _Parser:
    """Stateful single-transcript parser. One instance per agent transcript."""

    def __init__(self, session: Session, agent_id: str, parent_span: Optional[Span], depth: int):
        self.s = session
        self.agent_id = agent_id
        self.parent_span = parent_span
        self.depth = depth
        self.turn: Optional[Turn] = None
        self.active_span: Optional[Span] = None
        self.pending_tools: dict[str, ToolCall] = {}
        self.seen_msgs: set[str] = set()
        self.last_ts: Optional[datetime] = None
        self.turn_last_ts: Optional[datetime] = None  # last model/tool activity in the turn
        self.skill_call: Optional[ToolCall] = None  # Skill tool call whose body is pending

    # -- span helpers ---------------------------------------------------
    def _open_span(self, kind: str, name: str, ts: datetime, sid: str) -> Span:
        if self.active_span and self.active_span.ended_at is None and kind == "skill":
            # A new skill supersedes the current one within the same turn.
            if self.active_span.kind == "skill":
                self.active_span.ended_at = ts
        sp = Span(
            id=sid, kind=kind, name=name, started_at=ts,
            parent_id=self.parent_span.id if self.parent_span else "",
            turn_id=self.turn.id if self.turn else "",
            agent_id=self.agent_id, depth=self.depth,
        )
        self.s.spans[sp.id] = sp
        if self.turn is not None:
            self.turn.spans.append(sp)
        self.active_span = sp
        return sp

    def _close_turn(self, ts: datetime):
        if self.turn is None:
            return
        # End at the last activity, not at the next prompt: the user's idle
        # time between turns is not the agent's time.
        end = self.turn_last_ts or ts
        self.turn.ended_at = end
        for sp in self.turn.spans:
            if sp.ended_at is None:
                sp.ended_at = end
        for tc in list(self.pending_tools.values()):
            if tc.ended_at is None:
                tc.ended_at = end
        self.pending_tools.clear()
        self.active_span = None
        self.skill_call = None
        self.turn_last_ts = None

    # -- entry handlers -------------------------------------------------
    def feed(self, entry: dict):
        t = entry.get("type")
        ts_s = entry.get("timestamp")
        ts = parse_ts(ts_s) if ts_s else self.last_ts
        if ts:
            self.last_ts = ts
            if self.s.started_at is None or ts < self.s.started_at:
                self.s.started_at = ts
            if self.s.ended_at is None or ts > self.s.ended_at:
                self.s.ended_at = ts
        if t == "user":
            self._user(entry, ts)
        elif t == "assistant":
            self._assistant(entry, ts)
        elif t == "attachment":
            self._attachment(entry, ts)
        elif t == "cost-state":
            self.s.cost_usd = max(self.s.cost_usd, float(entry.get("totalCostUSD", 0) or 0))
        if not self.s.version and entry.get("version"):
            self.s.version = entry["version"]

    def _user(self, e: dict, ts: datetime):
        msg = e.get("message", {})
        content = msg.get("content")
        is_tool_result = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        if is_tool_result:
            self.turn_last_ts = ts
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tc = self.pending_tools.pop(b.get("tool_use_id", ""), None)
                if tc is None:
                    continue
                tc.ended_at = ts
                text = _content_text(b.get("content"))
                tc.result_chars = len(text)
                tc.is_error = bool(b.get("is_error"))
                if tc.name == "Agent":
                    m = AGENT_ID_RE.search(text)
                    if m:
                        tc.child_agent_id = m.group(1)
            return
        prompt = _content_text(content)
        pid = e.get("promptId")
        # Follow-up user messages that share the promptId of the current turn
        # are injected by the harness, not typed by the human: the SKILL.md
        # body after a `Skill` call, slash-command output, system notes.
        # They are context cost, attributed to the active span.
        if self.turn is not None and pid and pid == self.turn.id:
            self.turn_last_ts = ts
            span_id = self.active_span.id if self.active_span else ""
            if self.skill_call is not None and self.active_span and self.active_span.kind == "skill":
                self.skill_call.result_chars += len(prompt)
                self.skill_call = None
                self.s.attachments.append(Attachment(ts=ts, type="skill_body", chars=len(prompt), span_id=span_id))
            else:
                self.s.attachments.append(Attachment(ts=ts, type="user_injected", chars=len(prompt), span_id=span_id))
            return
        # Sidechain (subagent) transcripts have a single implicit turn.
        if e.get("isSidechain") and self.depth > 0 and self.active_span is not None:
            return
        if self.turn is not None:
            self._close_turn(ts)
        if self.depth == 0:
            tid = e.get("promptId") or e.get("uuid") or f"turn-{len(self.s.turns)}"
            self.turn = Turn(id=tid, index=len(self.s.turns) + 1, prompt=prompt[:400], started_at=ts)
            self.s.turns.append(self.turn)
            self._open_span("turn", f"turn {self.turn.index}", ts, f"turn:{tid}")
        else:
            # Subagent: attribute to the parent's agent span.
            self.turn = None
            self.active_span = self.parent_span

    def _assistant(self, e: dict, ts: datetime):
        msg = e.get("message", {})
        mid = msg.get("id", e.get("uuid"))
        model = msg.get("model", "")
        if model:
            self.s.models.add(model)
        self.turn_last_ts = ts
        span_id = self.active_span.id if self.active_span else (self.parent_span.id if self.parent_span else "")
        usage = msg.get("usage") or {}
        if mid not in self.seen_msgs and usage:
            self.seen_msgs.add(mid)
            self.s.api_calls.append(ApiCall(
                id=mid, ts=ts, model=model,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                cache_read=int(usage.get("cache_read_input_tokens", 0) or 0),
                cache_creation=int(usage.get("cache_creation_input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                span_id=span_id, stop_reason=msg.get("stop_reason") or "",
            ))
        for b in msg.get("content") or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name", "")
            inp = b.get("input") or {}
            tc = ToolCall(id=b.get("id", ""), name=name, input=inp, started_at=ts)
            if name == "Skill":
                tc.skill_name = str(inp.get("skill", "?"))
                sp = self._open_span("skill", tc.skill_name, ts, f"skill:{tc.id}")
                # The Skill call itself belongs to the caller. Its tool_result
                # is a one-liner; the SKILL.md body arrives as the next user
                # message with the same promptId and is added to result_chars.
                tc.span_id = span_id or sp.id
                self.skill_call = tc
            elif name == "Agent":
                sub = str(inp.get("subagent_type") or "agent")
                tc.span_id = span_id
                # Child span is opened when the result (with agentId) arrives;
                # the parser for the child transcript attaches to it. Store a
                # placeholder keyed by tool id so we can link later.
                tc.child_agent_id = ""  # filled from result
                self.s.spans[f"agent:{tc.id}"] = Span(
                    id=f"agent:{tc.id}", kind="agent", name=sub, started_at=ts,
                    parent_id=span_id, turn_id=self.turn.id if self.turn else "",
                    agent_id="", depth=self.depth + 1,
                )
                if self.turn is not None:
                    self.turn.spans.append(self.s.spans[f"agent:{tc.id}"])
            else:
                tc.span_id = span_id
            self.pending_tools[tc.id] = tc
            self.s.tool_calls.append(tc)

    def _attachment(self, e: dict, ts: datetime):
        att = e.get("attachment") or {}
        atype = att.get("type", "?")
        if atype == "prompt_snapshot":
            sp = att.get("systemPrompt")
            if isinstance(sp, list):
                self.s.system_prompt_chars = max(self.s.system_prompt_chars, sum(len(x) for x in sp))
            return
        chars = _rendered_chars(e)
        self.s.attachments.append(Attachment(
            ts=ts, type=atype, chars=chars,
            span_id=self.active_span.id if self.active_span else (self.parent_span.id if self.parent_span else ""),
        ))

    def finish(self):
        if self.turn is not None and self.last_ts:
            self._close_turn(self.last_ts)


def parse_session(path: str | os.PathLike) -> Session:
    path = Path(path)
    session = Session(id=path.stem, transcript_path=str(path), project=path.parent.name)
    p = _Parser(session, agent_id="", parent_span=None, depth=0)
    for entry in iter_entries(path):
        p.feed(entry)
    p.finish()

    # Link subagent transcripts to their Agent spans.
    sub_dir = path.parent / path.stem / "subagents"
    agent_calls = {tc.child_agent_id: tc for tc in session.tool_calls if tc.name == "Agent" and tc.child_agent_id}
    for tc in session.tool_calls:
        if tc.name != "Agent":
            continue
        sp = session.spans.get(f"agent:{tc.id}")
        if sp is None:
            continue
        sp.ended_at = tc.ended_at
        sp.agent_id = tc.child_agent_id
        sub_path = sub_dir / f"agent-{tc.child_agent_id}.jsonl"
        if tc.child_agent_id and sub_path.exists():
            cp = _Parser(session, agent_id=tc.child_agent_id, parent_span=sp, depth=sp.depth)
            cp.active_span = sp
            for entry in iter_entries(sub_path):
                cp.feed(entry)
            cp.finish()
    # Orphan subagent transcripts (agentId not found in any result text).
    if sub_dir.exists():
        for sub_path in sub_dir.glob("agent-*.jsonl"):
            aid = sub_path.stem[len("agent-"):]
            if aid in agent_calls:
                continue
            sp = Span(id=f"agent:{aid}", kind="agent", name="agent (unlinked)",
                      started_at=session.started_at, agent_id=aid, depth=1)
            session.spans[sp.id] = sp
            cp = _Parser(session, agent_id=aid, parent_span=sp, depth=1)
            cp.active_span = sp
            for entry in iter_entries(sub_path):
                cp.feed(entry)
            cp.finish()
            sp.ended_at = cp.last_ts
    return session
