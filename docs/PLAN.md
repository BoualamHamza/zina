# zina v1 — the plan

*Status: proposal, 2026-11-14. Supersedes the v0 CLI, which stays as a reference implementation of the parser.*

## 0. One paragraph

zina is a Claude Code plugin. You install it once, forget it, and when a session feels slow or expensive you type `/zina` and get a local dashboard that tells you, in plain sentences with numbers, where the time and the context went, **per skill and per subagent**, and what to change. `/zina diagnose` goes one step further: a subagent reads the trace and proposes concrete edits to your SKILL.md files, which you accept or reject like any other edit.

## 1. Who it is for, and the moment it is for

**The user**: someone who has a real repo, with `.claude/skills/`, a `CLAUDE.md`, a few plugins and MCP servers, and who has noticed that sessions got slower and more expensive as the repo grew but cannot say *why*.

**The moment**: not "when they decide to profile". Nobody decides to profile. The moment is: they just watched Claude spend 90 seconds and a visible chunk of context on a skill that should have been quick, and they are mildly annoyed. zina must be reachable *from that annoyance*, inside the session, in one command, with nothing to set up.

So the experience is designed backwards from that moment:

| When | What the user does | What zina does | Cost to the user |
|---|---|---|---|
| Install (once) | `/plugin marketplace add BoualamHamza/zina` then `/plugin install zina@zina` | nothing visible | ~30 s, once |
| Every session start | nothing | `SessionStart` hook registers `{session_id, transcript_path, cwd}` in `~/.zina/sessions.jsonl`. Silent. No output to context. | 0 tokens, ~40 ms |
| Working | nothing | nothing. **No per-tool-call hooks in v1** (see §6). | 0 |
| The annoyance | `/zina` | starts (or reuses) the local server, prints one line: `zina → http://127.0.0.1:7474/s/1709…  (live)`, opens the browser where it can | ~150 tokens |
| Looking | reads the dashboard, which refreshes itself while the session continues | parses the transcript incrementally | 0 tokens |
| Wanting a fix | `/zina diagnose` | spawns the `zina-diagnoser` subagent; it reads the trace and the SKILL.md files involved, writes a diagnosis the dashboard shows, and comes back with proposed edits | ~30–60k tokens in a subagent, ~1 min; main context grows by the summary only |
| Later | opens `http://127.0.0.1:7474` (or `/zina` again) | project home: the last sessions, and the skills that are slow *every time* | 0 |

Everything else (CLI flags, SQLite, JSON export) exists but is not the product.

## 2. What ships in v1

### In
- **Claude Code plugin** installable from this repo as its own marketplace. Components: one command (`/zina`), one agent (`zina-diagnoser`), one hook (`SessionStart`).
- **Local server + dashboard**, zero dependencies, started by `/zina`, serving three pages: *Session*, *Skill*, *Project* (§4).
- **Parser** with the corrected attribution model (§5), ported from v0 with its tests.
- **Deterministic insights** (§5.4): sentences with numbers, computed by rules, shown instantly.
- **LLM diagnosis** via the plugin's subagent (§5.5), on demand, with proposed SKILL.md edits.
- **Project memory**: parsed sessions cached in `~/.zina/cache/<session>.json` so the project page and the per-skill history are instant.

### Out (explicitly)
- Other agents (Cursor, Codex, …). The model is agent-agnostic; only the parser is Claude Code-specific. Not before the Claude Code version is loved.
- Hosted, shared or team dashboards. Local only.
- Per-tool-call hooks for exact timings. Transcript timestamps are within ~100 ms and include queue/permission wait, which is what the user feels anyway.
- Automatic diagnosis on every `/zina`. It costs tokens and a minute; the user asks for it.
- Editing SKILL.md from the browser. Edits happen in the session, where permissions and review already exist.
- Windows-specific testing. It should work (Node, no shell tricks) but is not a v1 promise.

### Runtime decision: Node, not Python
v0 is Python. v1 is **Node (plain JavaScript, ES modules, zero dependencies)** because Claude Code itself runs on Node, so Node is present on every machine where the plugin can be installed; Python 3.11+ is not. A plugin that says "first install Python" has already lost the moment described in §1. Port cost: ~900 lines, and the v0 tests become the port's acceptance tests.

## 3. User experience, step by step

### 3.1 Install
```
/plugin marketplace add BoualamHamza/zina
/plugin install zina@zina
```
Done. `claude plugin details zina` shows the projected token cost of the plugin; our target is **< 300 tokens** (one short command description, one agent description, no skill listing bloat). zina must not be the thing zina would flag.

### 3.2 `/zina`
The command file runs a deterministic shell step (no model judgment involved) and the model relays its output:

```
---
description: Open the zina dashboard for this session
---
!node "${CLAUDE_PLUGIN_ROOT}/bin/zina.js" open --session "$CLAUDE_CODE_SESSION_ID" --cwd "$PWD"
Reply with the dashboard URL above, on one line, and nothing else.
```

`zina open`:
1. Finds the server (`~/.zina/server.json` holds `{pid, port}`); starts it detached if absent or dead. Port 7474, next free if taken.
2. Prints `zina → http://127.0.0.1:7474/s/<id>  (live)`.
3. Tries to open the browser (`open` / `xdg-open` / `start`). In remote or headless sessions it just prints the URL.

Idempotent: a second `/zina` prints the same URL. The server exits by itself after 2 h without a request.

### 3.3 The dashboard is live
While the session continues, the page polls `/api/session/<id>` every 3 s. The server re-reads the transcript from the last byte offset, so a 10 MB transcript costs nothing after the first parse. A dot in the header shows *live* / *idle* / *ended*.

### 3.4 `/zina diagnose`
Same command with an argument. It spawns the `zina-diagnoser` agent (model: the session's, effort: medium) with a prompt that includes `zina json --session <id>` output (trimmed to the span and finding level, not raw tool outputs) and the SKILL.md files of the skills seen. The agent:
1. Writes `~/.zina/diagnoses/<session>.md` (the dashboard shows it immediately in the *Diagnosis* panel).
2. Returns a short summary plus, for each skill it wants to change, a unified diff of SKILL.md. The main session shows the diffs; the user accepts with the normal edit permission flow.

Why a subagent and not `claude -p`: it is already authenticated, already in the project, costs the main context only a summary, and its output can turn into edits without leaving the session.

### 3.5 `/zina stop`
Stops the server. Rarely needed; documented for completeness.

## 4. What the dashboard looks like

Design rule: **a page answers one question in its first screen, in sentences, before any chart.** Charts are evidence for the sentences. Tables are for the curious and live at the bottom.

Visual rule: one typeface, numbers tabular, light and dark from system preference, 1200 px max width, works at phone width because people will open it on a phone from the terminal URL. Colours mean one thing each, everywhere: skill = orange, subagent = purple, cache hit = green, cache write = red, harness = grey.

### 4.1 Session page — `/s/<id>` — "What happened in this session, and what should I change?"

**First screen**

1. **Verdict** (3 sentences, generated by rules):
   > 4 turns · 12 min active · $0.90.
   > 60 % of input tokens are a fixed 88k prefix re-read on every call (tool schemas 52k, MCP instructions 11k, skill listing 6k).
   > One cache break cost ~$0.30: the model switch at 20:49.

2. **Where the context is right now** — one horizontal stacked bar of the latest API call's input, segmented and labelled:
   `system prompt 3k │ tools & MCP 62k │ CLAUDE.md + skills 9k │ tool results 71k (keybindings-help 4k, Read 31k, Bash 20k…) │ conversation 27k`
   Hovering a segment shows the top five contributors. This is the chart that makes "context" tangible.

3. **Skills & subagents in this session** — cards, one per skill/agent invocation, sorted by cost:
   ```
   ┌ keybindings-help ─────────────────────────── skill ┐
   │ injected 3.8k tokens · 0 files read · 25 ms         │
   │ ▍ body is 3.8k tokens; 92 % is a reference table.    │
   │   Nothing in it was used in this session.            │
   └───────────────────────────────────────── open ▸ ────┘
   ┌ web-fetch (Explore) ───────────────────── subagent ┐
   │ 180k input · 4 API calls · 5 tools · 56 s           │
   │ ▍ 46k tokens of pages read; the main session paid   │
   │   only 1.4k for the summary. Good delegation.        │
   └───────────────────────────────────────── open ▸ ────┘
   ```
   Each card has one auto-insight line (§5.4). Click → Skill page.

**Second screen**

4. **Timeline** — one row per turn, time within the turn on x; skill spans orange, subagent spans purple, tool calls as ticks sized by tokens returned; idle time between turns not drawn, but printed (`+43 min idle`). Hover = details. Same as v0, tidier.

5. **Context per API call** — stacked bars (cache read / cache write / uncached / output), with a vertical marker and label at each cache break and at each compaction (`PreCompact` is visible in the transcript as a `system` entry).

6. **Findings** — rule-based, each with *what*, *why it matters*, *fix*, and the number that justifies it. Sorted by estimated cost impact, not severity labels.

7. **Diagnosis** — the subagent's markdown if it exists, else: *"Run `/zina diagnose` in your session for a reviewed diagnosis and proposed SKILL.md edits."*

**Below the fold**: sortable tables (spans, tool calls, harness attachments) for people who want the raw numbers. Collapsed by default.

### 4.2 Skill page — `/s/<id>/skill/<span>` — "What did this skill actually do, and did it need to?"

1. **Header**: name, where its SKILL.md lives, body size in tokens, how many times it ran in this project, median cost.

2. **What it touched** — a three-column flow: *skill → tool kinds → targets (files, commands, URLs)*, link width = tokens returned. This is the diagram that was missing from v0. It answers "it read 12 files under `references/` for 31k tokens" at a glance. Targets that the SKILL.md names are marked; targets it did not name are flagged (*the skill improvised*).

3. **Progressive-disclosure check** — the files SKILL.md references vs. the files actually read this session and historically:
   ```
   references/actions.md     named · read in 1/14 sessions · 2.9k tokens
   references/contexts.md    named · never read
   (inline table in SKILL.md) 2.4k tokens · loaded every time
   ```
   This single table usually tells you what to move out of the body.

4. **Direct cost vs. span cost**, side by side and explained: *direct* = body + results of tool calls that touch the skill's own files or files it names; *span* = everything until the span closed (§5.2). When they differ a lot, the page says so: *"most of this span is work the skill triggered, not the skill itself."*

5. **Across sessions**: invocations, median tokens, median wall, p95, and a sparkline. The trend is what tells you a skill is degrading as the repo grows.

6. **Recommendations**: rule-based lines (move the table to a reference file; Grep before Read; this skill always ends with a 20k Bash output: pipe through `head`), then the diagnoser's section for this skill if a diagnosis exists.

### 4.3 Project page — `/` — "Which skills cost me the most, every time?"

- Project name, number of sessions seen, total spend.
- **Skill leaderboard**: name, invocations, median injected tokens, median span tokens, median wall, trend arrow. Click → skill page with the latest session.
- **Sessions**: the last 20, each a one-line verdict and a tiny context sparkline. Click → session page.
- **Fixed prefix over time**: a line of the first-call `cache_creation` per session. When it climbs, you added a plugin or an MCP server; the page names what changed when the attachment types differ.

## 5. The model underneath

### 5.1 Data sources (unchanged from v0)
- `~/.claude/projects/<slug>/<session>.jsonl`: per-API-call `usage`, every `tool_use`/`tool_result`, timestamps, harness attachments with their rendered text, `prompt_snapshot` for the system prompt, `cost-state`.
- `<session>/subagents/agent-<id>.jsonl`: same for each `Agent` call, linked by the `agentId:` line in the parent's `tool_result`.
- SKILL.md files on disk (`.claude/skills/`, `~/.claude/skills/`, plugin roots), to measure body size and list the references they name.

### 5.2 Attribution: when a skill span ends
v0 charged everything until the end of the turn to the skill. The dogfood diagnosis showed this inflates a 4k-token skill to 3.8M. v1 rule, in order:

A skill span opens at the `Skill` call and closes at the **first** of:
1. the next human prompt;
2. the next `Skill` call;
3. the first assistant message with `stop_reason = end_turn` and no `tool_use` after the skill body was injected (Claude reported back: the skill's job is done);
4. a `PreCompact` event.

And every span reports two numbers: **direct** (body + tool calls touching the skill's directory or files it names) and **span** (everything inside). The UI never shows one without the other.

Subagent spans are exact: they are a separate transcript.

### 5.3 Token estimates, calibrated
Tool-result tokens are estimated from characters. v1 calibrates the ratio per session from the transcript's own truth: `cache_creation` of call *n* ≈ tokens added since call *n−1*. A least-squares fit over the session gives chars/token, usually 3.4–4.2. Estimates are then shown with `~` and the fitted ratio is in the footer. API token counts are always exact.

### 5.4 Deterministic insights (the sentences)
Each is a function `session → insight | null`, with a number and a fix. v1 list:

| Insight | Trigger | Fix it suggests |
|---|---|---|
| Fixed prefix | first-call `cache_creation` > 40k | breakdown by attachment type; name the plugins/MCP servers to disable |
| Cache break | `cache_creation` > 20k with `cache_read` < previous read | cause: model switch / TTL expiry (idle gap) / prefix change (attachment diff) |
| Oversized skill body | body > 1.5k tokens, or > 50 % tables/lists | move reference material to a file, Grep it |
| Reference never read | SKILL.md names a file never opened in N sessions | drop it from the body or make it on-demand |
| Read storm | ≥ 5 `Read`/`Grep`/`Glob` calls in one span (Bash excluded) | batch; Grep first; Explore subagent |
| Big result | a single tool result > 8k tokens in the main thread | `offset/limit`, `head`, delegate |
| Serial edits | ≥ 4 `Edit` calls to the same file in consecutive API calls | one response with all edits |
| Good delegation | subagent read > 10× what it returned | (praise; shows the pattern works) |
| Compaction pressure | context > 70 % of the model's window | compact now, before auto-compact picks the moment |

### 5.5 LLM diagnosis
Input: the span/finding-level JSON (no raw tool outputs), the SKILL.md bodies, and the deterministic insights. Prompt asks for: the three biggest sources with numbers; per skill, what it read, whether it needed to, and a SKILL.md rewrite as a diff; what broke the cache. The dogfood run of this prompt produced a review that found three bugs in zina's own rules, so the prompt is kept and the output format is pinned (markdown with fixed headings, so the dashboard can split it per skill).

## 6. Hooks: why only `SessionStart`
Per-tool hooks spawn a process twice per tool call (~100 ms each with Node startup). zina would slow down the thing it measures, and the transcript already carries `tool_use` and `tool_result` timestamps. `SessionStart` is kept because it costs nothing per tool and gives `/zina` a reliable "current session" even when several sessions share a project. `PreCompact` is a candidate for v1.1 (to snapshot the context breakdown before it is summarised) once we see whether users ask for it.

## 7. Repository layout (v1)

```
zina/
├── .claude-plugin/
│   ├── plugin.json            name, version, description (short), author
│   └── marketplace.json       so the repo is its own marketplace
├── commands/zina.md           /zina [diagnose|stop]
├── agents/zina-diagnoser.md   the diagnosis subagent
├── hooks/hooks.json           SessionStart → bin/zina.js register
├── bin/zina.js                CLI entry (open, serve, stop, json, register)
├── src/
│   ├── transcript.js          JSONL → model (port of v0, same tests)
│   ├── spans.js               attribution rules (§5.2)
│   ├── calibrate.js           chars/token fit (§5.3)
│   ├── insights.js            deterministic sentences (§5.4)
│   ├── skills.js              SKILL.md discovery, body size, named references
│   ├── cache.js               ~/.zina/cache, incremental parse by byte offset
│   └── server.js              http.server, /api/*, static dashboard
├── dashboard/                 index.html, session.html, skill.html, app.js, style.css
├── test/                      node:test, fixtures incl. a real anonymised transcript
├── docs/PLAN.md               this file
└── legacy/python/             v0, kept until the port passes the same tests
```

## 8. Milestones

Each milestone ends with something a user can run.

1. **Plugin skeleton + `/zina` opens a page** — plugin.json, command, SessionStart hook, `bin/zina.js open` starting a server that serves a static "hello, session <id>" page with the session's turn count. *Proves the install → `/zina` → browser loop with zero setup.*
2. **Parser port + Session page** — `transcript.js` passing the v0 tests, span rules of §5.2, calibration, Session page sections 1–5 live-refreshing.
3. **Insights + Findings** — §5.4 rules, verdict sentences, skill cards with their one-liners.
4. **Skill page** — flow diagram, progressive-disclosure check, direct vs span, per-skill history from the cache.
5. **`/zina diagnose`** — agent definition, prompt, dashboard Diagnosis panel, diff proposals in-session.
6. **Project page + polish** — leaderboard, sessions list, prefix-over-time, phone width, dark mode, README with a 20-second GIF.
7. **Dogfood on a real large repo** (not this one) — fix what the attribution gets wrong, then tag `v1.0.0`.

## 9. How we know v1 is good

- Install to first useful sentence: **under 2 minutes**, no step outside Claude Code.
- `/zina` adds **< 300 tokens** to the session; the plugin's own listed cost **< 300 tokens**.
- A 10 MB transcript renders in **< 1 s** after first parse; first parse **< 3 s**.
- Zero added latency on tool calls (no per-tool hooks).
- On the dogfood repo, every insight on the first screen is **true and actionable**: a user who follows the fixes sees the fixed prefix or the skill cost drop in the next session, and the project page shows it.
- The diagnoser's SKILL.md diffs are accepted as-is at least half the time.

## 10. Open questions (decide during milestone 1–2, not now)

- Port number and discovery: fixed 7474 vs. per-project ports. Leaning fixed, one server for all projects, pages keyed by session id.
- Whether the Session page's verdict should mention money. Cost is in the transcript (`cost-state`) but is not available for every account type; fall back to tokens.
- How to show a session that has been compacted: the transcript keeps the originals, the model does not. Probably a marker in the timeline and a "before/after compaction" toggle on the context bar.
- Plugin name collision: confirm `zina` is free on the public marketplace index before `v1.0.0`.
