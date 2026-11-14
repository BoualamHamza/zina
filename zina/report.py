"""Render a self-contained HTML trace report for one session."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .analyze import findings
from .model import Session

TEMPLATE_PATH = Path(__file__).parent / "report.html"


def session_payload(s: Session) -> dict:
    t0 = s.started_at
    def rel(dt):
        return int((dt - t0).total_seconds() * 1000) if (dt and t0) else None

    turns = [{
        "id": t.id, "index": t.index, "prompt": t.prompt,
        "start": rel(t.started_at), "end": rel(t.ended_at), "duration_ms": t.duration_ms,
    } for t in s.turns]
    spans = []
    for r in s.span_stats():
        sp = s.spans[r["id"]]
        r = dict(r)
        r["start"], r["end"] = rel(sp.started_at), rel(sp.ended_at)
        spans.append(r)
    tools = [{
        "id": tc.id, "name": tc.name, "summary": tc.summary(), "span_id": tc.span_id,
        "start": rel(tc.started_at), "end": rel(tc.ended_at), "duration_ms": tc.duration_ms,
        "result_tokens": tc.result_tokens, "is_error": tc.is_error,
        "child_agent_id": tc.child_agent_id, "skill": tc.skill_name,
    } for tc in s.tool_calls]
    api = [{
        "id": c.id, "t": rel(c.ts), "span_id": c.span_id, "model": c.model,
        "input": c.input_tokens, "cache_read": c.cache_read, "cache_creation": c.cache_creation,
        "output": c.output_tokens,
    } for c in sorted(s.api_calls, key=lambda c: c.ts)]
    att = Counter()
    for a in s.attachments:
        att[a.type] += (a.chars + 3) // 4
    return {
        "session": {
            "id": s.id, "project": s.project, "transcript": s.transcript_path,
            "version": s.version, "models": sorted(s.models), "cost_usd": s.cost_usd,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "duration_ms": int((s.ended_at - s.started_at).total_seconds() * 1000) if s.started_at and s.ended_at else 0,
            "system_prompt_tokens": (s.system_prompt_chars + 3) // 4,
            "tokens": s.tokens(),
        },
        "turns": turns, "spans": spans, "tools": tools, "api": api,
        "attachments": [{"type": k, "tokens": v} for k, v in att.most_common()],
        "findings": findings(s),
    }


def render(s: Session, leaderboard: list[dict] | None = None) -> str:
    data = session_payload(s)
    data["leaderboard"] = leaderboard or []
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    blob = json.dumps(data).replace("</", "<\\/")
    return html.replace("/*__ZINA_DATA__*/null", blob)
