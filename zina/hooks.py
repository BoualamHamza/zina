"""Hook collector: `zina hook` is registered for every Claude Code hook event
and records the payload it receives on stdin. It must be fast and must never
fail (a failing hook blocks the agent), so every error is swallowed.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_EVENTS = [
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "SubagentStop", "Stop", "PreCompact", "SessionEnd",
]


def hook_main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        from .store import connect, record_hook
        conn = connect()
        record_hook(conn, payload, datetime.now(timezone.utc).isoformat())
        conn.close()
    except Exception as exc:  # noqa: BLE001 - a hook must never break the agent
        try:
            log = Path.home() / ".zina" / "hook-errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} {exc!r}\n")
        except Exception:  # noqa: BLE001
            pass
    return 0


def hook_config(command: str = "zina hook") -> dict:
    return {ev: [{"hooks": [{"type": "command", "command": command, "timeout": 5}]}] for ev in HOOK_EVENTS}


def install(settings_path: Path, command: str = "zina hook") -> dict:
    """Merge zina hooks into a settings.json, returning the new settings."""
    settings: dict = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text() or "{}")
    hooks = settings.setdefault("hooks", {})
    for ev, entries in hook_config(command).items():
        existing = hooks.setdefault(ev, [])
        already = any(
            h.get("command") == command
            for e in existing for h in e.get("hooks", [])
        )
        if not already:
            existing.extend(entries)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings
