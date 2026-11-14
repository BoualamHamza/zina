"""zina — trace where your Claude Code skills spend time and context."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .transcript import list_sessions, parse_session


def _resolve_transcript(args) -> Path:
    if args.transcript:
        return Path(args.transcript)
    sessions = list_sessions(args.project)
    if not sessions:
        sys.exit("No transcripts found. Pass --transcript FILE or --project DIR.")
    if args.session and args.session != "latest":
        for p in sessions:
            if p.stem.startswith(args.session):
                return p
        sys.exit(f"No session starting with {args.session!r}.")
    return sessions[0]


def _load(args):
    path = _resolve_transcript(args)
    s = parse_session(path)
    # Enrich tool durations from hook events if we have them.
    try:
        from .store import connect, hook_durations
        conn = connect()
        durs = hook_durations(conn, s.id)
        for tc in s.tool_calls:
            if tc.id in durs:
                tc.hook_duration_ms = durs[tc.id]
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return s


def cmd_sessions(args):
    for p in list_sessions(args.project)[: args.limit]:
        st = p.stat()
        print(f"{p.stem}  {st.st_size / 1024:8.0f} KB  {p.parent.name}")


def cmd_report(args):
    from .report import render
    from .store import connect, ingest, skill_leaderboard

    s = _load(args)
    conn = connect()
    ingest(conn, s)
    lb = skill_leaderboard(conn, None if args.all_projects else s.project)
    conn.close()
    html = render(s, lb)
    out = Path(args.output or f"zina-{s.id[:8]}.html")
    out.write_text(html, encoding="utf-8")
    tok = s.tokens()
    print(f"session {s.id}  turns={len(s.turns)} api_calls={len(s.api_calls)} tool_calls={len(s.tool_calls)}")
    print(f"input={tok['total_input']:,} (cache_read={tok['cache_read']:,} cache_write={tok['cache_creation']:,}) output={tok['output']:,}")
    print(f"report -> {out.resolve()}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


def cmd_ingest(args):
    from .store import connect, ingest

    conn = connect()
    paths = [Path(args.transcript)] if args.transcript else list_sessions(args.project)
    for p in paths[: args.limit]:
        s = parse_session(p)
        ingest(conn, s)
        print(f"ingested {s.id}  turns={len(s.turns)} tools={len(s.tool_calls)}")
    conn.close()


def cmd_top(args):
    from .store import connect, skill_leaderboard

    conn = connect()
    rows = skill_leaderboard(conn, args.project_name)
    conn.close()
    if not rows:
        print("Nothing ingested yet. Run `zina ingest` or `zina report` first.")
        return
    print(f"{'kind':6} {'name':32} {'inv':>4} {'sess':>4} {'input tok':>12} {'cache wr':>10} {'tools':>6} {'tok ret':>9} {'avg wall':>9}")
    for r in rows[: args.limit]:
        print(f"{r['kind']:6} {r['name'][:32]:32} {r['invocations']:>4} {r['sessions']:>4} "
              f"{r['input_tokens'] or 0:>12,} {r['cache_creation'] or 0:>10,} {r['tool_calls'] or 0:>6} "
              f"{r['tool_result_tokens'] or 0:>9,} {(r['avg_ms'] or 0) / 1000:>8.1f}s")


def cmd_json(args):
    from .report import session_payload

    s = _load(args)
    json.dump(session_payload(s), sys.stdout, indent=None if args.compact else 2, default=str)
    print()


def cmd_init(args):
    from .hooks import install

    target = Path(args.settings or (Path.cwd() / ".claude" / "settings.json"))
    cmd = args.command or (f"{sys.executable} -m zina hook" if args.absolute else "zina hook")
    install(target, cmd)
    print(f"hooks installed in {target} (command: {cmd!r})")
    print("Restart Claude Code (or open /hooks) for the hooks to be picked up.")


def cmd_hook(args):
    from .hooks import hook_main
    sys.exit(hook_main())


DIAGNOSE_PROMPT = """You are reviewing a trace of a Claude Code session to find why skills and
tools were slow or expensive. Below is the trace as JSON (turns, spans, tool
calls, per-API-call token usage, rule-based findings) and, when available, the
SKILL.md files involved.

Produce a short report with:
1. The three biggest sources of input tokens / wall time, with numbers.
2. For each skill span: what it read, whether it needed to, and a concrete
   rewrite suggestion for its SKILL.md (progressive disclosure, Grep before
   Read, delegate to a subagent, cache-friendly ordering).
3. Anything that broke the prompt cache (cache_creation spikes) and how to avoid it.
Be specific and terse. Do not restate the data.
"""


def cmd_diagnose(args):
    from .report import session_payload

    s = _load(args)
    payload = session_payload(s)
    payload.pop("leaderboard", None)
    # Attach SKILL.md bodies for the skills seen, if we can find them.
    skills = {}
    names = {sp["name"] for sp in payload["spans"] if sp["kind"] == "skill"}
    roots = [Path.cwd() / ".claude" / "skills", Path.home() / ".claude" / "skills"]
    for n in names:
        base = n.split(":")[-1]
        for r in roots:
            f = r / base / "SKILL.md"
            if f.exists():
                skills[n] = f.read_text(encoding="utf-8")[:20_000]
                break
    prompt = DIAGNOSE_PROMPT + "\n\n## Trace\n" + json.dumps(payload, default=str)
    if skills:
        prompt += "\n\n## SKILL.md files\n" + "\n\n".join(f"### {k}\n{v}" for k, v in skills.items())
    if args.print_prompt:
        print(prompt)
        return
    cmd = ["claude", "-p", "--output-format", "text"]
    if args.model:
        cmd += ["--model", args.model]
    try:
        res = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=600)
    except FileNotFoundError:
        sys.exit("`claude` CLI not found; use --print-prompt and paste it yourself.")
    print(res.stdout or res.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="zina", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--project", help="project directory (default: all projects)")
        p.add_argument("--transcript", help="explicit transcript .jsonl path")
        p.add_argument("--session", default="latest", help="session id prefix or 'latest'")

    p = sub.add_parser("sessions", help="list transcripts, newest first"); p.add_argument("--project"); p.add_argument("--limit", type=int, default=20); p.set_defaults(fn=cmd_sessions)
    p = sub.add_parser("report", help="render an HTML trace report"); common(p)
    p.add_argument("-o", "--output"); p.add_argument("--open", action="store_true"); p.add_argument("--all-projects", action="store_true", help="leaderboard across all projects"); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("ingest", help="ingest transcripts into the local db"); p.add_argument("--project"); p.add_argument("--transcript"); p.add_argument("--limit", type=int, default=50); p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("top", help="skill/agent leaderboard across ingested sessions"); p.add_argument("--project-name", help="project slug to filter on"); p.add_argument("--limit", type=int, default=30); p.set_defaults(fn=cmd_top)
    p = sub.add_parser("json", help="dump the parsed trace as JSON"); common(p); p.add_argument("--compact", action="store_true"); p.set_defaults(fn=cmd_json)
    p = sub.add_parser("init", help="install zina hooks into .claude/settings.json"); p.add_argument("--settings"); p.add_argument("--command"); p.add_argument("--absolute", action="store_true", help="use the current python interpreter path"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("hook", help="(internal) hook receiver, reads JSON on stdin"); p.set_defaults(fn=cmd_hook)
    p = sub.add_parser("diagnose", help="ask Claude to review the trace"); common(p); p.add_argument("--model"); p.add_argument("--print-prompt", action="store_true"); p.set_defaults(fn=cmd_diagnose)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
