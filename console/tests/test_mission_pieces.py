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
    kind resolvable on disk (draft_x's real input_sources, PLAN-642 §2).

    PR-B fix: the config file is named `config/x.yaml` (mission-ID
    convention, `draft_x` -> `x`), NOT `config/brand_guidelines.yaml`.
    The original PR-A fixture invented the latter, which is why the
    config-tile bug shipped undetected — verified against the real
    bubble-ops-content repo (2026-07-16), ZERO of its 48 input_sources
    keys across 12 missions match a real config/*.yaml filename by
    literal key match; every config file is named after the mission/
    channel instead (see mission_pieces.py's module docstring)."""
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
    (repo / "skills" / "writing-investment-cases").mkdir(parents=True)
    (repo / "skills" / "writing-investment-cases" / "SKILL.md").write_text(
        "The paid-article process.\n", encoding="utf-8")

    (repo / "memory").mkdir()
    (repo / "memory" / "draft_x.md").write_text(
        "Memory body.\n", encoding="utf-8")

    (repo / "config").mkdir()
    (repo / "config" / "x.yaml").write_text(
        "cadence: daily\n", encoding="utf-8")

    (repo / "twitter").mkdir()
    (repo / "twitter" / "VOICE.md").write_text(
        "Terse, data-first.\n", encoding="utf-8")

    (repo / "WORKING_MEMORY.md").write_text("current focus\n", encoding="utf-8")

    (repo / "docs").mkdir()
    (repo / "docs" / "CONTEXT_POOL_SCHEMA.md").write_text(
        "# Pool schema\n", encoding="utf-8")

    (repo / "outputs" / "2026-07-16" / "missions" / "draft_x").mkdir(parents=True)
    (repo / "outputs" / "2026-07-16" / "missions" / "draft_x" / ".last-run").write_text(
        "2026-07-16T12:05:00Z\n", encoding="utf-8")

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
        "cadence": "daily",
        "description": "Reads the pool, writes via writing-investment-cases process too.",
        "output_queue": "queues/gates/",
        "creates": ["draft", "publish_proposal"],
        "input_sources": [
            "research_pool", "brand_guidelines", "twitter_voice",
            "working_memory", "draft_x_memory", "x_publish_history",
        ],
    }
    pieces = resolve_mission_pieces("content", mission)
    by_kind = {}
    for p in pieces:
        by_kind.setdefault(p["kind"], []).append(p)

    # mission core (📄) — PROMPT.md exists, clickable, grouped "cœur".
    assert by_kind["mission"][0]["clickable"] is True
    assert by_kind["mission"][0]["rel_path"] == "missions/draft_x/PROMPT.md"
    assert by_kind["mission"][0]["group"] == "coeur"

    # skill (🎓) — resolved via input_sources normalization AND/OR the
    # mission's own naming convention; either way draft-x/SKILL.md shows.
    # ALSO resolved via the description-mention scan (PR-B item 4):
    # writing-investment-cases is named in the description but not
    # declared in input_sources nor the mission's own naming convention.
    assert any(p["rel_path"] == "skills/draft-x/SKILL.md" for p in by_kind["skill"])
    assert any(p["rel_path"] == "skills/writing-investment-cases/SKILL.md"
               for p in by_kind["skill"])
    assert all(p["group"] == "coeur" for p in by_kind["skill"])

    # config (📇) — PR-B fix: resolved by MISSION-ID convention
    # (draft_x -> config/x.yaml), not by an input_sources key match
    # (brand_guidelines has no config/brand_guidelines.yaml on this
    # fixture, matching the real content repo's shape). Grouped "entrées".
    assert by_kind["config"][0]["rel_path"] == "config/x.yaml"
    assert by_kind["config"][0]["group"] == "entrees"
    # No config file is a phantom duplicate for the brand_guidelines key.
    assert len(by_kind["config"]) == 1

    # memory (🧠) — working_memory -> WORKING_MEMORY.md; draft_x_memory ->
    # memory/draft_x.md.
    memory_paths = {p["rel_path"] for p in by_kind["memory"]}
    assert "WORKING_MEMORY.md" in memory_paths
    assert "memory/draft_x.md" in memory_paths

    # voice (🎙️) — twitter_voice -> twitter/VOICE.md.
    assert by_kind["voice"][0]["rel_path"] == "twitter/VOICE.md"

    # reference (unresolved, muted, non-clickable) — research_pool and
    # x_publish_history have no openable file. brand_guidelines is now
    # ALSO unresolved as an input_sources key (it only resolves via the
    # mission-id config path above) — confirms the fixture stress-tests
    # the fix rather than confirming the old (wrong) assumption.
    reference_keys = {p["key"] for p in by_kind["reference"]}
    assert reference_keys == {"research_pool", "x_publish_history", "brand_guidelines"}
    assert all(p["clickable"] is False and p["rel_path"] is None
               for p in by_kind["reference"])
    assert all(p["group"] == "entrees" for p in by_kind["reference"])

    # output (📤) — PR-B item 3: the sortie tile from output_queue/creates,
    # always non-clickable (a queue destination, not a static file).
    assert "output" in by_kind
    assert by_kind["output"][0]["clickable"] is False
    assert by_kind["output"][0]["group"] == "sortie"
    assert "queues/gates" in by_kind["output"][0]["label"]
    assert "draft" in by_kind["output"][0]["label"]


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


# ── PR-B (#642 full completion) — the granularity-gap fixes ────────────────


def test_config_tile_resolves_by_mission_id_not_input_sources_key(content_shaped_repo, monkeypatch):
    """The config-tile bug's root cause, isolated: verified against the
    real bubble-ops-content repo, an L1 gather mission's config uses a
    `_sources`/`_topics` suffix on ONE TOKEN of the mission id
    (gather_newsletter_signal -> config/newsletter_sources.yaml), which
    must win over an unrelated mission's bare-name file that happens to
    share the same token (draft_newsletter -> config/newsletter.yaml) —
    the verb prefix disambiguates which one is THIS mission's config."""
    from console.services.mission_pieces import resolve_mission_pieces
    _reload_console_modules()

    repo = content_shaped_repo
    (repo / "config" / "newsletter.yaml").write_text("x: 1\n", encoding="utf-8")
    (repo / "config" / "newsletter_sources.yaml").write_text("x: 2\n", encoding="utf-8")
    (repo / "missions" / "gather_newsletter_signal").mkdir(parents=True)
    (repo / "missions" / "gather_newsletter_signal" / "PROMPT.md").write_text(
        "Gather newsletters.\n", encoding="utf-8")
    (repo / "missions" / "draft_newsletter").mkdir(parents=True)
    (repo / "missions" / "draft_newsletter" / "PROMPT.md").write_text(
        "Draft the newsletter.\n", encoding="utf-8")

    gather_pieces = resolve_mission_pieces(
        "content", {"id": "gather_newsletter_signal", "input_sources": []})
    draft_pieces = resolve_mission_pieces(
        "content", {"id": "draft_newsletter", "input_sources": []})

    gather_config = [p for p in gather_pieces if p["kind"] == "config"]
    draft_config = [p for p in draft_pieces if p["kind"] == "config"]
    assert gather_config and gather_config[0]["rel_path"] == "config/newsletter_sources.yaml"
    assert draft_config and draft_config[0]["rel_path"] == "config/newsletter.yaml"


def test_description_mentioned_skill_resolved_without_input_sources(content_shaped_repo):
    """PR-B item 4 (granularity gap): a skill named only in the mission's
    free-text description — not in input_sources, not matching the
    mission's own naming convention — must still resolve. Verified
    against the real content repo's draft_substack_ic (mentions
    writing-investment-cases + write-investment-teaser in prose) and
    publish_execution (mentions 4 publish-* skills in prose)."""
    from console.services.mission_pieces import resolve_mission_pieces

    mission = {
        "id": "draft_substack_ic",
        "description": "Drafts via the writing-investment-cases process.",
        "input_sources": [],
    }
    pieces = resolve_mission_pieces("content", mission)
    skill_paths = {p["rel_path"] for p in pieces if p["kind"] == "skill"}
    assert "skills/writing-investment-cases/SKILL.md" in skill_paths


def test_description_mention_scan_does_not_false_positive_on_prose(content_shaped_repo):
    """The description-mention scan must not fire on ordinary prose that
    happens to contain a skill's name as a substring of a longer word —
    only a whole-word (hyphen-boundary) match counts."""
    from console.services.mission_pieces import resolve_mission_pieces

    mission = {
        "id": "draft_x",
        "description": "This mentions draft-xtra-long-name-that-is-not-a-skill in passing.",
        "input_sources": [],
    }
    pieces = resolve_mission_pieces("content", mission)
    skill_labels = {p["label"] for p in pieces if p["kind"] == "skill"}
    # draft-x itself still resolves (own naming convention), but nothing
    # spurious was invented from the prose.
    assert skill_labels == {"draft-x"}


def test_mission_core_resolves_hyphenated_flat_yaml_filename(ben_shaped_repo):
    """PR-B fix: a flat missions/<id>.yaml file is conventionally
    HYPHENATED on disk even though the mission's own `id:` field (and
    dept.yaml's recurring_missions key) is underscored — verified on the
    real ben repo (`missions/data-update.yaml` for mission id
    `data_update`). The mission-core resolver must try both spellings,
    not just the literal underscored one."""
    from console.services.mission_pieces import resolve_mission_pieces
    _reload_console_modules()

    repo = ben_shaped_repo
    (repo / "missions" / "data-update.yaml").write_text(
        "id: data_update\nlayer: 1\n", encoding="utf-8")

    pieces = resolve_mission_pieces("ben", {"id": "data_update", "input_sources": []})
    core = pieces[0]
    assert core["kind"] == "mission"
    assert core["clickable"] is True
    assert core["rel_path"] == "missions/data-update.yaml"


def test_mission_status_actif_dormant_evenementiel_inconnu(content_shaped_repo):
    """PR-B item 8: status badge classification. content_shaped_repo's
    fixture gives draft_x a .last-run today (2026-07-16) -> 'actif'; an
    event-cadence mission is always 'événementiel' regardless of output
    history; a mission with no outputs/ entry at all is 'inconnu'."""
    from console.services.mission_pieces import mission_status
    from console.services.dept_registry import repo_path

    root = repo_path("content")
    assert mission_status(root, {"id": "draft_x", "cadence": "daily"}) == "actif"
    assert mission_status(
        root, {"id": "anything", "cadence": "event"}) == "événementiel"
    assert mission_status(
        root, {"id": "never_run_mission", "cadence": "daily"}) == "inconnu"


def test_mission_tagline_first_sentence_only(content_shaped_repo):
    """PR-B item 8: the tagline is the FIRST sentence only, not the whole
    description (keeps the mission card compact, matching the
    reference's one-line tagline under the mission id)."""
    from console.services.mission_pieces import mission_tagline

    mission = {
        "description": "Drafts one X thread per day. Reads the whole pool first. Never publishes directly."
    }
    assert mission_tagline(mission) == "Drafts one X thread per day."
    assert mission_tagline({"description": ""}) is None
    assert mission_tagline({}) is None


def test_resolve_layer_band_pool_and_generic(content_shaped_repo):
    """PR-B items 5+7: the connective band derives its queue path from the
    FROM layer's missions' own declared output_queue — content's L1
    writes queues/research/ (the reference's "LE POOL"), so `is_pool` is
    True and the schema deep-link resolves because docs/
    CONTEXT_POOL_SCHEMA.md exists on this fixture. A layer with no
    missions, or none declaring output_queue, gets no band at all."""
    from console.services.mission_pieces import resolve_layer_band

    l1_missions = [{"id": "gather_x_timeline", "output_queue": "queues/research/"}]
    band = resolve_layer_band("content", l1_missions)
    assert band is not None
    assert band["queue_path"] == "queues/research"
    assert band["is_pool"] is True
    assert band["schema_rel_path"] == "docs/CONTEXT_POOL_SCHEMA.md"

    assert resolve_layer_band("content", []) is None
    assert resolve_layer_band("content", [{"id": "x"}]) is None  # no output_queue declared


def test_resolve_layer_band_generic_non_pool_queue(ben_shaped_repo):
    """A dept whose L1 output_queue is NOT queues/research/ still gets a
    band (the "equivalent" instruction, item 5) but `is_pool` is False
    and there's no schema deep-link (ben has no docs/CONTEXT_POOL_SCHEMA.md)."""
    from console.services.mission_pieces import resolve_layer_band

    band = resolve_layer_band("ben", [{"id": "data_update", "output_queue": "queues/situation/"}])
    assert band is not None
    assert band["queue_path"] == "queues/situation"
    assert band["is_pool"] is False
    assert band["schema_rel_path"] is None


def test_resolve_gate_band_fires_on_gate_output_queue_after_highest_layer():
    """PR-B item 6: the gate band fires whenever any mission declares
    output_queue: queues/gates/, placed AFTER the highest layer among
    those missions — generic, not hardcoded to content's L2/L3 split."""
    from console.services.mission_pieces import resolve_gate_band

    missions = [
        {"id": "draft_x", "layer": 2, "output_queue": "queues/gates/"},
        {"id": "draft_linkedin", "layer": 2, "output_queue": "queues/gates/"},
        {"id": "publish_execution", "layer": 3, "output_queue": "queues/management/"},
    ]
    band = resolve_gate_band(missions)
    assert band is not None
    assert band["after_layer"] == 2
    assert band["gate_url_kind"] == "publish_proposal"

    # No mission gates -> no band.
    assert resolve_gate_band([{"id": "x", "layer": 1, "output_queue": "queues/research/"}]) is None
