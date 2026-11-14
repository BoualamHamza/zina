"""Rule-based findings over a parsed session.

These are deterministic heuristics; `zina diagnose` can hand the same data to
Claude for deeper suggestions. Each finding: severity, title, detail, span.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .model import Session

BIG_RESULT_TOKENS = 8_000
MANY_SEQ_READS = 5
CACHE_MISS_TOKENS = 20_000
ATTACHMENT_NOISE_TOKENS = 3_000


def findings(s: Session) -> list[dict]:
    out: list[dict] = []
    spans = s.spans

    # 1. Large tool results injected into context.
    for tc in s.tool_calls:
        if tc.result_tokens >= BIG_RESULT_TOKENS and tc.name != "Agent":
            sp = spans.get(tc.span_id)
            in_sub = bool(sp and sp.depth > 0)
            out.append({
                "severity": "medium" if in_sub else "high",
                "title": f"{tc.name} injected ~{tc.result_tokens:,} tokens" + (" (inside subagent)" if in_sub else ""),
                "detail": f"`{tc.summary()}` returned {tc.result_chars:,} chars. " + (
                    "It stays in the subagent's context, so the main session only pays for the summary. Fine."
                    if in_sub else
                    "Every later API call in the session pays for this until compaction. Narrow the read "
                    "(offset/limit, Grep) or delegate it to a subagent so only the summary comes back."),
                "span": sp.name if sp else "",
                "span_id": tc.span_id,
            })

    # 2. Sequential Read/Bash-cat storms within one span.
    by_span: dict[str, list] = defaultdict(list)
    for tc in s.tool_calls:
        by_span[tc.span_id].append(tc)
    for sid, calls in by_span.items():
        reads = [c for c in calls if c.name in ("Read", "Glob", "Grep") or
                 (c.name == "Bash" and any(k in str(c.input.get("command", "")) for k in ("cat ", "head ", "sed -n")))]
        if len(reads) >= MANY_SEQ_READS:
            sp = spans.get(sid)
            toks = sum(c.result_tokens for c in reads)
            out.append({
                "severity": "medium",
                "title": f"{len(reads)} file-read calls in {sp.name if sp else sid} (~{toks:,} tokens)",
                "detail": "Many small reads cost one API round-trip each and accumulate in context. "
                          "Batch independent reads in one response, use Grep to locate before reading, "
                          "or move exploration into an Explore subagent.",
                "span": sp.name if sp else "",
                "span_id": sid,
            })

    # 3. Prompt-cache misses: cache_creation spikes after the first call.
    calls = sorted(s.api_calls, key=lambda c: c.ts)
    for i, c in enumerate(calls[1:], start=1):
        if c.cache_creation >= CACHE_MISS_TOKENS:
            prev = calls[i - 1]
            sp = spans.get(c.span_id)
            # Was it caused by a big tool result right before?
            recent = [t for t in s.tool_calls if t.ended_at and prev.ts <= t.ended_at <= c.ts]
            injected = sum(t.result_tokens for t in recent)
            gap_s = (c.ts - prev.ts).total_seconds()
            if c.model != prev.model:
                cause = f"model switched {prev.model} -> {c.model}; the prompt cache is per model, so the whole prefix was rewritten"
            elif injected > c.cache_creation * 0.5:
                cause = f"{injected:,} tokens of tool results landed just before it (expected growth, not a miss)"
            elif gap_s > 3600:
                cause = f"{gap_s / 60:.0f} min idle since the previous call; the cache TTL expired"
            elif gap_s > 300:
                cause = f"{gap_s / 60:.0f} min idle since the previous call; a 5-min cache would have expired"
            else:
                cause = ("no matching tool-result volume: likely a prefix change (new system-reminder, "
                         "hook output, tool list or effort change)")
            out.append({
                "severity": "medium" if c.cache_creation < 60_000 else "high",
                "title": f"Cache write of {c.cache_creation:,} tokens at {c.ts.strftime('%H:%M:%S')}",
                "detail": f"cache_read={c.cache_read:,}, cache_creation={c.cache_creation:,}. Cause: {cause}.",
                "span": sp.name if sp else "",
                "span_id": c.span_id,
            })

    # 4. Harness attachments (system-reminders etc.) volume.
    att_by_type = Counter()
    for a in s.attachments:
        att_by_type[a.type] += a.chars // 4
    noisy = {k: v for k, v in att_by_type.items() if v >= ATTACHMENT_NOISE_TOKENS}
    if noisy:
        top = ", ".join(f"{k} ~{v:,}" for k, v in sorted(noisy.items(), key=lambda kv: -kv[1])[:5])
        out.append({
            "severity": "low",
            "title": f"Harness attachments ~{sum(att_by_type.values()):,} tokens over the session",
            "detail": f"Largest: {top}. These are injected by Claude Code (hooks, MCP instructions, "
                      f"deferred tool lists). Trim hook output and MCP server instructions where you control them.",
            "span": "", "span_id": "",
        })

    # 5. Slow tools.
    for tc in s.tool_calls:
        if tc.duration_ms >= 60_000 and tc.name not in ("Agent",):
            sp = spans.get(tc.span_id)
            out.append({
                "severity": "low",
                "title": f"{tc.name} took {tc.duration_ms / 1000:.0f}s",
                "detail": f"`{tc.summary()}`. Wall time, not tokens: consider running it in the background.",
                "span": sp.name if sp else "", "span_id": tc.span_id,
            })

    # 6. Skills that consume a lot relative to the rest.
    stats = {r["id"]: r for r in s.span_stats()}
    tot_in = sum(r["input_tokens"] + r["cache_creation"] + r["cache_read"] for r in stats.values()) or 1
    for r in stats.values():
        if r["kind"] != "skill":
            continue
        share = (r["input_tokens"] + r["cache_creation"] + r["cache_read"]) / tot_in
        if share >= 0.4 and r["api_calls"] >= 3:
            out.append({
                "severity": "medium",
                "title": f"Skill `{r['name']}` accounts for {share:.0%} of input tokens",
                "detail": f"{r['api_calls']} API calls, {r['tool_calls']} tool calls, "
                          f"~{r['tool_result_tokens']:,} tokens of tool results. "
                          f"Check whether SKILL.md tells Claude to read files it doesn't need (progressive disclosure).",
                "span": r["name"], "span_id": r["id"],
            })

    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda f: order[f["severity"]])
    return out
