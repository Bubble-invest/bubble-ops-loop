"""
test_loop_history.py — loop-run history section + output viewer.

Joris msg 1168 (2026-06-01): the dept page shows the history of loop runs
with clickable outputs, even for empty runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from console import settings
from console.services import loop_history


@pytest.fixture
def disk_root(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(tmp_path))
    return tmp_path


def _repo(root: Path, slug: str = "demo") -> Path:
    repo = root / f"bubble-ops-{slug}"
    (repo / "outputs").mkdir(parents=True)
    return repo


def _layer_run(repo: Path, day: str, layer: int, files: dict, last_run: str):
    d = repo / "outputs" / day / str(layer)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".last-run").write_text(last_run, encoding="utf-8")
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")


# ─── Service ────────────────────────────────────────────────────────────────

def test_lists_runs_newest_first(disk_root):
    repo = _repo(disk_root)
    _layer_run(repo, "2026-05-29", 1, {"summary.md": "old"}, "2026-05-29T06:00:00Z")
    _layer_run(repo, "2026-05-31", 4, {"summary.md": "new"}, "2026-05-31T22:00:00Z")
    runs = loop_history.list_loop_runs("demo")
    assert [r.date for r in runs] == ["2026-05-31", "2026-05-29"]


def test_layer_files_and_rounds_surfaced(disk_root):
    repo = _repo(disk_root)
    _layer_run(repo, "2026-05-31", 4,
               {"summary.md": "s", "risk-kpis.yaml": "k: 1", "logs.jsonl": "{}"},
               "2026-05-31T22:00:00Z")
    (repo / "outputs" / "2026-05-31" / "round_counter.json").write_text(
        json.dumps({"4": 3}), encoding="utf-8")
    (repo / "outputs" / "2026-05-31" / "heartbeat.log").write_text("tick", encoding="utf-8")

    run = loop_history.list_loop_runs("demo")[0]
    assert run.total_rounds == 3
    assert not run.is_empty
    layer = run.layers[0]
    assert layer.num == 4 and layer.rounds == 3
    assert layer.last_run == "2026-05-31T22:00:00Z"
    names = [f.name for f in layer.files]
    assert names[0] == "summary.md"                # surfaced first
    assert set(names) == {"summary.md", "risk-kpis.yaml", "logs.jsonl", ".last-run"}
    # heartbeat shows as a date-level extra file
    assert any(e.name == "heartbeat.log" for e in run.extra_files)


def test_empty_run_still_listed(disk_root):
    """A day with only a heartbeat (no layer output) must still appear."""
    repo = _repo(disk_root)
    day_dir = repo / "outputs" / "2026-06-01"
    day_dir.mkdir(parents=True)
    (day_dir / "heartbeat.log").write_text("alive", encoding="utf-8")
    runs = loop_history.list_loop_runs("demo")
    assert len(runs) == 1
    assert runs[0].is_empty
    assert any(e.name == "heartbeat.log" for e in runs[0].extra_files)


def test_artifacts_subdir_included(disk_root):
    repo = _repo(disk_root)
    base = repo / "outputs" / "2026-05-31" / "2"
    (base / "artifacts").mkdir(parents=True)
    (base / ".last-run").write_text("2026-05-31T12:00:00Z", encoding="utf-8")
    (base / "artifacts" / "draft.md").write_text("hi", encoding="utf-8")
    run = loop_history.list_loop_runs("demo")[0]
    names = [f.name for f in run.layers[0].files]
    assert "artifacts/draft.md" in names


def test_ignores_non_date_dirs(disk_root):
    repo = _repo(disk_root)
    (repo / "outputs" / "dry-run").mkdir()
    (repo / "outputs" / "onboarding").mkdir()
    _layer_run(repo, "2026-05-31", 1, {"summary.md": "s"}, "2026-05-31T06:00:00Z")
    runs = loop_history.list_loop_runs("demo")
    assert [r.date for r in runs] == ["2026-05-31"]


# ─── read_output_file (safety) ───────────────────────────────────────────────

def test_read_output_file_ok(disk_root):
    repo = _repo(disk_root)
    _layer_run(repo, "2026-05-31", 4, {"summary.md": "# Hello"}, "2026-05-31T22:00:00Z")
    info = loop_history.read_output_file("demo", "outputs/2026-05-31/4/summary.md")
    assert info is not None
    assert info["content"] == "# Hello"
    assert info["kind"] == "markdown"
    assert info["empty"] is False


def test_read_output_file_empty(disk_root):
    repo = _repo(disk_root)
    d = repo / "outputs" / "2026-05-31" / "4"
    d.mkdir(parents=True)
    (d / "empty.md").write_text("", encoding="utf-8")
    info = loop_history.read_output_file("demo", "outputs/2026-05-31/4/empty.md")
    assert info is not None and info["empty"] is True and info["content"] == ""


def test_read_output_file_rejects_traversal(disk_root):
    repo = _repo(disk_root)
    (repo / "SECRET.txt").write_text("nope", encoding="utf-8")
    assert loop_history.read_output_file("demo", "outputs/../SECRET.txt") is None
    assert loop_history.read_output_file("demo", "../bubble-ops-demo/SECRET.txt") is None
    assert loop_history.read_output_file("demo", "dept.yaml") is None  # outside outputs/


def test_read_output_file_missing(disk_root):
    _repo(disk_root)
    assert loop_history.read_output_file("demo", "outputs/2026-05-31/4/nope.md") is None
    assert loop_history.read_output_file("ghost", "outputs/x.md") is None


# ─── Routes ──────────────────────────────────────────────────────────────────

def test_dept_page_shows_history(client, fixture_root):
    repo = fixture_root / "bubble-ops-fixture"
    _layer_run(repo, "2026-05-31", 4, {"summary.md": "debrief"}, "2026-05-31T22:00:00Z")
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    assert "Historique des boucles" in body
    assert "2026-05-31" in body
    assert "/dept/fixture/output?f=" in body          # a clickable output link
    assert "summary.md" in body


def test_dept_page_history_empty_run(client, fixture_root):
    repo = fixture_root / "bubble-ops-fixture"
    day = repo / "outputs" / "2026-06-01"
    day.mkdir(parents=True)
    (day / "heartbeat.log").write_text("alive", encoding="utf-8")
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "aucune sortie de couche" in r.text.lower()


def test_output_viewer_renders_file(client, fixture_root):
    repo = fixture_root / "bubble-ops-fixture"
    _layer_run(repo, "2026-05-31", 4, {"summary.md": "# Débrief\nzéro activité"},
               "2026-05-31T22:00:00Z")
    r = client.get("/dept/fixture/output",
                   params={"f": "outputs/2026-05-31/4/summary.md"})
    assert r.status_code == 200
    assert "zéro activité" in r.text
    assert "outputs/2026-05-31/4/summary.md" in r.text   # breadcrumb path


def test_output_viewer_404_on_traversal(client):
    r = client.get("/dept/fixture/output", params={"f": "outputs/../dept.yaml"})
    assert r.status_code == 404


def test_output_viewer_404_unknown_dept(client):
    r = client.get("/dept/zzz/output", params={"f": "outputs/x.md"})
    assert r.status_code == 404
