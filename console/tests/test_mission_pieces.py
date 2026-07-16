"""
test_mission_pieces.py — the piece-resolution service for card #642 (layered
clickable-piece view). Model on test_missions_and_layers_in_ui.py's
direct-import + READ_FROM_DISK pattern (services/*, not the HTTP layer).

Covers PLAN-642 §4's per-dept fixture matrix:
  - content-shaped: full 6-piece taxonomy resolvable on disk.
  - ben-shaped: flat missions/*.yaml + skills/, no memory/config/voice.
  - accountant-shaped: no missions/ dir at all, inline-only mission dict.
  - maya-shaped: input_sources with a non-string (nested policy dict) entry
    must be skipped defensively, not raise (§0-bis refinement 2).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml


def _reload_console_modules():
    for k in list(sys.modules):
        if k.startswith("console"):
            del sys.modules[k]


@pytest.fixture
def content_shaped_repo(tmp_path: Path, monkeypatch) -> Path:
    """content-shaped: missions/<id>/PROMPT.md, skills/<name>/SKILL.md,
    memory/<id>.md, config/<key>.yaml, <channel>/VOICE.md — every piece
    kind resolvable on disk (draft_x's real input_sources, PLAN-642 §2)."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    _reload_console_modules()
    repo = tmp_path / "bubble-ops-content"
    repo.mkdir()

    (repo / "missions" / "draft_x").mkdir(parents=True)
    (repo / "missions" / "draft_x" / "PROMPT.md").write_text(
        "Draft an X thread.\n", encoding="utf-8")

    (repo / "skills" / "draft-x").mkdir(parents=True)
    (repo / "skills" / "draft-x" / "SKILL.md").write_text(
        "Skill body.\n", encoding="utf-8")

    (repo / "memory").mkdir()
    (repo / "memory" / "draft_x.md").write_text(
        "Memory body.\n", encoding="utf-8")

    (repo / "config").mkdir()
    (repo / "config" / "brand_guidelines.yaml").write_text(
        "voice: contrarian\n", encoding="utf-8")

    (repo / "twitter").mkdir()
    (repo / "twitter" / "VOICE.md").write_text(
        "Terse, data-first.\n", encoding="utf-8")

    (repo / "WORKING_MEMORY.md").write_text("current focus\n", encoding="utf-8")

    return repo


@pytest.fixture
def ben_shaped_repo(tmp_path: Path, monkeypatch) -> Path:
    """ben-shaped: flat missions/<id>.yaml, skills/ present, NO memory/
    NO config/ NO voice — mission core opens the .yaml; skills clickable;
    memory/config/voice tiles simply absent (unresolved -> reference chip),
    not phantom entries."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    _reload_console_modules()
    repo = tmp_path / "bubble-ops-ben"
    repo.mkdir()

    (repo / "missions").mkdir()
    (repo / "missions" / "fund_thesis_scorecard.yaml").write_text(
        yaml.safe_dump({"id": "fund_thesis_scorecard", "layer": 1}),
        encoding="utf-8")

    (repo / "skills" / "fund-thesis-scorecard").mkdir(parents=True)
    (repo / "skills" / "fund-thesis-scorecard" / "SKILL.md").write_text(
        "Scorecard skill.\n", encoding="utf-8")

    return repo


@pytest.fixture
def accountant_shaped_repo(tmp_path: Path, monkeypatch) -> Path:
    """accountant-shaped: NO missions/ dir at all — inline mission dict
    only, core tile must be non-clickable, no crash."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    _reload_console_modules()
    repo = tmp_path / "bubble-ops-accountant"
    repo.mkdir()
    (repo / "dept.yaml.draft").write_text(
        yaml.safe_dump({"department": {"slug": "accountant", "level": "ops"}}),
        encoding="utf-8")
    return repo


def test_content_shaped_resolves_all_six_piece_kinds(content_shaped_repo):
    from console.services.mission_pieces import resolve_mission_pieces

    mission = {
        "id": "draft_x",
        "input_sources": [
            "research_pool", "brand_guidelines", "twitter_voice",
            "working_memory", "draft_x_memory", "x_publish_history",
        ],
    }
    pieces = resolve_mission_pieces("content", mission)
    by_kind = {}
    for p in pieces:
        by_kind.setdefault(p["kind"], []).append(p)

    # mission core (📄) — PROMPT.md exists, clickable.
    assert by_kind["mission"][0]["clickable"] is True
    assert by_kind["mission"][0]["rel_path"] == "missions/draft_x/PROMPT.md"

    # skill (🎓) — resolved via input_sources normalization AND/OR the
    # mission's own naming convention; either way draft-x/SKILL.md shows.
    assert any(p["rel_path"] == "skills/draft-x/SKILL.md" for p in by_kind["skill"])

    # config (📇) — brand_guidelines -> config/brand_guidelines.yaml.
    assert by_kind["config"][0]["rel_path"] == "config/brand_guidelines.yaml"

    # memory (🧠) — working_memory -> WORKING_MEMORY.md; draft_x_memory ->
    # memory/draft_x.md.
    memory_paths = {p["rel_path"] for p in by_kind["memory"]}
    assert "WORKING_MEMORY.md" in memory_paths
    assert "memory/draft_x.md" in memory_paths

    # voice (🎙️) — twitter_voice -> twitter/VOICE.md.
    assert by_kind["voice"][0]["rel_path"] == "twitter/VOICE.md"

    # reference (unresolved, muted, non-clickable) — research_pool and
    # x_publish_history have no openable file.
    reference_keys = {p["key"] for p in by_kind["reference"]}
    assert reference_keys == {"research_pool", "x_publish_history"}
    assert all(p["clickable"] is False and p["rel_path"] is None
               for p in by_kind["reference"])


def test_ben_shaped_mission_core_opens_flat_yaml(ben_shaped_repo):
    from console.services.mission_pieces import resolve_mission_pieces

    mission = {"id": "fund_thesis_scorecard",
               "input_sources": ["fund_thesis_scorecard", "broker_apis", "fund_sqlite"]}
    pieces = resolve_mission_pieces("ben", mission)
    by_kind = {}
    for p in pieces:
        by_kind.setdefault(p["kind"], []).append(p)

    # Mission core resolves to the flat missions/<id>.yaml, not a PROMPT.md.
    assert by_kind["mission"][0]["clickable"] is True
    assert by_kind["mission"][0]["rel_path"] == "missions/fund_thesis_scorecard.yaml"

    # Skill resolves (fund_thesis_scorecard input_sources key normalizes to
    # the hyphenated skills/ dir).
    assert by_kind["skill"][0]["rel_path"] == "skills/fund-thesis-scorecard/SKILL.md"

    # broker_apis / fund_sqlite are abstract (no on-disk file) -> reference
    # chips, not absent, not crashing.
    assert "reference" in by_kind
    reference_keys = {p["key"] for p in by_kind["reference"]}
    assert reference_keys == {"broker_apis", "fund_sqlite"}

    # No memory/config/voice piece kinds at all for this dept shape.
    assert "memory" not in by_kind
    assert "config" not in by_kind
    assert "voice" not in by_kind


def test_accountant_shaped_mission_core_non_clickable_no_crash(accountant_shaped_repo):
    from console.services.mission_pieces import resolve_mission_pieces

    mission = {"id": "monthly_close", "description": "Close the books.",
               "input_sources": ["fund_sqlite"]}
    pieces = resolve_mission_pieces("accountant", mission)
    core = pieces[0]
    assert core["kind"] == "mission"
    assert core["clickable"] is False
    assert core["rel_path"] is None
    # Doesn't crash, still returns the unresolved input_sources key as a
    # reference chip.
    assert any(p["kind"] == "reference" and p["key"] == "fund_sqlite" for p in pieces)


def test_maya_shaped_nested_policy_key_skipped_defensively(ben_shaped_repo):
    """§0-bis refinement 2: maya's input_sources sometimes nests a policy
    dict (auto_if_policy_passed / auto_with_veto_window) instead of a flat
    string. The resolver must skip non-string entries, never attribute-error."""
    from console.services.mission_pieces import resolve_mission_pieces

    mission = {
        "id": "fund_thesis_scorecard",
        "input_sources": [
            "pool_db",
            {"auto_if_policy_passed": ["scoring_tier1"]},  # nested, non-string
            "linkedin_feed",
        ],
    }
    # Must not raise.
    pieces = resolve_mission_pieces("ben", mission)
    keys = [p.get("key") for p in pieces]
    assert "pool_db" in keys
    assert "linkedin_feed" in keys
    # The nested dict entry produced no piece of its own.
    assert not any(isinstance(k, dict) for k in keys)


def test_unknown_dept_returns_empty_list(tmp_path, monkeypatch):
    """A slug with no repo on disk (repo_path returns None) must degrade
    to an empty piece list, never raise."""
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    _reload_console_modules()
    from console.services.mission_pieces import resolve_mission_pieces

    pieces = resolve_mission_pieces("nonexistent-dept", {"id": "x", "input_sources": []})
    assert pieces == []


def test_missing_mission_id_returns_empty_list(content_shaped_repo):
    """A mission dict with no id (malformed) degrades to an empty list
    rather than raising or emitting a nonsensical tile."""
    from console.services.mission_pieces import resolve_mission_pieces

    pieces = resolve_mission_pieces("content", {"input_sources": []})
    assert pieces == []


def test_allowlist_downgrades_a_resolved_piece_to_non_clickable(content_shaped_repo):
    """§0-bis refinement 5 / the guard-lockstep property: if the caller
    passes an allowlist that does NOT include a resolved rel_path, the
    piece must be downgraded to non-clickable rather than rendering a
    clickable tile the guard would 404 on."""
    from console.services.mission_pieces import resolve_mission_pieces

    mission = {"id": "draft_x", "input_sources": ["brand_guidelines"]}
    # Empty allowlist -> nothing should end up clickable, even though the
    # files exist on disk.
    pieces = resolve_mission_pieces("content", mission, allowlist=set())
    assert all(p["clickable"] is False for p in pieces)
    assert all(p["rel_path"] is None for p in pieces)
