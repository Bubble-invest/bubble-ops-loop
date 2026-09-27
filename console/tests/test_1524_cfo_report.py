"""
test_1524_cfo_report.py — TDD tests for board #1524.

Jade/CEO ask (Telegram msg 740, 2026-09-25): surface Géraldine's weekly
`outputs/<date>/4/cfo-report.md` (mission `weekly_cfo_report`, Layer 4,
fires Wednesdays) on the cockpit /dept/accountant page, next to a short
history — today it's only reachable via GitHub.

Tests cover:
  - github_reader.load_cfo_report:
      * finds the latest report among several dated dirs
      * returns {"latest": None, "history": []} when none exists
      * ignores a report outside the lookback window (treated as none)
      * history is newest-first and capped at 6 entries
  - GET /dept/<slug>:
      * accountant-slug dept with a report renders it + a history link
      * accountant-slug dept with NO report renders the empty state
      * a non-accountant dept with no report at all never grows the card

Mirrors test_management_view.py's fixture style (disk-mode, tmp_path-built
bubble-ops-<slug> repos) — no network, no `gh` calls.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

TEST_BEARER = "test-token-xyz"


def _reload_console_modules() -> None:
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]


def _write_dept_yaml(dept_dir: Path, slug: str, mandate: str = "test dept") -> None:
    dept_dir.mkdir(parents=True, exist_ok=True)
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": slug, "level": "ops", "mandate": mandate},
                "layers": {"subscribed": [1, 2, 3, 4]},
                "recurring_missions": [],
                "skills": {},
                "tools": [],
                "gate_policies": {},
                "hierarchy": {
                    "level": "ops",
                    "parent": None,
                    "children": [],
                    "visibility": {
                        "read_outputs": [],
                        "read_risk_kpis": False,
                        "read_risk_briefs": False,
                        "read_raw_artifacts": False,
                        "read_secrets": False,
                    },
                    "directive_policy": {
                        "can_open_priority_prs": False,
                        "target_queue": None,
                        "requires_human_gate_for": [],
                    },
                },
                "optional_domain_ledger": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (dept_dir / "onboarding").mkdir(exist_ok=True)
    (dept_dir / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "slug": slug,
                "display_name": slug.capitalize(),
                "owner": "operator",
                "created_at": "2026-05-15T10:00:00Z",
                "status": "Live",
                "validated_steps": [
                    "mandate", "missions", "layers",
                    "skills_tools", "gates_kpis", "dry_run",
                ],
                "last_updated_at": "2026-05-19T10:00:00Z",
                "commits": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_cfo_report(dept_dir: Path, d: date, content: str) -> None:
    layer4 = dept_dir / "outputs" / d.isoformat() / "4"
    layer4.mkdir(parents=True, exist_ok=True)
    (layer4 / "cfo-report.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Service-level tests (github_reader.load_cfo_report)
# ---------------------------------------------------------------------------


class TestLoadCfoReport:

    def test_finds_latest_among_several_dated_dirs(self, tmp_path: Path, monkeypatch):
        root = tmp_path / "depts"
        root.mkdir()
        acc = root / "bubble-ops-accountant"
        _write_dept_yaml(acc, "accountant")

        today = date.today()
        # Three weekly reports, oldest to newest.
        d3 = today - timedelta(weeks=3)
        d2 = today - timedelta(weeks=2)
        d1 = today - timedelta(weeks=1)
        _write_cfo_report(acc, d3, "# CFO report — three weeks ago")
        _write_cfo_report(acc, d2, "# CFO report — two weeks ago")
        _write_cfo_report(acc, d1, "# CFO report — latest (one week ago)")

        monkeypatch.setenv("READ_FROM_DISK", str(root))
        _reload_console_modules()
        from console.services.github_reader import load_cfo_report

        result = load_cfo_report("accountant")
        assert result["latest"] is not None, f"expected a latest report, got {result}"
        assert result["latest"]["date"] == d1.isoformat(), (
            f"expected the newest dated dir ({d1.isoformat()}) to win, "
            f"got {result['latest']['date']}"
        )
        assert "latest (one week ago)" in result["latest"]["content"]
        assert result["latest"]["rel_path"] == f"outputs/{d1.isoformat()}/4/cfo-report.md"

    def test_none_found_when_no_report_ever_written(self, tmp_path: Path, monkeypatch):
        root = tmp_path / "depts"
        root.mkdir()
        acc = root / "bubble-ops-accountant"
        _write_dept_yaml(acc, "accountant")
        # outputs/ exists (other layers/missions write here) but no
        # 4/cfo-report.md anywhere.
        (acc / "outputs" / date.today().isoformat() / "1").mkdir(parents=True)

        monkeypatch.setenv("READ_FROM_DISK", str(root))
        _reload_console_modules()
        from console.services.github_reader import load_cfo_report

        result = load_cfo_report("accountant")
        assert result == {"latest": None, "history": []}

    def test_none_found_for_unknown_dept(self, tmp_path: Path, monkeypatch):
        root = tmp_path / "depts"
        root.mkdir()
        monkeypatch.setenv("READ_FROM_DISK", str(root))
        _reload_console_modules()
        from console.services.github_reader import load_cfo_report

        assert load_cfo_report("does-not-exist") == {"latest": None, "history": []}

    def test_ignores_report_outside_lookback_window(self, tmp_path: Path, monkeypatch):
        """A report older than the ~5-week lookback is treated as 'none' —
        a stale render of a months-old report would be worse than an honest
        empty state (the weekly mission itself would be the actual problem)."""
        root = tmp_path / "depts"
        root.mkdir()
        acc = root / "bubble-ops-accountant"
        _write_dept_yaml(acc, "accountant")

        stale_date = date.today() - timedelta(weeks=10)
        _write_cfo_report(acc, stale_date, "# CFO report — ten weeks ago (stale)")

        monkeypatch.setenv("READ_FROM_DISK", str(root))
        _reload_console_modules()
        from console.services.github_reader import load_cfo_report

        result = load_cfo_report("accountant")
        assert result["latest"] is None, (
            f"a 10-week-old report is outside the lookback window and must not "
            f"surface as 'latest', got {result}"
        )
        assert result["history"] == []

    def test_history_ordering_newest_first_and_capped(self, tmp_path: Path, monkeypatch):
        root = tmp_path / "depts"
        root.mkdir()
        acc = root / "bubble-ops-accountant"
        _write_dept_yaml(acc, "accountant")

        # 8 dated dirs within the lookback window (not realistically weekly,
        # but load_cfo_report only cares about dated dirs + the file's
        # presence — this exercises the cap independent of cadence).
        today = date.today()
        dates = [today - timedelta(days=3 * i) for i in range(8)]
        for i, d in enumerate(dates):
            _write_cfo_report(acc, d, f"# CFO report #{i}")

        monkeypatch.setenv("READ_FROM_DISK", str(root))
        _reload_console_modules()
        from console.services.github_reader import load_cfo_report

        result = load_cfo_report("accountant")
        history_dates = [h["date"] for h in result["history"]]
        expected_dates = sorted((d.isoformat() for d in dates), reverse=True)[:6]

        assert len(result["history"]) == 6, (
            f"history must be capped at 6 entries, got {len(result['history'])}: "
            f"{history_dates}"
        )
        assert history_dates == expected_dates, (
            f"history must be newest-first, got {history_dates}, "
            f"expected {expected_dates}"
        )
        # `latest` must be the same report as history[0].
        assert result["latest"]["date"] == history_dates[0]


# ---------------------------------------------------------------------------
# Route-level tests (GET /dept/<slug>)
# ---------------------------------------------------------------------------


@pytest.fixture
def cfo_fixture_root(tmp_path: Path) -> Path:
    """
    Root with:
      - bubble-ops-accountant : Live ops dept, one CFO report this week
      - bubble-ops-fixture    : Live ops dept, NO CFO report ever (existing
                                generic fixture shape, built inline here so
                                this file has no cross-test dependency)
    """
    root = tmp_path / "depts"
    root.mkdir()

    acc = root / "bubble-ops-accountant"
    _write_dept_yaml(acc, "accountant", mandate="Géraldine — CFO reporting")
    today = date.today()
    _write_cfo_report(
        acc, today,
        "# Rapport CFO hebdomadaire\n\n**NAV**: 1,000,000 USD\n\nTout va bien.",
    )

    other = root / "bubble-ops-fixture"
    _write_dept_yaml(other, "fixture")
    (other / "outputs").mkdir(parents=True, exist_ok=True)

    return root


@pytest.fixture
def cfo_client(monkeypatch, cfo_fixture_root: Path):
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", TEST_BEARER)
    monkeypatch.setenv("CONSOLE_GATE_RBAC", '{"bearer":["*"]}')
    monkeypatch.setenv("READ_FROM_DISK", str(cfo_fixture_root))
    _reload_console_modules()
    from console.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {TEST_BEARER}"})
    return c


class TestDeptDetailCfoReportSection:

    def test_accountant_page_shows_latest_report(self, cfo_client):
        resp = cfo_client.get("/dept/accountant")
        assert resp.status_code == 200, resp.text[:500]
        body = resp.text
        assert "Rapport CFO hebdomadaire" in body
        assert "NAV" in body and "1,000,000 USD" in body
        # A link back to the raw source file for this dated report (Jinja's
        # `urlencode` filter leaves `/` unescaped — same convention the
        # existing loop-run output links already use).
        assert "/dept/accountant/output?f=outputs/" in body
        assert "cfo-report.md" in body

    def test_accountant_page_empty_state_when_no_report(self, tmp_path: Path, monkeypatch):
        root = tmp_path / "depts"
        root.mkdir()
        acc = root / "bubble-ops-accountant"
        _write_dept_yaml(acc, "accountant")
        (acc / "outputs").mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("CONSOLE_BEARER_TOKEN", TEST_BEARER)
        monkeypatch.setenv("CONSOLE_GATE_RBAC", '{"bearer":["*"]}')
        monkeypatch.setenv("READ_FROM_DISK", str(root))
        _reload_console_modules()
        from console.main import create_app
        from fastapi.testclient import TestClient

        app = create_app()
        c = TestClient(app)
        c.headers.update({"Authorization": f"Bearer {TEST_BEARER}"})

        resp = c.get("/dept/accountant")
        assert resp.status_code == 200, resp.text[:500]
        body = resp.text
        assert "Rapport CFO hebdomadaire" in body
        assert "Aucun rapport CFO hebdomadaire trouvé" in body

    def test_non_accountant_dept_never_shows_the_card(self, cfo_client):
        """A dept that has never had a cfo-report.md must not grow a
        permanently-empty CFO card — it means nothing to Ben/Maya/content."""
        resp = cfo_client.get("/dept/fixture")
        assert resp.status_code == 200, resp.text[:500]
        assert "Rapport CFO hebdomadaire" not in resp.text
