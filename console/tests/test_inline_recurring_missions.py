"""
test_inline_recurring_missions.py — Bug A1.

Per Notion v5 line 526:
  `missions/` recurring missions qui remplacent les anciens crons

PR #1 (commit 2429b9c) made INLINE `dept.yaml::recurring_missions:` arrays
spec-equivalent to file-based `missions/<id>.yaml`. Layer 1 data-curator
resolves both forms identically.

But `console/services/github_reader.py::list_missions(slug)` currently only
scans `missions/*.yaml` files. When a dept (Maya, Ben, the live fixture)
declares missions INLINE in `dept.yaml::recurring_missions:`, the
`/dept/<slug>` page wrongly renders "no missions declared".

This test creates a dept with ONLY inline missions (no file-based ones) and
asserts that:
  1) `list_missions(slug)` surfaces the inline mission ids
  2) `/dept/<slug>` HTML renders the inline mission ids
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _make_dept_with_inline_missions(fixture_root: Path,
                                    slug: str = "inline-only") -> Path:
    """Build bubble-ops-<slug> with only INLINE recurring_missions, no files."""
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops",
                           "mandate": "inline-mission dept for A1 regression"},
            "layers": {"subscribed": [1, 2, 3, 4]},
            "recurring_missions": [
                {"id": "echo_heartbeat", "layer": 1, "cadence": "every_2h",
                 "description": "inline heartbeat"},
                {"id": "second_inline_mission", "layer": 2,
                 "cadence": "every_4h"},
            ],
            "gate_policies": {},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": slug,
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-20T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    # The missions/ dir exists but contains no .yaml — only a README/gitkeep
    (repo / "missions").mkdir()
    (repo / "missions" / "README.md").write_text("placeholder\n",
                                                  encoding="utf-8")
    (repo / "missions" / ".gitkeep").write_text("", encoding="utf-8")
    return repo


def test_list_missions_includes_inline_recurring_missions(
        app, fixture_root):
    """github_reader.list_missions must merge inline + file missions."""
    _make_dept_with_inline_missions(fixture_root)
    from console.services import github_reader
    missions = github_reader.list_missions("inline-only")
    # Whatever the return shape (list[str] or list[dict]), the inline ids
    # must be surfaced.
    flat = " ".join(
        m if isinstance(m, str) else (m.get("id", "") if isinstance(m, dict) else "")
        for m in missions
    )
    assert "echo_heartbeat" in flat, \
        f"expected echo_heartbeat in missions, got: {missions!r}"
    assert "second_inline_mission" in flat, \
        f"expected second_inline_mission in missions, got: {missions!r}"


def test_dept_detail_page_renders_inline_missions(client, fixture_root):
    """/dept/<slug> must list the inline missions in its 'Recurring missions'
    section, not 'no missions declared'."""
    _make_dept_with_inline_missions(fixture_root)
    r = client.get("/dept/inline-only")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.text
    assert "echo_heartbeat" in body, \
        "expected inline mission id 'echo_heartbeat' to appear in /dept/inline-only"
    assert "second_inline_mission" in body, \
        "expected inline mission id 'second_inline_mission' to appear in /dept/inline-only"
    # And the fallback empty-state must NOT be shown
    assert "no missions declared" not in body.lower()


def test_list_missions_merges_inline_and_file_missions(app, fixture_root):
    """When both inline + file missions exist, both should be returned with
    de-duplication on id."""
    slug = "merged-missions"
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops",
                           "mandate": "merged-missions dept"},
            "layers": {"subscribed": [1]},
            "recurring_missions": [
                {"id": "inline_one", "layer": 1, "cadence": "every_2h"},
            ],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": slug,
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-20T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "missions").mkdir()
    (repo / "missions" / "file_two.yaml").write_text(
        yaml.safe_dump({"id": "file_two", "layer": 2,
                        "cadence": "every_4h"}, sort_keys=False),
        encoding="utf-8",
    )
    from console.services import github_reader
    missions = github_reader.list_missions(slug)
    flat = " ".join(
        m if isinstance(m, str) else (m.get("id", "") if isinstance(m, dict) else "")
        for m in missions
    )
    assert "inline_one" in flat
    assert "file_two" in flat
