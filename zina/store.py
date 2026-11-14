"""SQLite persistence for cross-session leaderboards and hook events."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .model import Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, project TEXT, transcript_path TEXT, version TEXT,
  started_at TEXT, ended_at TEXT, cost_usd REAL, models TEXT,
  input_tokens INTEGER, cache_read INTEGER, cache_creation INTEGER, output_tokens INTEGER,
  turns INTEGER, tool_calls INTEGER, ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS spans (
  id TEXT, session_id TEXT, turn_id TEXT, parent_id TEXT, kind TEXT, name TEXT,
  agent_id TEXT, depth INTEGER, started_at TEXT, ended_at TEXT, duration_ms INTEGER,
  api_calls INTEGER, input_tokens INTEGER, cache_read INTEGER, cache_creation INTEGER,
  output_tokens INTEGER, tool_calls INTEGER, tool_result_tokens INTEGER, tool_ms INTEGER,
  PRIMARY KEY (session_id, id)
);
CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT, session_id TEXT, span_id TEXT, name TEXT, summary TEXT,
  started_at TEXT, ended_at TEXT, duration_ms INTEGER, result_chars INTEGER,
  is_error INTEGER, child_agent_id TEXT, skill_name TEXT,
  PRIMARY KEY (session_id, id)
);
CREATE TABLE IF NOT EXISTS api_calls (
  id TEXT, session_id TEXT, span_id TEXT, ts TEXT, model TEXT,
  input_tokens INTEGER, cache_read INTEGER, cache_creation INTEGER, output_tokens INTEGER,
  PRIMARY KEY (session_id, id)
);
CREATE TABLE IF NOT EXISTS hook_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT, session_id TEXT,
  event TEXT, tool_name TEXT, tool_use_id TEXT, transcript_path TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS hook_events_session ON hook_events(session_id, tool_use_id);
"""


def default_db_path() -> Path:
    env = os.environ.get("ZINA_DB")
    if env:
        return Path(env)
    return Path(os.environ.get("ZINA_HOME", Path.home() / ".zina")) / "zina.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _iso(dt):
    return dt.isoformat() if dt else None


def ingest(conn: sqlite3.Connection, s: Session) -> None:
    tok = s.tokens()
    conn.execute(
        "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (s.id, s.project, s.transcript_path, s.version, _iso(s.started_at), _iso(s.ended_at),
         s.cost_usd, ",".join(sorted(s.models)), tok["input"], tok["cache_read"],
         tok["cache_creation"], tok["output"], len(s.turns), len(s.tool_calls)),
    )
    conn.execute("DELETE FROM spans WHERE session_id=?", (s.id,))
    conn.execute("DELETE FROM tool_calls WHERE session_id=?", (s.id,))
    conn.execute("DELETE FROM api_calls WHERE session_id=?", (s.id,))
    conn.executemany(
        "INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["id"], s.id, r["turn_id"], r["parent_id"], r["kind"], r["name"], r["agent_id"],
          r["depth"], r["started_at"], r["ended_at"], r["duration_ms"], r["api_calls"],
          r["input_tokens"], r["cache_read"], r["cache_creation"], r["output_tokens"],
          r["tool_calls"], r["tool_result_tokens"], r["tool_ms"]) for r in s.span_stats()],
    )
    conn.executemany(
        "INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(t.id, s.id, t.span_id, t.name, t.summary(), _iso(t.started_at), _iso(t.ended_at),
          t.duration_ms, t.result_chars, int(t.is_error), t.child_agent_id, t.skill_name)
         for t in s.tool_calls],
    )
    conn.executemany(
        "INSERT INTO api_calls VALUES (?,?,?,?,?,?,?,?,?)",
        [(c.id, s.id, c.span_id, _iso(c.ts), c.model, c.input_tokens, c.cache_read,
          c.cache_creation, c.output_tokens) for c in s.api_calls],
    )
    conn.commit()


def record_hook(conn: sqlite3.Connection, payload: dict, received_at: str) -> None:
    conn.execute(
        "INSERT INTO hook_events (received_at, session_id, event, tool_name, tool_use_id, transcript_path, payload)"
        " VALUES (?,?,?,?,?,?,?)",
        (received_at, payload.get("session_id", ""), payload.get("hook_event_name", ""),
         payload.get("tool_name", ""), payload.get("tool_use_id", ""),
         payload.get("transcript_path", ""), json.dumps(payload)[:200_000]),
    )
    conn.commit()


def hook_durations(conn: sqlite3.Connection, session_id: str) -> dict[str, int]:
    """tool_use_id -> duration_ms, from PreToolUse/PostToolUse pairs."""
    rows = conn.execute(
        "SELECT event, tool_use_id, received_at FROM hook_events WHERE session_id=? AND tool_use_id!='' ORDER BY seq",
        (session_id,),
    ).fetchall()
    from datetime import datetime
    pre: dict[str, datetime] = {}
    out: dict[str, int] = {}
    for r in rows:
        ts = datetime.fromisoformat(r["received_at"])
        if r["event"] == "PreToolUse":
            pre[r["tool_use_id"]] = ts
        elif r["event"] == "PostToolUse" and r["tool_use_id"] in pre:
            out[r["tool_use_id"]] = int((ts - pre.pop(r["tool_use_id"])).total_seconds() * 1000)
    return out


def skill_leaderboard(conn: sqlite3.Connection, project: str | None = None) -> list[dict]:
    where = "WHERE sp.kind IN ('skill','agent')"
    args: tuple = ()
    if project:
        where += " AND se.project=?"
        args = (project,)
    rows = conn.execute(f"""
        SELECT sp.kind, sp.name, COUNT(*) AS invocations, COUNT(DISTINCT sp.session_id) AS sessions,
               SUM(sp.api_calls) AS api_calls, SUM(sp.tool_calls) AS tool_calls,
               SUM(sp.input_tokens + sp.cache_read + sp.cache_creation) AS input_tokens,
               SUM(sp.cache_creation) AS cache_creation, SUM(sp.cache_read) AS cache_read,
               SUM(sp.tool_result_tokens) AS tool_result_tokens,
               AVG(sp.duration_ms) AS avg_ms, MAX(sp.duration_ms) AS max_ms
        FROM spans sp JOIN sessions se ON se.id = sp.session_id
        {where} GROUP BY sp.kind, sp.name ORDER BY input_tokens DESC
    """, args).fetchall()
    return [dict(r) for r in rows]
