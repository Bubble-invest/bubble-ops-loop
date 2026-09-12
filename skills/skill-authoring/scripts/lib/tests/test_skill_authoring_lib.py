#!/usr/bin/env python3
"""Unit tests for the skill-authoring evidence collectors (#1222).

Self-contained (stdlib unittest + tempdir fixtures) — no live corpus, no live
`claude`, no network. Run:  python3 -m unittest discover -s scripts/lib/tests
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB))

import skill_usage_count as suc  # noqa: E402
import list_candidates as lc  # noqa: E402
import eval_harness as eh  # noqa: E402


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def _skill_line(name: str, ts: str | None) -> dict:
    line = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": name, "args": "x"}}
    ]}}
    if ts:
        line["timestamp"] = ts
    return line


class TestUsageCount(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.corpus = self.tmp / "-home-claude-agents-tony"
        self.corpus.mkdir()
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)

    def test_counts_and_windows(self):
        recent = (self.now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
        old = (self.now - timedelta(days=90)).isoformat().replace("+00:00", "Z")
        _write_jsonl(self.corpus / "s1.jsonl", [
            _skill_line("rnd_loop", recent),
            _skill_line("rnd_loop", recent),
            _skill_line("auth", recent),
            _skill_line("rnd_loop", old),   # outside 45d window → excluded
        ])
        stats = suc.count_usage([self.corpus], window_days=45, now=self.now)
        self.assertEqual(stats["rnd_loop"]["count"], 2)  # old one excluded
        self.assertEqual(stats["auth"]["count"], 1)
        self.assertEqual(stats["rnd_loop"]["last_used"][:10], recent[:10])

    def test_no_timestamp_is_counted_not_dropped(self):
        _write_jsonl(self.corpus / "s2.jsonl", [_skill_line("mystery", None)])
        stats = suc.count_usage([self.corpus], window_days=45, now=self.now)
        self.assertEqual(stats["mystery"]["count"], 1)
        self.assertIsNone(stats["mystery"]["last_used"])

    def test_registry_zero_use_appears(self):
        _write_jsonl(self.corpus / "s3.jsonl", [_skill_line("used_skill", None)])
        reg = self.tmp / "skills"
        (reg / "used_skill").mkdir(parents=True)
        (reg / "used_skill" / "SKILL.md").write_text("x")
        (reg / "dead_skill").mkdir(parents=True)
        (reg / "dead_skill" / "SKILL.md").write_text("x")
        report = suc.build_report([self.corpus], [str(reg)], window_days=45, now=self.now)
        self.assertEqual(report["skills"]["dead_skill"]["count"], 0)
        self.assertTrue(report["skills"]["dead_skill"]["registered"])
        self.assertEqual(report["skills"]["used_skill"]["count"], 1)

    def test_malformed_lines_ignored(self):
        (self.corpus / "bad.jsonl").write_text('not json\n{"message":123}\n"Skill"\n')
        stats = suc.count_usage([self.corpus], window_days=45, now=self.now)
        self.assertEqual(stats, {})

    def test_parse_ts(self):
        self.assertIsNone(suc.parse_ts(None))
        self.assertIsNone(suc.parse_ts("garbage"))
        self.assertIsNotNone(suc.parse_ts("2026-08-25T15:08:55.971Z"))


class TestListCandidates(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.cdir = self.tmp / "skill-updates"
        self.cdir.mkdir()

    def test_iso_week_stamp_format(self):
        stamp = lc.iso_week_stamp(datetime(2026, 7, 20, tzinfo=timezone.utc))
        self.assertRegex(stamp, r"^\d{4}-W\d{2}$")

    def test_locate_exact_then_fallback(self):
        week = "2026-W37"
        (self.cdir / lc.candidates_filename(week)).write_text("### 1. foo — MISSING\nbody")
        path, note = lc.locate_candidates_file(str(self.cdir), week)
        self.assertIsNotNone(path)
        self.assertIn("this-week", note)
        # fallback: ask for a different week → returns the existing file w/ note
        path2, note2 = lc.locate_candidates_file(str(self.cdir), "2026-W40")
        self.assertIsNotNone(path2)
        self.assertIn("ABSENT", note2)

    def test_missing_dir(self):
        path, note = lc.locate_candidates_file(str(self.tmp / "nope"), "2026-W37")
        self.assertIsNone(path)
        self.assertIn("does not exist", note)

    def test_split_blocks(self):
        text = ("preamble\n### 1. a — MISSING\nx\n### 2. b — EXTEND\ny\n")
        blocks = lc.split_candidate_blocks(text)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0].startswith("### 1."))
        self.assertEqual(lc.split_candidate_blocks(""), [])

    def test_build_report_and_registry(self):
        week = "2026-W37"
        (self.cdir / lc.candidates_filename(week)).write_text("### 1. foo — MISSING\nq")
        reg = self.tmp / "skills"
        (reg / "auth").mkdir(parents=True)
        (reg / "auth" / "SKILL.md").write_text("---\nname: auth\ndescription: do auth things\n---\nbody")
        rep = lc.build_report(str(self.cdir), [str(reg)], week)
        self.assertTrue(rep["found"])
        self.assertEqual(rep["candidate_count"], 1)
        self.assertEqual(rep["registered_skills"][0]["name"], "auth")
        self.assertIn("auth things", rep["registered_skills"][0]["description"])


class TestEvalHarness(unittest.TestCase):
    def test_dry_run_plans_without_calling(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        draft = tmp / "SKILL.md"
        draft.write_text("---\nname: t\ndescription: d\n---\nknow-how")
        probes = [{"id": "p1", "prompt": "do the thing"}]
        res = eh.eval_draft(str(draft), probes, "haiku", str(tmp / "out"), dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(len(res["pairs"]), 1)
        self.assertTrue(res["pairs"][0]["planned"])
        # dry-run must not create output files
        self.assertFalse((tmp / "out").exists())

    def test_load_probes(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        pf = tmp / "p.json"
        pf.write_text(json.dumps([{"id": "a", "prompt": "x"}, {"prompt": "y"}, {"bad": 1}]))
        probes = eh.load_probes(str(pf))
        self.assertEqual(len(probes), 2)  # third has no prompt → dropped
        self.assertEqual(probes[0]["id"], "a")
        self.assertEqual(eh.load_probes(None), [])

    def test_build_cmd_with_and_without(self):
        without = eh.build_claude_cmd("haiku", "P", None)
        withc = eh.build_claude_cmd("haiku", "P", "SYS")
        self.assertNotIn("--append-system-prompt", without)
        self.assertIn("--append-system-prompt", withc)
        self.assertEqual(withc[-1], "P")


if __name__ == "__main__":
    unittest.main()
