"""Tests for Layer 1's independent data-source primitives (framework copy).

Joris flag 2026-06-01: "Export from dept shouldn't be your only data."
Layer 1 (morning brief) must cross-check each child dept against signals the
child does NOT author about itself, so a dead runtime shipping a stale "clean"
export gets caught. The deterministic decision rule (classify_liveness) and its
gathering wrapper (dept_liveness — export mtime + git commit recency) are pure
framework primitives, unit-tested here.

The PROMPT.md-wiring assertions (that a dept's layers/1/PROMPT.md mentions
notion-reader, dept_liveness, calendar, "not the only data") live in each
DEPT'S test suite — the prompt is rendered per-dept by scaffold, not a framework
artifact — so they are intentionally not duplicated here.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.lib.dispatch_helpers import classify_liveness, dept_liveness


# ─── classify_liveness: the pure decision rule ──────────────────────────────

def test_live_when_signals_fresh():
    # Export 2h old, commit 3h old → clearly alive.
    assert classify_liveness(True, 2.0, 3.0) == "live"


def test_freshest_signal_wins():
    # Export is stale (30h) but a commit landed 1h ago → still live.
    assert classify_liveness(True, 30.0, 1.0) == "live"
    # Symmetric: cold commit but fresh export.
    assert classify_liveness(True, 1.0, 40.0) == "live"


def test_stale_when_both_lag_past_stale_window():
    # Both signals between stale (26h) and dead (50h) → stale.
    assert classify_liveness(True, 30.0, 28.0) == "stale"


def test_dead_when_both_signals_cold():
    # Export and commit both older than the dead threshold → runtime stopped.
    assert classify_liveness(True, 60.0, 72.0) == "dead"


def test_missing_when_nothing_present():
    # No export, no measurable signal at all → dept produced nothing.
    assert classify_liveness(False, None, None) == "missing"


def test_dead_when_present_but_no_measurable_age():
    # Something claims present but neither age is measurable → no pulse.
    assert classify_liveness(True, None, None) == "dead"


def test_commit_only_signal_keeps_dept_live():
    # Export missing, but the repo committed 5h ago → not dead, the repo lives.
    assert classify_liveness(False, None, 5.0) == "live"


def test_custom_thresholds_respected():
    assert classify_liveness(True, 10.0, 10.0, stale_after_h=8.0, dead_after_h=20.0) == "stale"
    assert classify_liveness(True, 25.0, 25.0, stale_after_h=8.0, dead_after_h=20.0) == "dead"


# ─── dept_liveness: the gathering wrapper (filesystem + git metadata) ────────

def test_dept_liveness_requires_tz_aware_now():
    with pytest.raises(ValueError):
        dept_liveness(Path("."), datetime(2026, 6, 1, 9, 0, 0), "2026-05-31")


def test_dept_liveness_missing_repo_degrades_to_low_signal(tmp_path):
    # A path that is neither a git repo nor has an export → no signal, "missing".
    sig = dept_liveness(tmp_path / "nope", datetime.now(timezone.utc), "2026-05-31")
    assert sig["export_present"] is False
    assert sig["export_age_hours"] is None
    assert sig["last_commit_iso"] is None
    assert sig["liveness"] == "missing"


def test_dept_liveness_reads_export_mtime_and_commit(tmp_path):
    # Build a minimal child repo: one commit + a yesterday export file.
    repo = tmp_path / "bubble-ops-child"
    (repo / "outputs" / "2026-05-31" / "4").mkdir(parents=True)
    export = repo / "outputs" / "2026-05-31" / "4" / "management-export.yaml"
    export.write_text("status: clean\n", encoding="utf-8")

    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "x"], check=True)

    sig = dept_liveness(repo, datetime.now(timezone.utc), "2026-05-31")
    assert sig["export_present"] is True
    assert sig["export_age_hours"] is not None and sig["export_age_hours"] >= 0
    assert sig["last_commit_iso"] is not None
    assert sig["commit_age_hours"] is not None
    # Just built it → both signals fresh → live.
    assert sig["liveness"] == "live"
