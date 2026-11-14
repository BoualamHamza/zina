# zina

Tracing for Claude Code skills, plugins and subagents. Answers the question
*"why did that take so long, and where did my context go?"* per skill, not per session.

```
pip install -e .
zina report --open          # trace the latest session in ~/.claude/projects
zina top                    # skill / subagent leaderboard across sessions
zina diagnose               # hand the trace to `claude -p` for rewrite suggestions
```

No dependencies, no server, no network. Python 3.11+, SQLite, one HTML file.

## What it measures

Claude Code already writes everything needed; zina joins it:

| Source | Used for |
|---|---|
| `~/.claude/projects/<slug>/<session>.jsonl` | per-API-call `usage` (cache read / cache write / uncached / output), every `tool_use` and `tool_result`, timestamps, harness-injected system reminders |
| `<session>/subagents/agent-<id>.jsonl` | the same, for each `Agent` call, linked back by `agentId` |
| hooks (`zina init`) | exact wall time per tool call via `PreToolUse`/`PostToolUse`; optional |

### The attribution model

```
Session
└─ Turn                         one human prompt → last model/tool activity
   ├─ Skill span                from the `Skill` call to the end of the turn
   │   ├─ tool calls            Read / Grep / Bash … with tokens returned and duration
   │   └─ API calls             tokens: cache read · cache write · uncached · output
   └─ Agent span                an `Agent` call; its events come from the child transcript
```

Everything the model does after invoking a skill, until the turn ends, is
charged to that skill. It is a heuristic (a skill has no "end" marker), but it
is the same one you apply when reading a transcript by hand and it is enough
to rank skills by cost.

Two details that matter and are easy to get wrong:

- The `Skill` tool_result is a one-liner. The SKILL.md body arrives as the
  *next user message with the same `promptId`*. zina counts that body as the
  skill's injected tokens.
- One API response is written as several transcript lines (one per content
  block) that repeat the same `usage`. zina dedupes on `message.id`.

### The report

- **Timeline**: one row per turn, skill spans in orange, subagent spans in
  purple, tool calls as ticks whose height is the tokens they returned.
- **Context per API call**: stacked bars (cache read / cache write / uncached /
  output). A tall red bar is a cache miss; the strip underneath says which span
  caused it.
- **Findings**: rule-based. Large tool results, read storms, cache misses with
  their likely cause (model switch, idle TTL expiry, prefix change), harness
  attachment volume, slow tools, skills that dominate input tokens.
- **Spans / tool calls / attachments** tables, sortable.
- **Leaderboard** over every ingested session, so the skill that is slow
  *every time* floats to the top.

## Commands

```
zina sessions [--project DIR]                list transcripts, newest first
zina report   [--session ID|latest] [-o F] [--open] [--all-projects]
zina ingest   [--project DIR] [--limit N]    load transcripts into ~/.zina/zina.db
zina top      [--project-name SLUG]          leaderboard from the db
zina json     [--compact]                    the parsed trace, for your own tooling
zina init     [--settings PATH] [--absolute] add hooks to .claude/settings.json
zina diagnose [--model M] [--print-prompt]   ask Claude to review the trace + SKILL.md files
zina hook                                    (internal) hook receiver
```

`ZINA_DB` or `ZINA_HOME` relocate the database. `CLAUDE_CONFIG_DIR` is honoured.

## Hooks

`zina init` registers `zina hook` for SessionStart, UserPromptSubmit,
PreToolUse, PostToolUse, SubagentStop, Stop, PreCompact and SessionEnd. The
receiver appends the payload to SQLite and always exits 0, so it can never
block the agent. Without hooks everything still works; tool durations are
then estimated from transcript timestamps (tool_use → tool_result), which
is within a few hundred ms.

## Dogfood

This repo was built inside a Claude Code session and traced itself
(`zina report` on its own transcript). The first run found: a 104k-token
cache write caused by a mid-session model switch, a 3.8k-token skill body,
and ~32k tokens of harness attachments (MCP instructions, skill listing,
deferred tool lists) before any work started.

## Roadmap

- [ ] Session-level "context budget" view: system prompt / CLAUDE.md / skills / tool results / conversation over time, with compaction markers
- [ ] Attribute CLAUDE.md and hook-injected instructions (no clean start marker today → counted as session overhead)
- [ ] Watch mode: re-render while the session runs
- [ ] `zina diagnose --apply`: propose SKILL.md diffs, not just findings
- [ ] Adapters for other agents' transcript formats (the model is agent-agnostic; only `transcript.py` is Claude Code-specific)
