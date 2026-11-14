"""Parser tests on a synthetic transcript shaped like Claude Code 2.x output."""
import json
import unittest
from pathlib import Path
import tempfile

from zina.transcript import parse_session
from zina.analyze import findings
from zina.report import render


from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ts(sec: float) -> str:
    return (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int((sec % 1) * 1000):03d}Z"


def user(pid, content, t, **kw):
    return {"type": "user", "promptId": pid, "uuid": f"u{t}", "timestamp": ts(t),
            "message": {"role": "user", "content": content}, "sessionId": "s1", "version": "2.1.0", **kw}


def assistant(mid, blocks, t, usage, model="claude-test"):
    return {"type": "assistant", "uuid": f"a{t}", "timestamp": ts(t),
            "message": {"id": mid, "model": model, "role": "assistant", "content": blocks, "usage": usage}}


def tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid, text, is_error=False):
    return [{"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}], "is_error": is_error}]


U = lambda cr=1000, cc=0, i=5, o=50: {"input_tokens": i, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc, "output_tokens": o}  # noqa: E731


def build_session(tmp: Path):
    main = tmp / "sess.jsonl"
    sub = tmp / "sess" / "subagents"
    sub.mkdir(parents=True)
    lines = [
        # Turn 1: plain read.
        user("p1", "hello", 0),
        assistant("m1", [tool_use("t1", "Read", {"file_path": "a.py"})], 1, U(cr=0, cc=5000)),
        assistant("m1", [{"type": "text", "text": "dup usage line"}], 1.1, U(cr=0, cc=5000)),
        user("p1", tool_result("t1", "x" * 40000), 2),  # 10k tokens -> high finding
        assistant("m2", [{"type": "text", "text": "done"}], 3, U(cr=5000, cc=10000)),
        # Turn 2: skill + agent.
        user("p2", "use the skill", 60),
        assistant("m3", [tool_use("t2", "Skill", {"skill": "my-skill"})], 61, U()),
        user("p2", tool_result("t2", "Launching skill: my-skill"), 61.5),
        user("p2", "# My skill\n" + "body " * 2000, 61.6),  # injected SKILL.md body
        assistant("m4", [tool_use("t3", "Agent", {"subagent_type": "Explore", "prompt": "look"})], 62, U()),
        user("p2", tool_result("t3", "report...\nagentId: abc123 (use SendMessage)"), 70),
        assistant("m5", [{"type": "text", "text": "ok"}], 71, U(cr=2000, cc=30000)),
        {"type": "cost-state", "totalCostUSD": 1.25},
    ]
    main.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    sub_lines = [
        user("p2", "look", 62.1, isSidechain=True, agentId="abc123"),
        assistant("s1", [tool_use("st1", "Grep", {"pattern": "foo"})], 63, U(cr=0, cc=8000)),
        user("p2", tool_result("st1", "match"), 64, isSidechain=True, agentId="abc123"),
        assistant("s2", [{"type": "text", "text": "report"}], 69, U(cr=8000, cc=100)),
    ]
    (sub / "agent-abc123.jsonl").write_text("\n".join(json.dumps(x) for x in sub_lines) + "\n")
    return main


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.s = parse_session(build_session(self.tmp))

    def test_turns_and_durations(self):
        self.assertEqual(len(self.s.turns), 2)
        t1, t2 = self.s.turns
        # Turn ends at last activity (3s), not at next prompt (60s).
        self.assertEqual(t1.duration_ms, 3000)
        self.assertEqual(t2.duration_ms, 11000)

    def test_usage_deduped_by_message_id(self):
        ids = [c.id for c in self.s.api_calls]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len([c for c in self.s.api_calls if c.id == "m1"]), 1)
        self.assertEqual(self.s.tokens()["cache_creation"], 5000 + 10000 + 30000 + 8000 + 100)

    def test_skill_span_gets_body_and_following_calls(self):
        skill = [sp for sp in self.s.spans.values() if sp.kind == "skill"][0]
        self.assertEqual(skill.name, "my-skill")
        call = [t for t in self.s.tool_calls if t.name == "Skill"][0]
        self.assertGreater(call.result_tokens, 2000)  # body counted, not just "Launching skill"
        stats = {r["id"]: r for r in self.s.span_stats()}[skill.id]
        self.assertEqual(stats["api_calls"], 2)  # m4, m5 happen after the skill call
        self.assertIn("Agent", stats["tools"])

    def test_agent_linked_to_child_transcript(self):
        agent = [sp for sp in self.s.spans.values() if sp.kind == "agent"][0]
        self.assertEqual(agent.agent_id, "abc123")
        self.assertEqual(agent.name, "Explore")
        stats = {r["id"]: r for r in self.s.span_stats()}[agent.id]
        self.assertEqual(stats["api_calls"], 2)
        self.assertEqual(stats["tool_calls"], 1)
        self.assertEqual(agent.duration_ms, 8000)  # tool_use at 62 -> result at 70

    def test_tool_durations(self):
        read = [t for t in self.s.tool_calls if t.name == "Read"][0]
        self.assertEqual(read.duration_ms, 1000)
        self.assertEqual(read.result_tokens, 10000)

    def test_findings(self):
        titles = [f["title"] for f in findings(self.s)]
        self.assertTrue(any("Read injected" in t for t in titles))
        self.assertTrue(any("Cache write of 30,000" in t for t in titles))

    def test_render_embeds_data(self):
        html = render(self.s, [])
        self.assertIn("my-skill", html)
        self.assertNotIn("/*__ZINA_DATA__*/null", html)
        self.assertEqual(self.s.cost_usd, 1.25)


if __name__ == "__main__":
    unittest.main()
