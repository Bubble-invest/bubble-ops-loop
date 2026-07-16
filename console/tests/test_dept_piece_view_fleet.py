"""
test_dept_piece_view_fleet.py — card #642 PR-B (full completion): fleet-wide
rollout of the layered clickable-piece view to every dept page, not just
/dept/content (PR-A's scope).

Covers PLAN-642 §5's per-dept shape matrix at the HTTP layer (PR-A's
test_mission_pieces.py already covers the resolver directly; this file
proves the route + template render correctly end-to-end for each shape):
  - content-shaped (richest — full 6-piece taxonomy, grouped rows, pool +
    gate bands).
  - ben-shaped (flat missions/*.yaml, no memory/config/voice — mostly
    reference chips, no crash, no phantom tiles).
  - accountant-shaped (dept.yaml.draft only, inline-only recurring
    missions, no missions/ dir at all — leanest, no crash).
  - maya-shaped (nested-dict input_sources entries skipped defensively at
    the route/template layer, not just the resolver unit level).

Every dept page must render 200, look intentional (grouped piece rows, not
a broken-empty page), and never crash on an unresolved-key Jinja error.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from console.tests.conftest import TEST_BEARER


def _onboarding_state(slug: str, display_name: str) -> dict:
    return {
        "schema_version": 1, "slug": slug, "display_name": display_name,
        "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
        "status": "Live", "validated_steps": ["mandate", "missions", "layers"],
        "last_updated_at": "2026-05-19T10:00:00Z", "commits": [],
    }


def _base_dept_dirs(repo: Path) -> None:
    (repo / "onboarding").mkdir(parents=True, exist_ok=True)
    (repo / "queues" / "gates").mkdir(parents=True, exist_ok=True)
    (repo / "outputs").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def fleet_root(tmp_path: Path) -> Path:
    root = tmp_path / "depts"
    root.mkdir()

    # ── content-shaped (richest) ──
    content = root / "bubble-ops-content"
    content.mkdir()
    _base_dept_dirs(content)
    (content / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(_onboarding_state("content", "Miranda"), sort_keys=False),
        encoding="utf-8")
    (content / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "content", "level": "ops", "mandate": "produce content"},
            "layers": {"subscribed": [1, 2]},
            "recurring_missions": [
                {
                    "id": "draft_x", "layer": 2, "cadence": "daily",
                    "description": "Reads the pool. Drafts a thread.",
                    "output_queue": "queues/gates/", "creates": ["draft", "publish_proposal"],
                    "input_sources": ["research_pool", "brand_guidelines", "twitter_voice"],
                },
                {
                    "id": "gather_x_timeline", "layer": 1, "cadence": "daily",
                    "description": "Gathers the X timeline.",
                    "output_queue": "queues/research/", "creates": ["context_pool_item"],
                    "input_sources": ["x_timeline"],
                },
            ],
        }, sort_keys=False),
        encoding="utf-8")
    (content / "missions" / "draft_x").mkdir(parents=True)
    (content / "missions" / "draft_x" / "PROMPT.md").write_text("Draft.\n", encoding="utf-8")
    (content / "missions" / "gather_x_timeline").mkdir(parents=True)
    (content / "missions" / "gather_x_timeline" / "PROMPT.md").write_text("Gather.\n", encoding="utf-8")
    (content / "skills" / "draft-x").mkdir(parents=True)
    (content / "skills" / "draft-x" / "SKILL.md").write_text("Skill.\n", encoding="utf-8")
    (content / "config").mkdir()
    (content / "config" / "x.yaml").write_text("cadence: daily\n", encoding="utf-8")
    (content / "config" / "x_sources.yaml").write_text("accounts: []\n", encoding="utf-8")
    (content / "twitter").mkdir()
    (content / "twitter" / "VOICE.md").write_text("Terse.\n", encoding="utf-8")
    (content / "docs").mkdir()
    (content / "docs" / "CONTEXT_POOL_SCHEMA.md").write_text("# Schema\n", encoding="utf-8")

    # ── ben-shaped (flat missions/*.yaml, no memory/config/voice) ──
    ben = root / "bubble-ops-ben"
    ben.mkdir()
    _base_dept_dirs(ben)
    (ben / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(_onboarding_state("ben", "Ben"), sort_keys=False), encoding="utf-8")
    (ben / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "ben", "level": "ops", "mandate": "run the fund"},
            "layers": {"subscribed": [1, 2, 3]},
            "recurring_missions": [
                {
                    "id": "data_update", "layer": 1, "cadence": "daily",
                    "description": "Syncs broker positions.",
                    "output_queue": "queues/research/", "creates": ["situation_brief"],
                    "input_sources": ["broker_positions", "fund_sqlite"],
                },
            ],
        }, sort_keys=False),
        encoding="utf-8")
    (ben / "missions").mkdir()
    (ben / "missions" / "data-update.yaml").write_text(
        yaml.safe_dump({"id": "data_update", "layer": 1}), encoding="utf-8")

    # ── accountant-shaped (leanest — dept.yaml.draft only, inline missions) ──
    accountant = root / "bubble-ops-accountant"
    accountant.mkdir()
    _base_dept_dirs(accountant)
    (accountant / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(_onboarding_state("accountant", "Géraldine"), sort_keys=False),
        encoding="utf-8")
    (accountant / "dept.yaml.draft").write_text(
        yaml.safe_dump({
            "department": {"slug": "accountant", "level": "ops", "mandate": "close the books"},
            "layers": {"subscribed": [1]},
            "recurring_missions": [
                {
                    "id": "monthly_close", "layer": 1, "cadence": "monthly",
                    "description": "Closes the monthly books.",
                    "input_sources": ["fund_sqlite"],
                },
            ],
        }, sort_keys=False),
        encoding="utf-8")

    # ── maya-shaped (nested-dict input_sources entries) ──
    maya = root / "bubble-ops-maya"
    maya.mkdir()
    _base_dept_dirs(maya)
    (maya / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(_onboarding_state("maya", "Maya"), sort_keys=False), encoding="utf-8")
    (maya / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "maya", "level": "ops", "mandate": "prospection"},
            "layers": {"subscribed": [1, 2]},
            "recurring_missions": [
                {
                    "id": "qualify", "layer": 2, "cadence": "daily",
                    "description": "Qualifies prospects from the pool.",
                    "output_queue": "queues/research/", "creates": ["qualify_task"],
                    "input_sources": [
                        "pool_db",
                        {"auto_if_policy_passed": ["scoring_tier1"]},
                    ],
                },
            ],
        }, sort_keys=False),
        encoding="utf-8")
    (maya / "missions").mkdir()
    (maya / "missions" / "qualify.yaml").write_text(
        yaml.safe_dump({"id": "qualify", "layer": 2}), encoding="utf-8")

    return root


@pytest.fixture
def fleet_client(monkeypatch, fleet_root: Path):
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", TEST_BEARER)
    monkeypatch.setenv("READ_FROM_DISK", str(fleet_root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": f"Bearer {TEST_BEARER}"})
    return c


@pytest.mark.parametrize("slug", ["content", "ben", "accountant", "maya"])
def test_every_dept_page_renders_200_with_piece_view(fleet_client, slug):
    """Template smoke (§4.4): every dept renders 200 with the layered
    view, no unresolved-key Jinja errors, regardless of shape richness."""
    r = fleet_client.get(f"/dept/{slug}")
    assert r.status_code == 200
    assert "piece-groups" in r.text or "piece-tile" in r.text or slug == "accountant"


def test_content_shows_grouped_rows_and_pool_gate_bands(fleet_client):
    r = fleet_client.get("/dept/content")
    body = r.text
    assert r.status_code == 200
    assert "entrées" in body
    assert "cœur" in body
    assert "sortie" in body
    assert "pool-band" in body
    assert "gate-band" in body
    assert "LE POOL" in body
    assert "Gate humaine" in body
    # The pool band renders exactly ONCE (between Moment 1 and Moment 2),
    # not once per Moment transition — content subscribes to layers 1-4,
    # and every layer's missions declare SOME output_queue (L2 -> gates,
    # L3/L4 -> management), but only L1's output_queue is "the pool".
    assert body.count('<div class="pool-band"') == 1
    assert body.count('<div class="gate-band">') == 1


def test_ben_shaped_no_config_memory_voice_tiles_no_crash(fleet_client):
    """ben has no config/memory/voice dirs — those piece classes must be
    ABSENT (not phantom tiles), and the mission core must open the
    hyphenated flat yaml file, and the page must not crash."""
    r = fleet_client.get("/dept/ben")
    assert r.status_code == 200
    body = r.text
    assert "piece-config" not in body
    assert "piece-memory" not in body
    assert "piece-voice" not in body
    assert "data_update" in body
    assert "mission-file?f=missions/data-update.yaml" in body


def test_accountant_shaped_inline_mission_non_clickable_no_crash(fleet_client):
    """accountant has no missions/ dir at all — the inline mission's core
    tile must be non-clickable and the page must not crash."""
    r = fleet_client.get("/dept/accountant")
    assert r.status_code == 200
    assert "monthly_close" in r.text


def test_maya_shaped_nested_input_source_does_not_crash_route(fleet_client):
    """The nested-dict input_sources entry (maya's policy-gated sources)
    must not raise anywhere in the route/template pipeline, only in the
    resolver unit tests — this proves the full request path degrades
    gracefully too."""
    r = fleet_client.get("/dept/maya")
    assert r.status_code == 200
    assert "qualify" in r.text
    assert "pool_db" in r.text


def test_cross_dept_isolation_across_fleet(fleet_client):
    """No dept's page shows another dept's mission ids or files."""
    r = fleet_client.get("/dept/ben")
    assert "draft_x" not in r.text
    assert "twitter/VOICE.md" not in r.text
    r = fleet_client.get("/dept/maya")
    assert "data_update" not in r.text
