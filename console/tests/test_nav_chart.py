"""
test_nav_chart.py — native NAV-over-time chart on /dept/<slug> (board #364, PR-A).

Rick's research on #364 found 37% of Ben's kpi_snapshots rows (13/35) were
written DEGRADED — a broker leg's live read failed and the row understates
NAV by tens of thousands of dollars. A naive plot of `nav` reads as a
sawtooth of fake crashes. Ben's own chart tool (nav_vs_acwi_chart.py)
excludes any row whose `notes` contains a `[DEGRADED:` marker; this feature
uses the same marker but renders those points as a visible gap instead of
silently dropping them (a diagnostic surface should show that a bad write
happened, not make it look like no snapshot occurred that day).

#638 (still in progress as of this writing) will add a structural
`degraded` column to kpi_snapshots — nav_history.py checks for that column
first and falls back to the notes-marker heuristic, so this suite covers
BOTH paths.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from console import settings
from console.services import nav_history


@pytest.fixture
def disk_root(monkeypatch, tmp_path):
    """Point the services' disk-mode reader at a temp root (mirrors
    test_whiteboard_graphs.py's fixture — settings.READ_FROM_DISK is
    captured at import time, so the module attribute must be patched)."""
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(tmp_path))
    return tmp_path


def _build_ben_repo(root: Path, slug: str = "ben") -> Path:
    repo = root / f"bubble-ops-{slug}"
    (repo / "db").mkdir(parents=True)
    return repo


def _make_db(repo: Path, extra_columns: str = "") -> sqlite3.Connection:
    db_path = repo / "db" / "fund.sqlite"
    con = sqlite3.connect(db_path)
    con.execute(f"""
        CREATE TABLE kpi_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT NOT NULL UNIQUE,
            nav REAL NOT NULL,
            notes TEXT
            {extra_columns}
        )
    """)
    con.commit()
    return con


def _insert(con, snapshot_at, nav, notes=None, degraded=None):
    if degraded is None:
        con.execute(
            "INSERT INTO kpi_snapshots (snapshot_at, nav, notes) VALUES (?, ?, ?)",
            (snapshot_at, nav, notes),
        )
    else:
        con.execute(
            "INSERT INTO kpi_snapshots (snapshot_at, nav, notes, degraded) VALUES (?, ?, ?, ?)",
            (snapshot_at, nav, notes, int(degraded)),
        )
    con.commit()


# ─── Service-level tests ────────────────────────────────────────────────

def test_empty_when_no_repo(disk_root):
    hist = nav_history.load_nav_history("nonexistent")
    assert hist.points == []
    assert not hist.has_chart


def test_empty_when_no_db_file(disk_root):
    repo = _build_ben_repo(disk_root)
    hist = nav_history.load_nav_history("ben")
    assert hist.points == []
    assert not hist.has_chart


def test_series_built_from_kpi_snapshots(disk_root):
    repo = _build_ben_repo(disk_root)
    con = _make_db(repo)
    _insert(con, "2026-06-06T07:00:00+00:00", 236661.55)
    _insert(con, "2026-06-07T07:00:00+00:00", 237000.00)
    _insert(con, "2026-06-08T07:00:00+00:00", 238000.00)
    con.close()

    hist = nav_history.load_nav_history("ben", "all")
    assert hist.has_chart
    assert [p.nav for p in hist.points] == [236661.55, 237000.00, 238000.00]
    assert [p.degraded for p in hist.points] == [False, False, False]
    assert hist.last_display == "$238,000"
    assert len(hist.segments) == 1  # one unbroken clean run


def test_degraded_row_detected_via_notes_marker_uppercase(disk_root):
    """Mirrors nav_vs_acwi_chart.py's own authoritative marker convention."""
    repo = _build_ben_repo(disk_root)
    con = _make_db(repo)
    _insert(con, "2026-07-06T07:00:00+00:00", 249269.0)
    _insert(con, "2026-07-07T07:04:35+00:00", 198093.68,
            notes="nav_basis=... [DEGRADED: alpaca: TypeError: int() argument ...]")
    _insert(con, "2026-07-08T07:00:00+00:00", 248192.66)
    con.close()

    hist = nav_history.load_nav_history("ben", "all")
    by_date = {p.date: p for p in hist.points}
    assert by_date["2026-07-07"].degraded is True
    assert by_date["2026-07-06"].degraded is False
    assert by_date["2026-07-08"].degraded is False
    assert hist.degraded_count == 1


def test_degraded_row_detected_via_notes_marker_lowercase(disk_root):
    """Some rows write the marker lowercase — the regex must be case-insensitive
    (verified against the live DB: both cases occur across the row history)."""
    repo = _build_ben_repo(disk_root)
    con = _make_db(repo)
    _insert(con, "2026-06-18T05:27:00+00:00", 239411.28)
    _insert(con, "2026-06-19T07:14:36+00:00", 219941.18,
            notes="nav_basis=... [degraded: saxo: SecretsAbsent: broker 'saxo' is fenced]")
    con.close()

    hist = nav_history.load_nav_history("ben", "all")
    by_date = {p.date: p for p in hist.points}
    assert by_date["2026-06-19"].degraded is True


def test_degraded_points_never_join_the_polyline(disk_root):
    """A degraded point must split the polyline into separate segments — it
    must never draw a solid connecting line through a bad read (that would
    look like a real NAV move)."""
    repo = _build_ben_repo(disk_root)
    con = _make_db(repo)
    _insert(con, "2026-07-05T00:00:00+00:00", 249000.0)
    _insert(con, "2026-07-06T00:00:00+00:00", 249269.0)
    _insert(con, "2026-07-07T00:00:00+00:00", 198093.68, notes="[DEGRADED: alpaca zeroed]")
    _insert(con, "2026-07-08T00:00:00+00:00", 248192.66)
    _insert(con, "2026-07-09T00:00:00+00:00", 248500.0)
    con.close()

    hist = nav_history.load_nav_history("ben", "all")
    # 5 points, 1 degraded in the middle -> two clean segments (before/after)
    assert len(hist.segments) == 2
    assert len(hist.degraded_x) == 1
    assert len(hist.degraded_y) == 1
    # every point (clean + degraded) still gets a tooltip hit-target
    assert len(hist.all_x) == 5


def test_structural_degraded_column_preferred_over_notes(disk_root):
    """Once #638 lands, a structural `degraded` column exists. It must win
    over the notes-marker heuristic (e.g. a corrected/superseded row might
    still carry historical DEGRADED prose in notes but be marked clean)."""
    repo = _build_ben_repo(disk_root)
    con = _make_db(repo, extra_columns=", degraded INTEGER")
    # degraded=0 (clean) despite notes mentioning "degraded" in prose
    _insert(con, "2026-07-01T00:00:00+00:00", 246733.0,
            notes="supersedes the earlier degraded auto-write", degraded=False)
    # degraded=1 (structural flag) with NO marker text at all
    _insert(con, "2026-07-02T00:00:00+00:00", 200000.0,
            notes="clean-looking prose", degraded=True)
    con.close()

    hist = nav_history.load_nav_history("ben", "all")
    by_date = {p.date: p for p in hist.points}
    assert by_date["2026-07-01"].degraded is False
    assert by_date["2026-07-02"].degraded is True


def test_one_point_per_day_last_snapshot_wins(disk_root):
    repo = _build_ben_repo(disk_root)
    con = _make_db(repo)
    _insert(con, "2026-06-30T07:00:00+00:00", 245795.0)
    _insert(con, "2026-06-30T19:00:00+00:00", 245557.0)  # later same-day row wins
    con.close()

    hist = nav_history.load_nav_history("ben", "all")
    assert len(hist.points) == 1
    assert hist.points[0].nav == 245557.0


def test_bare_date_timestamp_parses(disk_root):
    """Some rows store a bare date (no time component) — must not crash."""
    repo = _build_ben_repo(disk_root)
    con = _make_db(repo)
    _insert(con, "2026-06-28", 245161.0)
    _insert(con, "2026-06-29T07:06:08Z", 245596.0)
    con.close()

    hist = nav_history.load_nav_history("ben", "all")
    assert [p.date for p in hist.points] == ["2026-06-28", "2026-06-29"]


def test_range_filter_30_90_all(disk_root):
    repo = _build_ben_repo(disk_root)
    con = _make_db(repo)
    # 120 days of data ending 2026-07-16
    import datetime
    base = datetime.date(2026, 7, 16)
    for i in range(120):
        d = base - datetime.timedelta(days=i)
        _insert(con, f"{d.isoformat()}T07:00:00+00:00", 240000.0 + i)
    con.close()

    all_hist = nav_history.load_nav_history("ben", "all")
    hist_90 = nav_history.load_nav_history("ben", "90")
    hist_30 = nav_history.load_nav_history("ben", "30")

    assert len(all_hist.points) == 120
    assert len(hist_90.points) <= 91  # inclusive cutoff
    assert len(hist_30.points) <= 31
    assert len(hist_30.points) < len(hist_90.points) < len(all_hist.points)


def test_invalid_range_falls_back_to_default(disk_root):
    repo = _build_ben_repo(disk_root)
    con = _make_db(repo)
    _insert(con, "2026-07-01T00:00:00+00:00", 246000.0)
    _insert(con, "2026-07-02T00:00:00+00:00", 246500.0)
    con.close()

    hist = nav_history.load_nav_history("ben", "not-a-real-range")
    assert hist.range_key == nav_history.DEFAULT_RANGE


def test_nulls_and_missing_table_degrade_gracefully(disk_root):
    """A DB that exists but has no kpi_snapshots table (or an unrelated
    schema) must return an empty, graceful result — never raise."""
    repo = _build_ben_repo(disk_root)
    db_path = repo / "db" / "fund.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE unrelated (id INTEGER)")
    con.commit()
    con.close()

    hist = nav_history.load_nav_history("ben")
    assert hist.points == []
    assert not hist.has_chart


def test_single_point_has_no_chart():
    """< 2 points: has_chart is False (matches whiteboard_series' contract —
    can't draw a meaningful line through one point)."""
    pt = nav_history.NavPoint(date="2026-07-01", nav=100.0, degraded=False)
    hist = nav_history.NavHistory(points=[pt])
    assert not hist.has_chart


# ─── Route + template rendering ─────────────────────────────────────────

def _write_dept_yaml(repo: Path, slug: str) -> None:
    import yaml
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops", "mandate": "family office"},
            "layers": {"subscribed": [1, 2, 3, 4]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir(exist_ok=True)
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({"status": "Live", "validated_steps": ["mandate"]}),
        encoding="utf-8",
    )


def test_dept_page_renders_nav_chart_for_ben(client, fixture_root):
    repo = _build_ben_repo(fixture_root)
    _write_dept_yaml(repo, "ben")
    con = _make_db(repo)
    _insert(con, "2026-07-15T07:00:00+00:00", 247843.0)
    _insert(con, "2026-07-16T06:00:00+00:00", 248853.0)
    con.close()

    r = client.get("/dept/ben")
    assert r.status_code == 200
    assert "nav-chart-card" in r.text
    assert "$248,853" in r.text


def test_dept_page_no_nav_chart_for_dept_without_fund_db(client, fixture_root):
    repo = fixture_root / "bubble-ops-otherdept"
    repo.mkdir(parents=True)
    _write_dept_yaml(repo, "otherdept")

    r = client.get("/dept/otherdept")
    assert r.status_code == 200
    assert "nav-chart-card" not in r.text


def test_dept_page_range_toggle_query_param(client, fixture_root):
    repo = _build_ben_repo(fixture_root)
    _write_dept_yaml(repo, "ben")
    con = _make_db(repo)
    import datetime
    base = datetime.date(2026, 7, 16)
    for i in range(120):
        d = base - datetime.timedelta(days=i)
        _insert(con, f"{d.isoformat()}T07:00:00+00:00", 240000.0 + i)
    con.close()

    r_default = client.get("/dept/ben")
    r_30 = client.get("/dept/ben?nav_range=30")
    r_all = client.get("/dept/ben?nav_range=all")
    assert r_default.status_code == r_30.status_code == r_all.status_code == 200
    assert "nav-chart-range-link--active" in r_30.text


def test_dept_page_degraded_row_renders_as_gap_not_solid_line(client, fixture_root):
    repo = _build_ben_repo(fixture_root)
    _write_dept_yaml(repo, "ben")
    con = _make_db(repo)
    _insert(con, "2026-07-06T00:00:00+00:00", 249269.0)
    _insert(con, "2026-07-07T00:00:00+00:00", 198093.68, notes="[DEGRADED: alpaca zeroed]")
    _insert(con, "2026-07-08T00:00:00+00:00", 248192.66)
    con.close()

    r = client.get("/dept/ben?nav_range=all")
    assert r.status_code == 200
    assert "nav-chart-degraded-marker" in r.text
    assert "DÉGRADÉ" in r.text
    assert "1 snapshot" in r.text  # degraded-count note
