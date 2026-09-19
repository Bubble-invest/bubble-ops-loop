"""Tests for memory_hygiene_notify.py (#1223).

Covers:
  - WORKING_MEMORY.md is now scanned (size + staleness triggers), mirroring
    the MEMORY.md logic (analyze_working / working_memories / build_working_nudge).
  - An agent with MEMORY.md but no WORKING_MEMORY.md is skipped, never crashes.
  - The #874 fix (a LIVE path always wins over a larger, frozen/stale cache,
    by mtime for WORKING_MEMORY.md and by live-VPS-path-outranks-size for
    MEMORY.md) still holds.
  - The tool stays notify-only: analysis functions never touch the source
    files, and deliver() in --dry-run mode performs no filesystem writes.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/cloud-wiki-compile/scripts/memory_hygiene_notify.py"
SPEC = importlib.util.spec_from_file_location("memory_hygiene_notify", SCRIPT)
mhn = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mhn)


def _write(path: pathlib.Path, size_bytes: int = 100, mtime_days_ago: float | None = None) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * size_bytes, encoding="utf-8")
    if mtime_days_ago is not None:
        ts = time.time() - mtime_days_ago * 86400
        os.utime(path, (ts, ts))
    return path


# ── analyze_working: size + staleness triggers (mirrors analyze_index) ─────
def test_analyze_working_healthy_small_recent(tmp_path):
    wm = _write(tmp_path / "WORKING_MEMORY.md", size_bytes=500)
    info = mhn.analyze_working(wm)
    assert info["cluttered"] is False
    assert info["reasons"] == []


def test_analyze_working_size_trigger(tmp_path):
    wm = _write(tmp_path / "WORKING_MEMORY.md", size_bytes=mhn.WM_SIZE_BUDGET_BYTES + 1024)
    info = mhn.analyze_working(wm)
    assert info["cluttered"] is True
    assert any("soft budget" in r for r in info["reasons"])


def test_analyze_working_staleness_trigger(tmp_path):
    wm = _write(
        tmp_path / "WORKING_MEMORY.md",
        size_bytes=mhn.WM_STALE_MIN_BYTES + 1024,
        mtime_days_ago=mhn.WM_STALE_DAYS + 5,
    )
    info = mhn.analyze_working(wm)
    assert info["cluttered"] is True
    assert any("not updated" in r for r in info["reasons"])


def test_analyze_working_stale_but_tiny_not_triggered(tmp_path):
    # Old but under WM_STALE_MIN_BYTES and under the size budget -> not clutter.
    wm = _write(
        tmp_path / "WORKING_MEMORY.md",
        size_bytes=mhn.WM_STALE_MIN_BYTES - 100,
        mtime_days_ago=mhn.WM_STALE_DAYS + 30,
    )
    info = mhn.analyze_working(wm)
    assert info["cluttered"] is False


def test_analyze_working_never_mutates_source_file(tmp_path):
    wm = _write(tmp_path / "WORKING_MEMORY.md", size_bytes=mhn.WM_SIZE_BUDGET_BYTES + 1)
    before = wm.read_text()
    before_mtime = wm.stat().st_mtime
    mhn.analyze_working(wm)
    assert wm.read_text() == before
    assert wm.stat().st_mtime == before_mtime


# ── working_memories(): discovery, slug normalization, missing file safety ──
def test_working_memories_finds_and_normalizes_slugs(tmp_path, monkeypatch):
    _write(tmp_path / "ben" / "WORKING_MEMORY.md", size_bytes=200)
    _write(tmp_path / "tony" / "WORKING_MEMORY.md", size_bytes=200)  # WM_DIR_ALIAS -> main-strategist
    monkeypatch.setattr(mhn, "WM_SCAN_BASES", [tmp_path])
    found = mhn.working_memories()
    assert set(found.keys()) == {"ben", "main-strategist"}


def test_working_memories_skips_agent_without_working_memory_no_crash(tmp_path, monkeypatch):
    # ben has WORKING_MEMORY.md; maya has ONLY MEMORY.md (no working-memory file).
    _write(tmp_path / "ben" / "WORKING_MEMORY.md", size_bytes=200)
    (tmp_path / "maya").mkdir(parents=True, exist_ok=True)
    _write(tmp_path / "maya" / "MEMORY.md", size_bytes=200)
    monkeypatch.setattr(mhn, "WM_SCAN_BASES", [tmp_path])
    found = mhn.working_memories()  # must not raise
    assert set(found.keys()) == {"ben"}
    assert "maya" not in found


def test_working_memories_missing_scan_base_no_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(mhn, "WM_SCAN_BASES", [tmp_path / "does-not-exist"])
    assert mhn.working_memories() == {}


def test_working_memories_newest_mtime_wins_not_largest(tmp_path, monkeypatch):
    # Two candidate dirs normalize to the same agent ("ben" and "bubble-ops-ben").
    # The #874 bug picked the LARGEST file as canonical; the fix picks NEWEST.
    old = _write(tmp_path / "bubble-ops-ben" / "WORKING_MEMORY.md", size_bytes=50_000)
    os.utime(old, (time.time() - 10 * 86400,) * 2)  # 10 days old, big
    new = _write(tmp_path / "ben" / "WORKING_MEMORY.md", size_bytes=200)  # fresh, tiny
    monkeypatch.setattr(mhn, "WM_SCAN_BASES", [tmp_path])
    found = mhn.working_memories()
    assert found["ben"] == new  # newest wins, not the 50KB stale one


# ── #874 re-verify: canonical_memories() never lets a frozen cache outrank a
#    live path, regardless of size ──────────────────────────────────────────
def test_canonical_memories_live_vps_path_wins_over_larger_frozen_cache(tmp_path, monkeypatch):
    vps_projects = tmp_path / "vps_projects"
    scan_base = tmp_path / "mac_cache"

    # Small, live, healthy index.
    live_dir = vps_projects / f"{mhn.VPS_DEPT_PREFIX}ben"
    live_md = _write(live_dir / "memory" / "MEMORY.md", size_bytes=2_000)

    # Larger, frozen, stale cache that predates migration -- must NOT win.
    frozen_md = _write(scan_base / "ben" / "MEMORY.md", size_bytes=90_000)

    monkeypatch.setattr(mhn, "VPS_PROJECTS_DIR", vps_projects)
    monkeypatch.setattr(mhn, "SCAN_BASES", [scan_base])

    best = mhn.canonical_memories()
    assert best["ben"] == live_md
    assert best["ben"] != frozen_md


def test_canonical_memories_falls_back_to_scan_bases_when_no_live_path(tmp_path, monkeypatch):
    scan_base = tmp_path / "mac_cache"
    md = _write(scan_base / "rnd" / "MEMORY.md", size_bytes=1_000)
    monkeypatch.setattr(mhn, "VPS_PROJECTS_DIR", tmp_path / "no-such-dir")
    monkeypatch.setattr(mhn, "SCAN_BASES", [scan_base])
    best = mhn.canonical_memories()
    assert best["rnd"] == md


# ── _run_pass / main(): graceful with zero files, no crash ──────────────────
def test_run_pass_working_empty_files_no_crash():
    report: list[str] = []
    mhn._run_pass("working", {}, mhn.analyze_working, mhn.build_working_nudge, True, report)
    assert any("no live files found" in line for line in report)


def test_main_dry_run_end_to_end_no_working_memory_anywhere(tmp_path, monkeypatch, capsys):
    # Full main() smoke test: an agent has MEMORY.md but no WORKING_MEMORY.md
    # at all anywhere in WM_SCAN_BASES. Must not crash and must report cleanly.
    scan_base = tmp_path / "mac_cache"
    _write(scan_base / "rnd" / "MEMORY.md", size_bytes=1_000)
    monkeypatch.setattr(mhn, "VPS_PROJECTS_DIR", tmp_path / "no-such-dir")
    monkeypatch.setattr(mhn, "SCAN_BASES", [scan_base])
    monkeypatch.setattr(mhn, "WM_SCAN_BASES", [tmp_path / "no-working-memory-anywhere"])
    monkeypatch.setattr("sys.argv", ["memory_hygiene_notify.py", "--dry-run"])

    rc = mhn.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "WORKING_MEMORY.md scan:" in out
    assert "(no live files found)" in out


def test_main_dry_run_flags_bloated_working_memory(tmp_path, monkeypatch, capsys):
    scan_base = tmp_path / "mac_cache"
    _write(scan_base / "rnd" / "MEMORY.md", size_bytes=1_000)
    wm_base = tmp_path / "srv_agents"
    _write(wm_base / "ben" / "WORKING_MEMORY.md", size_bytes=mhn.WM_SIZE_BUDGET_BYTES + 2048)

    monkeypatch.setattr(mhn, "VPS_PROJECTS_DIR", tmp_path / "no-such-dir")
    monkeypatch.setattr(mhn, "SCAN_BASES", [scan_base])
    monkeypatch.setattr(mhn, "WM_SCAN_BASES", [wm_base])
    monkeypatch.setattr(mhn, "ROUTING", {"ben": ("local", str(tmp_path / "inject"))})
    monkeypatch.setattr("sys.argv", ["memory_hygiene_notify.py", "--dry-run"])

    rc = mhn.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "ben" in out
    assert "CLUTTERED" in out
    # --dry-run must never write the inject target (notify-only).
    assert not (tmp_path / "inject").exists()


# ── notify-only guarantee: dry-run delivery writes nothing ──────────────────
def test_deliver_dry_run_writes_nothing(tmp_path, monkeypatch):
    inject = tmp_path / "inject"
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(mhn, "ROUTING", {"ben": ("local", str(inject))})
    monkeypatch.setattr(mhn, "MAC_OUTBOX", outbox)
    ok, detail = mhn.deliver("ben", "test message", dry_run=True)
    assert ok is True
    assert "DRY-RUN" in detail
    assert not inject.exists()
    assert not outbox.exists()
