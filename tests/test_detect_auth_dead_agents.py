#!/usr/bin/env python3
"""test_detect_auth_dead_agents.py — board #832.

Covers tools/detect_auth_dead_agents.py against fixture transcripts under
tests/fixtures/:

  - healthy_old_auth_failures.jsonl — 34 auth-failure lines (mirrors the
    exact count measured on the real M1 incident transcript) followed by
    500 lines of normal healthy operation. This is THE regression that
    matters most per the card: it's the false-positive that would mute a
    real alert if the detector ever regressed to whole-file grep. Expected:
    HEALTHY.
  - currently_dead.jsonl — 100 healthy lines, then the last 20 lines are
    all auth failures (agent died and every turn since has failed).
    Expected: DEAD.
  - never_failed.jsonl — 250 lines, zero auth-failure strings anywhere.
    Expected: HEALTHY.
  - empty_transcript.jsonl — zero-byte file. Expected: HEALTHY (documented
    conservative default — see classify_transcript docstring).
  - a missing path (no fixture file — constructed in the test itself).
    Expected: UNKNOWN status text but still HEALTHY-equivalent for the
    non-alerting purpose (does not raise, does not report DEAD).

Run: python3 tests/test_detect_auth_dead_agents.py
Exits 0 on pass, 1 on any failure (unittest default).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import detect_auth_dead_agents as dad  # noqa: E402


FIXTURES = Path(REPO_ROOT) / "tests" / "fixtures"


class TestHealthyWithOldAuthFailures(unittest.TestCase):
    """The single most important test in this suite: a re-authed agent
    whose transcript still contains its historical failures must NOT
    alarm forever. This is the exact false-positive that would have muted
    the real alert and reproduced the #832 outage."""

    def test_reports_healthy_not_dead(self):
        v = dad.classify_transcript(FIXTURES / "healthy_old_auth_failures.jsonl", agent_name="miranda-m1")
        self.assertEqual(v.status, "HEALTHY", msg=f"reason={v.reason!r}")

    def test_matched_signatures_empty(self):
        v = dad.classify_transcript(FIXTURES / "healthy_old_auth_failures.jsonl", agent_name="miranda-m1")
        self.assertEqual(v.matched_signatures, [])

    def test_scanned_exactly_the_default_window(self):
        v = dad.classify_transcript(FIXTURES / "healthy_old_auth_failures.jsonl", agent_name="miranda-m1")
        self.assertEqual(v.lines_scanned, dad.DEFAULT_WINDOW_LINES)


class TestCurrentlyDead(unittest.TestCase):
    def test_reports_dead(self):
        v = dad.classify_transcript(FIXTURES / "currently_dead.jsonl", agent_name="test-agent")
        self.assertEqual(v.status, "DEAD", msg=f"reason={v.reason!r}")

    def test_matched_signature_present(self):
        v = dad.classify_transcript(FIXTURES / "currently_dead.jsonl", agent_name="test-agent")
        self.assertIn("Please run /login", v.matched_signatures)

    def test_hit_lines_nonempty(self):
        v = dad.classify_transcript(FIXTURES / "currently_dead.jsonl", agent_name="test-agent")
        self.assertTrue(v.hit_lines)

    def test_remediation_language_in_reason(self):
        v = dad.classify_transcript(FIXTURES / "currently_dead.jsonl", agent_name="test-agent")
        self.assertIn("/login", v.reason)


class TestNeverFailed(unittest.TestCase):
    def test_reports_healthy(self):
        v = dad.classify_transcript(FIXTURES / "never_failed.jsonl", agent_name="test-agent")
        self.assertEqual(v.status, "HEALTHY", msg=f"reason={v.reason!r}")
        self.assertEqual(v.matched_signatures, [])


class TestEmptyTranscript(unittest.TestCase):
    def test_empty_file_is_healthy_not_crash(self):
        v = dad.classify_transcript(FIXTURES / "empty_transcript.jsonl", agent_name="test-agent")
        self.assertEqual(v.status, "HEALTHY")
        self.assertEqual(v.lines_scanned, 0)


class TestMissingTranscript(unittest.TestCase):
    def test_missing_file_reports_unknown_healthy_no_crash(self):
        v = dad.classify_transcript(Path("/nonexistent/path/does-not-exist.jsonl"), agent_name="ghost")
        # Documented conservative default: unknown => not DEAD (never alerts
        # on evidence we don't have).
        self.assertEqual(v.status, "UNKNOWN")
        self.assertNotEqual(v.status, "DEAD")


class TestUnreadableTranscript(unittest.TestCase):
    def test_unreadable_file_reports_unknown_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "locked.jsonl"
            p.write_text("Please run /login\n")
            os.chmod(p, 0o000)
            try:
                v = dad.classify_transcript(p, agent_name="locked-agent")
            finally:
                os.chmod(p, 0o644)  # so tempdir cleanup can remove it
        # root/sandboxed test runners can sometimes still read a 0o000 file;
        # only assert the no-crash + non-DEAD guarantee, not which branch fired.
        self.assertNotEqual(v.status, "DEAD")


class TestAllFourSignaturesDetected(unittest.TestCase):
    """Board #832 explicitly measured that `authentication_error` ALONE
    scored 0 hits on the real incident. Assert each of the four documented
    signatures is independently detected, so a future edit can't silently
    drop one and still pass the suite."""

    def _fixture_with_line(self, text):
        import json
        td = tempfile.mkdtemp()
        p = Path(td) / "sig.jsonl"
        rec = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        p.write_text(json.dumps(rec) + "\n")
        return p

    def test_please_run_login(self):
        p = self._fixture_with_line("Please run /login to continue.")
        v = dad.classify_transcript(p, agent_name="x")
        self.assertEqual(v.status, "DEAD")
        self.assertIn("Please run /login", v.matched_signatures)

    def test_authentication_error(self):
        p = self._fixture_with_line("authentication_error: token invalid")
        v = dad.classify_transcript(p, agent_name="x")
        self.assertEqual(v.status, "DEAD")
        self.assertIn("authentication_error", v.matched_signatures)

    def test_oauth_token_expired(self):
        p = self._fixture_with_line("OAuth token has expired, please reauthenticate")
        v = dad.classify_transcript(p, agent_name="x")
        self.assertEqual(v.status, "DEAD")
        self.assertIn("OAuth token has expired", v.matched_signatures)

    def test_invalid_api_key(self):
        p = self._fixture_with_line("Invalid API key provided")
        v = dad.classify_transcript(p, agent_name="x")
        self.assertEqual(v.status, "DEAD")
        self.assertIn("Invalid API key", v.matched_signatures)


class TestFindLiveTranscript(unittest.TestCase):
    """The 'newest .jsonl by mtime, not by filename' heuristic — a resumed
    (--continue --fork-session) session starts a new file; mtime is what
    tracks which one is still being written."""

    def test_picks_most_recently_modified_file(self):
        with tempfile.TemporaryDirectory() as td:
            pd = Path(td)
            old = pd / "old-session-uuid.jsonl"
            new = pd / "new-session-uuid.jsonl"
            old.write_text("old content\n")
            import time
            time.sleep(0.05)
            new.write_text("new content\n")
            picked = dad.find_live_transcript(pd)
            self.assertEqual(picked, new)

    def test_no_jsonl_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(dad.find_live_transcript(Path(td)))

    def test_missing_dir_returns_none(self):
        self.assertIsNone(dad.find_live_transcript(Path("/nonexistent/project/dir")))


class TestFleetScanNeverCrashesOnMixedInput(unittest.TestCase):
    def test_scan_across_healthy_dead_and_empty_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            pd = Path(td)

            healthy_dir = pd / "-Users-x-healthy-agent"
            healthy_dir.mkdir()
            (healthy_dir / "s1.jsonl").write_bytes(
                (FIXTURES / "never_failed.jsonl").read_bytes()
            )

            dead_dir = pd / "-Users-x-dead-agent"
            dead_dir.mkdir()
            (dead_dir / "s1.jsonl").write_bytes(
                (FIXTURES / "currently_dead.jsonl").read_bytes()
            )

            empty_dir = pd / "-Users-x-no-sessions-yet"
            empty_dir.mkdir()  # no .jsonl at all

            project_dirs = dad.discover_project_dirs(pd)
            self.assertEqual(len(project_dirs), 3)

            verdicts = dad.run_fleet_scan(project_dirs)
            by_name = {v.agent_name: v for v in verdicts}
            self.assertEqual(by_name["-Users-x-healthy-agent"].status, "HEALTHY")
            self.assertEqual(by_name["-Users-x-dead-agent"].status, "DEAD")
            self.assertEqual(by_name["-Users-x-no-sessions-yet"].status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
