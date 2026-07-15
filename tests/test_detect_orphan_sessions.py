#!/usr/bin/env python3
"""test_detect_orphan_sessions.py — board #588.

Covers the classification logic in tools/detect_orphan_sessions.py against
two fixtures:
  - tests/fixtures/healthy_fleet.json — mirrors a subset of the REAL live
    Mac ps snapshot taken 2026-07-15 (3 claude --channels sessions, 6
    bun server.ts pollers, every poller's ancestor chain reaches a live
    claude process). Expected: zero orphans.
  - tests/fixtures/orphaned_pollers.json — healthy chains plus two
    synthetic `bun server.ts` reparented directly to launchd (PID 1, no
    claude anywhere upstream), modeling the 2026-07-07 incident (2 orphans
    hot-spinning ~94% CPU each). Expected: exactly those two flagged.

The hard requirement from the card: a live-parent poller must NEVER be
classified as an orphan. That is asserted directly, not just implied by
the fixture-level counts, so a future change to the classifier that starts
being trigger-happy fails loudly here.

Run: python3 tests/test_detect_orphan_sessions.py
Exits 0 on pass, 1 on any failure (unittest default).
"""
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import detect_orphan_sessions as dos  # noqa: E402


FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        data = json.load(f)
    return [dos.ProcRow(pid=d["pid"], ppid=d["ppid"], pcpu=d.get("pcpu", 0.0), command=d["command"]) for d in data]


class TestHealthyFleet(unittest.TestCase):
    def setUp(self):
        self.rows = _load("healthy_fleet.json")
        self.verdicts = dos.run(self.rows)

    def test_finds_all_six_pollers(self):
        self.assertEqual(len(self.verdicts), 6)

    def test_zero_orphans_on_healthy_fleet(self):
        orphans = [v for v in self.verdicts if v.orphaned]
        self.assertEqual(orphans, [], f"false positive(s) on a healthy fleet: {orphans}")

    def test_every_healthy_poller_has_live_claude_in_chain_reason(self):
        # Every single poller in this fixture has an intact 3-level chain
        # (server.ts -> bun run wrapper -> live claude). Assert the
        # classifier actually found that, not just that it defaulted to
        # "not orphaned" for an unrelated/unknown reason.
        for v in self.verdicts:
            self.assertFalse(v.orphaned)
            self.assertIn("live claude process found", v.reason, msg=f"pid={v.pid} reason={v.reason!r}")


class TestOrphanedFixture(unittest.TestCase):
    def setUp(self):
        self.rows = _load("orphaned_pollers.json")
        self.verdicts = dos.run(self.rows)

    def test_finds_all_four_pollers(self):
        self.assertEqual(len(self.verdicts), 4)

    def test_exactly_two_orphans_detected(self):
        orphans = {v.pid for v in self.verdicts if v.orphaned}
        self.assertEqual(orphans, {9101, 9202})

    def test_healthy_pollers_in_same_snapshot_not_flagged(self):
        # The orphan fixture ALSO contains two genuinely healthy pollers
        # (1636, 1882) sitting right next to the orphans. This is the
        # sharpest test of the "never misclassify a live-parent poller"
        # requirement: healthy and orphaned processes coexist in one
        # snapshot and must be told apart correctly.
        by_pid = {v.pid: v for v in self.verdicts}
        self.assertFalse(by_pid[1636].orphaned, by_pid[1636].reason)
        self.assertFalse(by_pid[1882].orphaned, by_pid[1882].reason)

    def test_orphan_reason_mentions_launchd(self):
        by_pid = {v.pid: v for v in self.verdicts}
        for pid in (9101, 9202):
            self.assertIn("launchd", by_pid[pid].reason.lower())

    def test_orphan_ppid_is_one(self):
        by_pid = {v.pid: v for v in self.verdicts}
        for pid in (9101, 9202):
            self.assertEqual(by_pid[pid].ppid, 1)


class TestNeverMisclassifiesLiveParent(unittest.TestCase):
    """Card-mandated guarantee, exercised directly against the
    classify_poller() function rather than through a full fixture, so the
    test doesn't silently stop covering this if fixtures change shape."""

    def test_live_parent_never_orphaned_even_with_high_cpu(self):
        rows = [
            dos.ProcRow(pid=1, ppid=0, pcpu=0.0, command="/sbin/launchd"),
            dos.ProcRow(pid=500, ppid=1, pcpu=0.0, command="claude --channels plugin:telegram@x"),
            dos.ProcRow(pid=501, ppid=500, pcpu=0.0, command="bun run --cwd /plugins/telegram/0.0.6 start"),
            # deliberately pathological CPU value: a busy-but-legitimately-
            # owned poller must still never be flagged as an orphan. CPU is
            # not part of the classification signal at all.
            dos.ProcRow(pid=502, ppid=501, pcpu=99.9, command="/Users/joris/.bun/bin/bun server.ts"),
        ]
        by_pid = dos._by_pid(rows)
        poller = by_pid[502]
        verdict = dos.classify_poller(poller, by_pid)
        self.assertFalse(verdict.orphaned, verdict.reason)

    def test_missing_parent_in_snapshot_is_unknown_not_orphaned(self):
        # Simulates a race: ps snapshot caught the poller but the parent
        # row fell out (process exited between the two reads, or a ps
        # parsing edge case). Must default to "not orphaned".
        rows = [
            dos.ProcRow(pid=1, ppid=0, pcpu=0.0, command="/sbin/launchd"),
            dos.ProcRow(pid=999, ppid=888, pcpu=50.0, command="/Users/joris/.bun/bin/bun server.ts"),
        ]
        by_pid = dos._by_pid(rows)
        verdict = dos.classify_poller(by_pid[999], by_pid)
        self.assertFalse(verdict.orphaned, verdict.reason)
        self.assertIn("UNKNOWN", verdict.reason)

    def test_genuine_orphan_still_detected(self):
        # Sanity check the negative-guarantee tests above aren't just
        # trivially permissive — a real orphan in isolation must still
        # be flagged.
        rows = [
            dos.ProcRow(pid=1, ppid=0, pcpu=0.0, command="/sbin/launchd"),
            dos.ProcRow(pid=7000, ppid=1, pcpu=94.0, command="/Users/joris/.bun/bin/bun server.ts"),
        ]
        by_pid = dos._by_pid(rows)
        verdict = dos.classify_poller(by_pid[7000], by_pid)
        self.assertTrue(verdict.orphaned, verdict.reason)


class TestFindPollers(unittest.TestCase):
    def test_ignores_non_poller_bun_processes(self):
        rows = [
            dos.ProcRow(pid=1, ppid=0, pcpu=0.0, command="/sbin/launchd"),
            dos.ProcRow(pid=2, ppid=1, pcpu=0.0, command="bun run --cwd /plugins/telegram/0.0.6 start"),
            dos.ProcRow(pid=3, ppid=2, pcpu=0.0, command="/Users/joris/.bun/bin/bun server.ts"),
        ]
        pollers = dos.find_pollers(rows)
        self.assertEqual([p.pid for p in pollers], [3])

    def test_empty_snapshot_yields_no_pollers_no_crash(self):
        self.assertEqual(dos.run([]), [])


if __name__ == "__main__":
    unittest.main()
