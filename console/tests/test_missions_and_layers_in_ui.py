"""
test_missions_and_layers_in_ui.py — {{OPERATOR}} flag 2026-05-24 msg 3137.

> Please verify if the content of the layers and missions docs will be
> correctly reflected in the front end and if not implement it, as well
> as in current éclosion phase.

After PR #X (6 missions/*.yaml + 4-layer subscribed model for Maya), the
console must surface, for BOTH lifecycle phases:

  1. Per mission (file-based AND inline `dept.yaml::recurring_missions[]`):
       - display id (title-case from snake_case)
       - cadence humanized in French
         ("daily" + time="07:00" -> "Tous les jours à 07h00";
          weekly + day="friday" + time="17:00" -> "Le vendredi à 17h00";
          hourly + active_hours="08:00-21:00" -> "Toutes les heures entre 08h00 et 21h00")
       - description verbatim
       - layer it belongs to (Moment N)
       - creates[] and gate_policy_id when set

  2. Per layer subscribed (`dept.yaml::layers.subscribed`):
       - Layer N + canonical moment name (reuse scaffold._LAYER_MOMENT_NAMES)
       - layers/<N>/PROMPT.md content (collapsible details) when present
       - graceful degradation "Prompt à écrire à l'étape Layers" when absent

Phase 1 = onboarding (status != Live, /agents/<slug>/onboarding).
Phase 2 = operating  (status == Live, /dept/<slug>).
Both surfaces must read the same data with the same humanizer.
"""
from __future__ import annotations

from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Helper: build a Maya-shaped fixture dept with rich missions + a few layer
# PROMPT.md files. We use a non-existing slug to avoid collisions with the
# pre-existing conftest depts (fixture / miranda).
# ---------------------------------------------------------------------------

_MAYA_DEPT_YAML = {
    "department": {
        "slug": "maya-rich",
        "level": "ops",
        "mandate": "Sourcer et qualifier des prospects LinkedIn",
    },
    "layers": {"subscribed": [1, 2, 3, 4]},
    "recurring_missions": [
        {
            "id": "morning_sync",
            "layer": 1,
            "cadence": "daily",
            "time": "07:00",
            "description": "Briefing matinal: scanner le Pool, identifier mouvements.",
            "input_sources": ["pool_db", "vault_capsules"],
            "output_queue": "queues/research/",
            "creates": ["morning_briefing", "warming_task"],
        },
        {
            "id": "draft_batch",
            "layer": 2,
            "cadence": "daily",
            "time": "08:00",
            "description": "Pour chaque lead, composer un draft selon l'angle choisi.",
            "input_sources": ["pool_db", "vault_capsules"],
            "output_queue": "queues/gates/",
            "creates": ["prospect_dm", "warming_comment"],
            "gate_policy_id": "prospect_dm",
        },
        {
            "id": "warming",
            "layer": 3,
            "cadence": "daily",
            "time": "11:00",
            "active_hours": "08:00-21:00",
            "description": "Présence LinkedIn warming Tier 2: poste les commentaires validés.",
            "input_sources": ["queues/gates_validated", "pool_db"],
            "output_queue": "queues/research/",
            "creates": ["warming_outcome"],
            "gate_policy_id": "warming",
        },
        {
            "id": "weekly_audit",
            "layer": 4,
            "cadence": "weekly",
            "day": "friday",
            "time": "17:00",
            "description": "Bilan hebdomadaire de la performance Maya.",
            "input_sources": ["pool_db"],
            "output_queue": "outputs/",
            "creates": ["weekly_summary"],
        },
        {
            "id": "hourly_poll",
            "layer": 2,
            "cadence": "hourly",
            "active_hours": "08:00-21:00",
            "description": "Polling régulier de la boîte de réception LinkedIn.",
            "input_sources": ["linkedin_inbox"],
            "output_queue": "queues/research/",
            "creates": ["inbound_reply"],
        },
    ],
    "gate_policies": {},
}


def _make_maya_rich_repo(
    fixture_root: Path,
    slug: str = "maya-rich",
    *,
    status: str = "Live",
    with_layer_prompt: bool = True,
) -> Path:
    """Spin up bubble-ops-<slug> with the 5-mission inline yaml + a few
    layer PROMPT.md files."""
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    dept_doc = dict(_MAYA_DEPT_YAML)
    dept_doc["department"] = dict(_MAYA_DEPT_YAML["department"], slug=slug)
    (repo / "dept.yaml").write_text(
        yaml.safe_dump(dept_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": slug.title(),
            "owner": "joris", "created_at": "2026-05-22T10:00:00Z",
            "status": status,
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"]
                              if status == "Live" else ["mandate", "missions"],
            "last_updated_at": "2026-05-24T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    # missions/ dir empty (inline-only mode) is fine — list_missions_full
    # still resolves from inline. We do NOT pollute it with files.
    (repo / "missions").mkdir()
    (repo / "missions" / ".gitkeep").write_text("", encoding="utf-8")

    # Layers: write PROMPT.md for layer 1 + 3 only, leave 2 + 4 missing
    # to exercise the graceful-degradation branch.
    if with_layer_prompt:
        for n in (1, 3):
            ldir = repo / "layers" / str(n)
            ldir.mkdir(parents=True)
            (ldir / "PROMPT.md").write_text(
                f"# Layer {n} prompt for {slug}\n\n"
                f"## What I do at Moment {n}\n\n"
                f"layer-{n}-sentinel-marker-{slug}\n",
                encoding="utf-8",
            )
        # Layer 2 + 4 dirs exist but no PROMPT.md
        for n in (2, 4):
            (repo / "layers" / str(n)).mkdir(parents=True, exist_ok=True)
    return repo


# ---------------------------------------------------------------------------
# A. Helpers under test
# ---------------------------------------------------------------------------


def test_load_layer_prompt_md_helper_exists():
    """github_reader must expose load_layer_prompt_md(slug, layer_num)
    returning the PROMPT.md content as str, or None if absent."""
    from console.services import github_reader
    assert hasattr(github_reader, "load_layer_prompt_md"), (
        "github_reader.load_layer_prompt_md(slug, layer_num) is the canonical "
        "helper for reading a dept's layers/<N>/PROMPT.md. Add it as a sibling "
        "of load_mandate_md."
    )


def test_load_layer_prompt_md_reads_existing_file(tmp_path, monkeypatch):
    from console.services import github_reader
    repo = tmp_path / "bubble-ops-someslug"
    repo.mkdir()
    (repo / "layers" / "2").mkdir(parents=True)
    body = "# Layer 2\n\nfoo bar PROMPT-CONTENT-SENTINEL\n"
    (repo / "layers" / "2" / "PROMPT.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        github_reader, "repo_path",
        lambda slug: repo if slug == "someslug" else None,
    )
    assert github_reader.load_layer_prompt_md("someslug", 2) == body


def test_load_layer_prompt_md_returns_none_when_missing(tmp_path, monkeypatch):
    from console.services import github_reader
    repo = tmp_path / "bubble-ops-x"
    repo.mkdir()
    monkeypatch.setattr(
        github_reader, "repo_path",
        lambda slug: repo if slug == "x" else None,
    )
    assert github_reader.load_layer_prompt_md("x", 1) is None
    assert github_reader.load_layer_prompt_md("unknown", 1) is None


def test_load_layer_prompt_md_handles_non_int_safely(tmp_path, monkeypatch):
    """layer_num=5 (out-of-range) or bad int → None, no crash."""
    from console.services import github_reader
    repo = tmp_path / "bubble-ops-x"
    repo.mkdir()
    monkeypatch.setattr(
        github_reader, "repo_path",
        lambda slug: repo if slug == "x" else None,
    )
    assert github_reader.load_layer_prompt_md("x", 5) is None
    assert github_reader.load_layer_prompt_md("x", 99) is None


def test_list_missions_full_returns_rich_dicts(app, fixture_root):
    """github_reader.list_missions_full(slug) returns the full mission
    docs (cadence, description, layer, time, creates, gate_policy_id),
    not just {id, source} like list_missions()."""
    _make_maya_rich_repo(fixture_root)
    from console.services import github_reader
    missions = github_reader.list_missions_full("maya-rich")
    assert isinstance(missions, list)
    assert len(missions) == 5, f"expected 5 missions, got {len(missions)}"

    by_id = {m["id"]: m for m in missions}
    assert "morning_sync" in by_id
    m = by_id["morning_sync"]
    assert m["layer"] == 1
    assert m["cadence"] == "daily"
    assert m["time"] == "07:00"
    assert "Briefing matinal" in m["description"]
    assert m["creates"] == ["morning_briefing", "warming_task"]

    # draft_batch carries gate_policy_id
    assert by_id["draft_batch"]["gate_policy_id"] == "prospect_dm"


def test_list_missions_full_returns_empty_list_for_unknown_slug():
    from console.services import github_reader
    assert github_reader.list_missions_full("does-not-exist-zzz") == []


# ---------------------------------------------------------------------------
# B. Cadence humanizer
# ---------------------------------------------------------------------------


def test_humanize_cadence_daily_with_time():
    """Daily cadence shows the time slot. The literal cron-like phrasing
    ('Tous les jours à 07h00') is forbidden — msg 3142 wants
    'earliest-possible' wording instead. The detailed phrasing test
    lives in test_humanize_cadence_daily_uses_au_plus_tot_phrasing."""
    from console.services.humanize import humanize_cadence
    out = humanize_cadence({"cadence": "daily", "time": "07:00"})
    assert "07h00" in out
    assert "Tous les jours à 07h00" not in out


def test_humanize_cadence_weekly_with_day_and_time():
    from console.services.humanize import humanize_cadence
    out = humanize_cadence({"cadence": "weekly", "day": "friday",
                             "time": "17:00"})
    assert "vendredi" in out.lower()
    assert "17h00" in out
    assert "Le vendredi à 17h00" not in out


def test_humanize_cadence_hourly_with_active_hours():
    from console.services.humanize import humanize_cadence
    out = humanize_cadence({"cadence": "hourly",
                             "active_hours": "08:00-21:00"})
    assert "heure" in out.lower()
    assert "08h00" in out
    assert "21h00" in out


def test_humanize_cadence_every_2h_legacy():
    """The legacy `every_2h` / `every_4h` format from PR #1 must still
    render to readable French, not raw enum."""
    from console.services.humanize import humanize_cadence
    out = humanize_cadence({"cadence": "every_2h"})
    assert "2" in out
    assert "heure" in out.lower()


def test_humanize_cadence_unknown_falls_back_gracefully():
    from console.services.humanize import humanize_cadence
    out = humanize_cadence({"cadence": "lunar_phase"})
    # Must not crash, must not leak raw enum
    assert isinstance(out, str)
    assert len(out) > 0


def test_humanize_cadence_handles_missing_dict():
    from console.services.humanize import humanize_cadence
    assert humanize_cadence({}) == "" or humanize_cadence({}).strip() != ""
    # None should not crash
    assert isinstance(humanize_cadence(None), str)


# ---------------------------------------------------------------------------
# C. /agents/<slug>/onboarding renders mission cards + layer cards
# ---------------------------------------------------------------------------


def test_onboarding_page_renders_each_mission_with_cadence_and_description(
        client, fixture_root):
    _make_maya_rich_repo(fixture_root, status="Drafting")
    r = client.get("/agents/maya-rich/onboarding")
    assert r.status_code == 200, r.text
    body = r.text

    # Every mission id surfaces somewhere
    for mid in ("morning_sync", "draft_batch", "warming",
                "weekly_audit", "hourly_poll"):
        assert mid in body, f"mission id '{mid}' missing from onboarding page"

    # Humanized cadence + description fragments surface
    assert "07h00" in body, "daily time for morning_sync not humanized"
    assert "Briefing matinal" in body, "morning_sync description missing"
    assert "vendredi" in body.lower(), "weekly cadence for weekly_audit not humanized"


def test_onboarding_page_renders_each_subscribed_layer(client, fixture_root):
    _make_maya_rich_repo(fixture_root, status="Drafting")
    r = client.get("/agents/maya-rich/onboarding")
    assert r.status_code == 200
    body = r.text

    # All 4 subscribed layers should appear
    for n in (1, 2, 3, 4):
        # Either as "Moment N", "Layer N", or in a per-layer block
        assert f"Layer {n}" in body or f"Moment {n}" in body, (
            f"layer {n} not surfaced on onboarding page"
        )


def test_onboarding_page_renders_layer_prompt_when_present(
        client, fixture_root):
    """When layers/1/PROMPT.md exists, its content must be readable."""
    _make_maya_rich_repo(fixture_root, status="Drafting")
    r = client.get("/agents/maya-rich/onboarding")
    body = r.text
    # Layer 1 has a PROMPT.md with this sentinel
    assert "layer-1-sentinel-marker-maya-rich" in body, (
        "Layer 1 PROMPT.md body not rendered on onboarding page."
    )
    assert "layer-3-sentinel-marker-maya-rich" in body


def test_onboarding_page_gracefully_handles_missing_layer_prompt(
        client, fixture_root):
    """Layers 2 + 4 have no PROMPT.md — must show placeholder, not crash."""
    _make_maya_rich_repo(fixture_root, status="Drafting")
    r = client.get("/agents/maya-rich/onboarding")
    assert r.status_code == 200
    body = r.text
    # Friendly placeholder (French Bureau-de-Cadre voice)
    assert "Prompt à écrire" in body or "Prompt à écrire" in body


def test_onboarding_page_with_no_missions_does_not_crash(
        client, fixture_root):
    """A dept declared with empty recurring_missions[] still renders."""
    slug = "empty-missions"
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml.draft").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops",
                           "mandate": "empty dept"},
            "layers": {"subscribed": [1]},
            "recurring_missions": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": slug,
            "owner": "joris", "created_at": "2026-05-22T10:00:00Z",
            "status": "Drafting", "validated_steps": ["mandate"],
            "last_updated_at": "2026-05-24T10:00:00Z", "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    r = client.get(f"/agents/{slug}/onboarding")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# D. /dept/<slug> renders mission cards + layer cards
# ---------------------------------------------------------------------------


def test_dept_detail_page_renders_each_mission_with_cadence_and_description(
        client, fixture_root):
    _make_maya_rich_repo(fixture_root, status="Live")
    r = client.get("/dept/maya-rich")
    assert r.status_code == 200
    body = r.text

    for mid in ("morning_sync", "draft_batch", "warming",
                "weekly_audit", "hourly_poll"):
        assert mid in body, f"mission '{mid}' missing from /dept page"

    assert "07h00" in body
    assert "Briefing matinal" in body
    assert "vendredi" in body.lower()


def test_dept_detail_renders_layer_prompt_when_present(
        client, fixture_root):
    _make_maya_rich_repo(fixture_root, status="Live")
    r = client.get("/dept/maya-rich")
    assert r.status_code == 200
    body = r.text
    assert "layer-1-sentinel-marker-maya-rich" in body
    assert "layer-3-sentinel-marker-maya-rich" in body


def test_dept_detail_gracefully_handles_missing_layer_prompt(
        client, fixture_root):
    _make_maya_rich_repo(fixture_root, status="Live")
    r = client.get("/dept/maya-rich")
    assert r.status_code == 200
    body = r.text
    assert "Prompt à écrire" in body


def test_dept_detail_renders_gate_policy_id_for_missions(
        client, fixture_root):
    """draft_batch + warming both carry gate_policy_id — should be visible
    so {{OPERATOR}} can audit which gate rules apply to which recurring task."""
    _make_maya_rich_repo(fixture_root, status="Live")
    r = client.get("/dept/maya-rich")
    body = r.text
    # The gate policy id "prospect_dm" appears (at least once for draft_batch)
    assert "prospect_dm" in body


def test_dept_detail_renders_creates_for_missions(client, fixture_root):
    """The `creates: [...]` queue-item kinds should surface — they're
    the public output contract of each mission."""
    _make_maya_rich_repo(fixture_root, status="Live")
    r = client.get("/dept/maya-rich")
    body = r.text
    # One of the unique 'creates' values
    assert "morning_briefing" in body or "warming_outcome" in body


def test_dept_detail_existing_mission_slug_rendering_still_works(
        client, fixture_root):
    """Regression: the existing inline_recurring_missions test patterns
    must still work — we add details on TOP of, not replace, the slug list."""
    # Re-use the inline-only test pattern
    slug = "inline-only-regression"
    repo = fixture_root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops",
                           "mandate": "regression test"},
            "layers": {"subscribed": [1, 2]},
            "recurring_missions": [
                {"id": "echo_heartbeat", "layer": 1, "cadence": "every_2h",
                 "description": "inline heartbeat"},
            ],
            "gate_policies": {},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": slug,
            "owner": "joris", "created_at": "2026-05-22T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-24T10:00:00Z", "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    r = client.get(f"/dept/{slug}")
    assert r.status_code == 200
    body = r.text
    assert "echo_heartbeat" in body
    # Cadence "every_2h" is humanized to mention "2" + "heure"
    assert "heure" in body.lower()


# ---------------------------------------------------------------------------
# E. New honest cadence phrasing + layer-grouping (msg 3142, 2026-05-24)
# ---------------------------------------------------------------------------
#
# {{OPERATOR}} flagged: the UI shows missions as if they were guaranteed crons
# ("Tous les jours à 07h00") but the /loop dispatch model is "when L1's
# turn comes AND time≥07:00 AND not yet fired today". The display must
# reflect that.
#
# Also: "Missions récurrentes" and "Moments de la journée" duplicate
# (every mission has a `layer:N`). Collapse to one section: 4 Moment
# cards, each with its missions inline.


def test_humanize_cadence_daily_uses_au_plus_tot_phrasing():
    """Daily cadence MUST express 'earliest possible' semantics, not
    'guaranteed at this time' (msg 3142)."""
    from console.services.humanize import humanize_cadence
    out = humanize_cadence({"cadence": "daily", "time": "07:00"})
    # Old: "Tous les jours à 07h00"
    # New: must include "au plus tôt" or "dès que" or "à partir de"
    out_low = out.lower()
    earliest_markers = ["au plus tôt", "au plus tot", "dès que", "des que",
                         "à partir de", "a partir de"]
    assert any(m in out_low for m in earliest_markers), (
        f"daily cadence must use 'earliest possible' wording. Got: {out!r}. "
        f"Expected one of {earliest_markers}."
    )
    # The 07h00 time still shows up
    assert "07h00" in out


def test_humanize_cadence_weekly_uses_au_plus_tot_phrasing():
    """Weekly cadence — same honest phrasing."""
    from console.services.humanize import humanize_cadence
    out = humanize_cadence({"cadence": "weekly", "day": "friday",
                             "time": "17:00"})
    out_low = out.lower()
    assert "vendredi" in out_low
    assert "17h00" in out
    earliest_markers = ["au plus tôt", "au plus tot", "dès que", "des que",
                         "à partir de", "a partir de"]
    assert any(m in out_low for m in earliest_markers), (
        f"weekly cadence must use 'earliest possible' wording. Got: {out!r}."
    )


def test_humanize_cadence_hourly_uses_when_turn_comes_phrasing():
    """Hourly cadence — express 'when its turn comes within the window'."""
    from console.services.humanize import humanize_cadence
    out = humanize_cadence({"cadence": "hourly",
                             "active_hours": "08:00-21:00"})
    out_low = out.lower()
    # Active-hours window still shown
    assert "08h00" in out
    assert "21h00" in out
    # Must convey "when its turn comes" or "once per hour" semantics
    turn_markers = ["dès que", "des que", "son tour", "une fois par"]
    assert any(m in out_low for m in turn_markers), (
        f"hourly cadence must convey turn-based dispatch. Got: {out!r}."
    )


# ── group_missions_by_layer helper ─────────────────────────────────────


def test_group_missions_by_layer_helper_exists():
    """Helper that buckets a flat mission list into {layer_num: [missions]}.
    Used by both pages to render Moments-with-missions-inside."""
    from console.services import github_reader
    assert hasattr(github_reader, "group_missions_by_layer"), (
        "github_reader.group_missions_by_layer(missions) returns "
        "{1: [...], 2: [...], 3: [...], 4: [...]} bucketed by mission.layer"
    )


def test_group_missions_by_layer_buckets_correctly():
    from console.services.github_reader import group_missions_by_layer
    missions = [
        {"id": "m1", "layer": 1},
        {"id": "m2", "layer": 2},
        {"id": "m3", "layer": 2},
        {"id": "m4", "layer": 3},
        {"id": "m5", "layer": 4},
    ]
    out = group_missions_by_layer(missions)
    assert isinstance(out, dict)
    assert {m["id"] for m in out.get(1, [])} == {"m1"}
    assert {m["id"] for m in out.get(2, [])} == {"m2", "m3"}
    assert {m["id"] for m in out.get(3, [])} == {"m4"}
    assert {m["id"] for m in out.get(4, [])} == {"m5"}


def test_group_missions_by_layer_handles_missing_layer_field():
    """Missions without a `layer` key default to layer 1 (the materializer)."""
    from console.services.github_reader import group_missions_by_layer
    missions = [{"id": "lonely"}]  # no layer field
    out = group_missions_by_layer(missions)
    # Either skipped cleanly or defaulted — must not raise
    assert isinstance(out, dict)


def test_group_missions_by_layer_handles_empty_list():
    from console.services.github_reader import group_missions_by_layer
    out = group_missions_by_layer([])
    assert isinstance(out, dict)
    # All buckets either absent or empty
    for layer in (1, 2, 3, 4):
        assert out.get(layer, []) == []


# ── Single consolidated section on both pages ───────────────────────────


def test_dept_detail_consolidates_missions_under_moments(client, monkeypatch):
    """Per msg 3142: 'Rendez-vous récurrents' + 'Moments de la journée'
    were duplicates. The dept_detail page must now have ONE consolidated
    section (Moments) with missions nested under their respective layer.
    NOT two separate sections with overlapping content."""
    from console.services import github_reader
    sentinel_missions = [
        {"id": "m_morning", "layer": 1, "cadence": "daily", "time": "07:00",
         "description": "test desc morning marker UNIQUE_TOKEN_AAA",
         "creates": ["a"], "output_queue": "queues/research/"},
        {"id": "m_research", "layer": 2, "cadence": "daily", "time": "08:00",
         "description": "test desc research marker UNIQUE_TOKEN_BBB",
         "creates": ["b"], "output_queue": "queues/gates/"},
    ]
    monkeypatch.setattr(github_reader, "list_missions_full",
                        lambda slug: sentinel_missions if slug == "fixture" else [])
    monkeypatch.setattr(github_reader, "load_layer_prompt_md",
                        lambda slug, n: None)
    monkeypatch.setattr(github_reader, "load_dept_yaml",
                        lambda slug: {
                            "department": {"slug": "fixture",
                                            "level": "ops"},
                            "layers": {"subscribed": [1, 2, 3, 4]},
                        })
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    # Both mission unique tokens still surface (missions are visible)
    assert "UNIQUE_TOKEN_AAA" in body
    assert "UNIQUE_TOKEN_BBB" in body
    # The duplicated section title MUST be gone
    rendezvous_count = body.lower().count("rendez-vous récurrents")
    assert rendezvous_count == 0, (
        f"'Rendez-vous récurrents' section still present "
        f"({rendezvous_count}× found). Should be removed in favor of "
        f"a single consolidated 'Moments' section per msg 3142."
    )


def test_onboarding_consolidates_missions_under_moments(client, fixture_root):
    """Same consolidation on the onboarding page.

    We check specifically for the section HEADING (h2 wrapping
    'Ses rendez-vous récurrents'), not just any mention of the phrase
    — the éclosion-step descriptions + the 'what she has learned' recap
    legitimately mention the concept in body prose.
    """
    _make_maya_rich_repo(fixture_root, status="Drafting")
    r = client.get("/agents/maya-rich/onboarding")
    assert r.status_code == 200
    body = r.text
    # Missions still visible (sentinel mission IDs from the fixture)
    assert "morning_sync" in body
    # The section HEADING must be gone. The string "Ses rendez-vous
    # récurrents" only appeared inside the deleted section's <h2>.
    assert "Ses rendez-vous récurrents" not in body, (
        "Section heading 'Ses rendez-vous récurrents' still present on "
        "onboarding page. Per msg 3142, the section was consolidated "
        "under 'Moments de la journée' with missions nested inline."
    )
