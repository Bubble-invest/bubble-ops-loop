"""
test_carnet_live.py — the Carnet de bord (/health) reads REAL loop activity.

{{OPERATOR}} msg 1180 (2026-06-01): the page was stale — morty_reader was a stub
that marked every (dept × layer) as never-run. It now reads the real
on-disk loop traces (per-layer .last-run + heartbeat) live, since the
console runs on the box.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from console import settings
from console.services import morty_reader


def _epoch(iso: str) -> float:
    return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=_dt.timezone.utc).timestamp()


# A fixed "now" so age/staleness are deterministic.
NOW = _epoch("2026-06-01T12:00:00")


@pytest.fixture
def disk_root(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(tmp_path))
    return tmp_path


def _dept(root: Path, slug: str) -> Path:
    repo = root / f"bubble-ops-{slug}"
    (repo / "outputs").mkdir(parents=True)
    return repo


def _last_run(repo: Path, day: str, layer: int, iso: str) -> None:
    d = repo / "outputs" / day / str(layer)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".last-run").write_text(iso, encoding="utf-8")


def _heartbeat(repo: Path, day: str, iso: str) -> None:
    (repo / "outputs" / day).mkdir(parents=True, exist_ok=True)
    (repo / "outputs" / day / "heartbeat.log").write_text(
        f"{iso} tick — decide_dispatch=layer_2\n", encoding="utf-8")


# ─── Per-layer rows ──────────────────────────────────────────────────────

def test_never_run_dept_all_layers_blank(disk_root):
    _dept(disk_root, "maya")
    rows = morty_reader.per_dept_layer_heartbeats(["maya"], now_epoch=NOW)
    assert len(rows) == 4
    assert all(r.last_success_iso == "" and r.is_stale for r in rows)


def test_real_last_run_surfaced_and_fresh(disk_root):
    repo = _dept(disk_root, "maya")
    _last_run(repo, "2026-06-01", 4, "2026-06-01T06:00:00Z")  # 6h ago
    rows = morty_reader.per_dept_layer_heartbeats(["maya"], now_epoch=NOW)
    l4 = next(r for r in rows if r.layer == 4)
    assert l4.last_success_iso == "2026-06-01T06:00:00Z"
    assert l4.is_stale is False                # 6h < 30h
    assert "il y a 6 h" == l4.age_human


def test_newest_last_run_wins_across_dates(disk_root):
    repo = _dept(disk_root, "maya")
    _last_run(repo, "2026-05-30", 1, "2026-05-30T06:00:00Z")
    _last_run(repo, "2026-06-01", 1, "2026-06-01T06:00:00Z")
    rows = morty_reader.per_dept_layer_heartbeats(["maya"], now_epoch=NOW)
    l1 = next(r for r in rows if r.layer == 1)
    assert l1.last_success_iso == "2026-06-01T06:00:00Z"   # newest


def test_layer_stale_when_old(disk_root):
    repo = _dept(disk_root, "maya")
    _last_run(repo, "2026-05-30", 2, "2026-05-30T06:00:00Z")  # ~54h ago
    rows = morty_reader.per_dept_layer_heartbeats(["maya"], now_epoch=NOW)
    l2 = next(r for r in rows if r.layer == 2)
    assert l2.is_stale is True


# ─── Loop pulse (dept-level heartbeat) ───────────────────────────────────

def test_pulse_alive_for_recent_heartbeat(disk_root):
    repo = _dept(disk_root, "tony")
    _heartbeat(repo, "2026-06-01", "2026-06-01T11:30:00Z")  # 30 min ago
    pulse = morty_reader.loop_pulse(["tony"], now_epoch=NOW)
    assert pulse["tony"].alive is True
    assert pulse["tony"].heartbeat_iso == "2026-06-01T11:30:00Z"


def test_pulse_silent_for_old_heartbeat(disk_root):
    repo = _dept(disk_root, "maya")
    _heartbeat(repo, "2026-05-31", "2026-05-31T23:35:00Z")  # ~12h ago
    pulse = morty_reader.loop_pulse(["maya"], now_epoch=NOW)
    assert pulse["maya"].alive is False
    assert "il y a 12 h" == pulse["maya"].age_human


def test_pulse_none_when_no_heartbeat(disk_root):
    _dept(disk_root, "cgp")
    pulse = morty_reader.loop_pulse(["cgp"], now_epoch=NOW)
    assert pulse["cgp"].heartbeat_iso == "" and pulse["cgp"].alive is False


# ─── Route rendering ─────────────────────────────────────────────────────

def test_health_page_renders_live_pulse(client, fixture_root):
    repo = fixture_root / "bubble-ops-fixture"
    # Fresh heartbeat + a recent layer-4 run for the fixture dept.
    (repo / "outputs" / "2026-06-01").mkdir(parents=True, exist_ok=True)
    (repo / "outputs" / "2026-06-01" / "heartbeat.log").write_text(
        "2026-06-01T11:55:00Z tick\n", encoding="utf-8")
    r = client.get("/health")
    assert r.status_code == 200
    # The live pulse phrasing appears (alive or silent — both are "live").
    assert "Boucle active" in r.text or "Boucle silencieuse" in r.text
