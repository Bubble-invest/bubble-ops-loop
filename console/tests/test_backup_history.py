"""
test_backup_history.py — surface the loop-backup safety net in the cockpit.

{{OPERATOR}} msg 1171 (2026-06-01): the twice-daily backup timer (loop-backup.sh)
appends one event per dept per fire to a central JSONL. The dept page must
show a "Filet de sécurité" block (latest verdict + recent checks) and the
home page a one-line roll-up — the timer used to be journal-only, invisible
in the front end.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from console import settings
from console.services import backup_history


def _write_log(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )


@pytest.fixture
def backup_log(monkeypatch, tmp_path):
    """Point the service at a temp event log."""
    path = tmp_path / "state" / "loop-backup.jsonl"
    monkeypatch.setattr(settings, "BACKUP_LOG_PATH", path)
    return path


# ─── Service ──────────────────────────────────────────────────────────────

def test_no_log_is_empty(backup_log):
    assert backup_history.recent_backups("maya") == []
    assert backup_history.latest_backup("maya") is None
    r = backup_history.rollup()
    assert r.any_activity is False
    assert (r.healthy, r.backed_up, r.failed) == (0, 0, 0)


def test_recent_backups_newest_first_and_filtered(backup_log):
    _write_log(backup_log, [
        {"ts": "2026-06-01T08:00:00Z", "slug": "maya", "action": "skip", "reason": "alive"},
        {"ts": "2026-06-01T08:00:01Z", "slug": "tony", "action": "skip", "reason": "alive"},
        {"ts": "2026-06-01T14:00:00Z", "slug": "maya", "action": "run",
         "reason": "stale", "age_sec": 9000, "exit": 0},
    ])
    maya = backup_history.recent_backups("maya")
    assert [e.ts for e in maya] == ["2026-06-01T14:00:00Z", "2026-06-01T08:00:00Z"]
    assert len(backup_history.recent_backups("tony")) == 1   # filtered by slug
    latest = backup_history.latest_backup("maya")
    assert latest.action == "run" and latest.exit_code == 0


def test_skip_event_verdict_and_ok(backup_log):
    _write_log(backup_log, [
        {"ts": "t", "slug": "maya", "action": "skip", "reason": "alive", "age_sec": 600},
    ])
    ev = backup_history.latest_backup("maya")
    assert ev.ok is True
    assert "active" in ev.verdict_fr.lower()
    assert ev.age_human == "10 min"


def test_run_success_vs_failure_verdict(backup_log):
    _write_log(backup_log, [
        {"ts": "t1", "slug": "maya", "action": "run", "reason": "stale", "exit": 0},
        {"ts": "t2", "slug": "tony", "action": "run", "reason": "stale", "exit": 1},
    ])
    ok = backup_history.latest_backup("maya")
    bad = backup_history.latest_backup("tony")
    assert ok.ok is True and "✓" in ok.verdict_fr
    assert bad.ok is False and "✗" in bad.verdict_fr


def test_rollup_counts_latest_per_dept(backup_log):
    _write_log(backup_log, [
        # maya recovered: first run, then a later skip → counts as healthy
        {"ts": "2026-06-01T08:00:00Z", "slug": "maya", "action": "run", "reason": "stale", "exit": 0},
        {"ts": "2026-06-01T14:00:00Z", "slug": "maya", "action": "skip", "reason": "alive"},
        {"ts": "2026-06-01T14:00:01Z", "slug": "tony", "action": "run", "reason": "stale", "exit": 0},
        {"ts": "2026-06-01T14:00:02Z", "slug": "cgp", "action": "run", "reason": "stale", "exit": 1},
    ])
    r = backup_history.rollup()
    assert r.any_activity is True
    assert r.last_fire_ts == "2026-06-01T14:00:02Z"
    assert r.healthy == 1          # maya (latest = skip)
    assert r.backed_up == 2        # tony + cgp ran
    assert r.failed == 1           # cgp exit 1


def test_malformed_events_ignored(backup_log):
    backup_log.parent.mkdir(parents=True, exist_ok=True)
    backup_log.write_text(
        '{"ts":"t","slug":"maya","action":"skip","reason":"ok"}\n'
        "garbage\n"
        '{"ts":"t2","slug":"maya","action":"bogus","reason":"x"}\n',  # bad action dropped
        encoding="utf-8",
    )
    events = backup_history.recent_backups("maya")
    assert len(events) == 1 and events[0].action == "skip"


# ─── Dept page rendering ────────────────────────────────────────────────

def test_dept_page_shows_safety_net_empty_state(client, monkeypatch, tmp_path):
    from console import settings as s
    monkeypatch.setattr(s, "BACKUP_LOG_PATH", tmp_path / "absent.jsonl")
    resp = client.get("/dept/fixture")
    assert resp.status_code == 200
    assert "Filet de sécurité" in resp.text
    assert "Aucun passage enregistré" in resp.text


def test_dept_page_shows_latest_verdict(client, monkeypatch, tmp_path):
    from console import settings as s
    path = tmp_path / "loop-backup.jsonl"
    _write_log(path, [
        {"ts": "2026-06-01T14:00:00Z", "slug": "fixture", "action": "run",
         "reason": "stale", "age_sec": 9000, "exit": 0},
    ])
    monkeypatch.setattr(s, "BACKUP_LOG_PATH", path)
    resp = client.get("/dept/fixture")
    assert resp.status_code == 200
    assert "tick de secours exécuté" in resp.text
    assert "2026-06-01T14:00:00Z" in resp.text


# ─── Home roll-up banner ────────────────────────────────────────────────

def test_home_banner_hidden_without_activity(client, monkeypatch, tmp_path):
    from console import settings as s
    monkeypatch.setattr(s, "BACKUP_LOG_PATH", tmp_path / "absent.jsonl")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Filet de sécurité" not in resp.text


def test_home_banner_shows_rollup(client, monkeypatch, tmp_path):
    from console import settings as s
    path = tmp_path / "loop-backup.jsonl"
    _write_log(path, [
        {"ts": "2026-06-01T14:00:00Z", "slug": "fixture", "action": "skip", "reason": "alive"},
    ])
    monkeypatch.setattr(s, "BACKUP_LOG_PATH", path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Filet de sécurité" in resp.text
    assert "dernier passage 2026-06-01T14:00:00Z" in resp.text
