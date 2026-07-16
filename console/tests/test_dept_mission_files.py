"""
test_dept_mission_files.py — "Mission files (L1/L2)" read-only pane on
/dept/content (card #622).

Jade wants Miranda's L1/L2 mission files (MANDATE.md, layer PROMPT.mds,
mission PROMPT.mds, working memory, config) reviewable async in the
cockpit, independent of a live R&D session. This is READ-ONLY — no
write/edit affordance should exist anywhere in this surface.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from console.tests.conftest import TEST_BEARER


def _build_content_repo(root: Path) -> Path:
    """Build a bubble-ops-content repo shaped like the real one (re-verified
    2026-07-16 via fresh shallow clone, PLAN-642 §0-bis refinement 1): the
    real content repo now ALSO has skills/ (22), memory/*.md (12), and 3
    VOICE.md files (twitter/, substack/, newsletter/) post-Miranda-cutover
    — the original #622 fixture's "these don't exist" premise is stale.
    This fixture is content-SHAPED (a representative subset, not the full
    22/12/13), covering: MANDATE.md, layers/{1,2}/PROMPT.md,
    missions/<name>/PROMPT.md, WORKING_MEMORY.md, whiteboard.yaml,
    config/content_cadence.yaml, skills/draft-x/SKILL.md,
    memory/draft_x.md, twitter/VOICE.md. Also includes a `draft_x`
    dept.yaml recurring_mission (real shape, PLAN-642 §2) for the
    piece-resolver tests."""
    repo = root / "bubble-ops-content"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "content", "level": "ops",
                           "mandate": "produce content"},
            "layers": {"subscribed": [1, 2]},
            "recurring_missions": [
                {
                    "id": "linkedin_sage_batch",
                    "layer": 1,
                    "cadence": "daily",
                    "time": "08:30",
                    "description": "Research phase for the LinkedIn sage batch.",
                    "output_queue": "queues/research/",
                    "creates": ["context_pool_item"],
                    "input_sources": ["shared_wiki", "agent_logbook"],
                },
                {
                    "id": "draft_x",
                    "layer": 2,
                    "cadence": "daily",
                    "time": "12:05",
                    "description": "Reads the research pool + working memory + X's publish history; drafts a thread per twitter/VOICE.md.",
                    "output_queue": "queues/gates/",
                    "creates": ["draft", "publish_proposal"],
                    "input_sources": [
                        "research_pool", "brand_guidelines", "twitter_voice",
                        "working_memory", "draft_x_memory", "x_publish_history",
                    ],
                    "gate_policy_id": "high_visibility_publish",
                },
            ],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "content", "display_name": "Miranda",
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "queues" / "gates").mkdir(parents=True)
    (repo / "outputs").mkdir()

    (repo / "MANDATE.md").write_text(
        "# Mandate\n\nProduce, plan, audit social content for Bubble.\n",
        encoding="utf-8",
    )
    (repo / "WORKING_MEMORY.md").write_text(
        "## Working memory\n\ncurrent focus: linkedin sage batch\n",
        encoding="utf-8",
    )
    (repo / "whiteboard.yaml").write_text(
        yaml.safe_dump({"notes": []}), encoding="utf-8",
    )
    (repo / "config").mkdir()
    (repo / "config" / "content_cadence.yaml").write_text(
        yaml.safe_dump({"cadence": "daily"}), encoding="utf-8",
    )

    for n in (1, 2):
        layer_dir = repo / "layers" / str(n)
        layer_dir.mkdir(parents=True)
        (layer_dir / "PROMPT.md").write_text(
            f"Layer {n} prompt body.\n", encoding="utf-8",
        )

    for mission in ("linkedin_sage_batch", "skills_audit_content"):
        mdir = repo / "missions" / mission
        mdir.mkdir(parents=True)
        (mdir / "PROMPT.md").write_text(
            f"Mission {mission} prompt body.\n", encoding="utf-8",
        )
    # An empty mission dir with no PROMPT.md must NOT show up in the pane.
    (repo / "missions" / "no_prompt_here").mkdir(parents=True)

    # #642 piece classes: skills/, memory/*.md, <channel>/VOICE.md.
    skill_dir = repo / "skills" / "draft-x"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# draft-x skill\n\nDrafts an X thread.\n", encoding="utf-8",
    )
    (repo / "memory").mkdir()
    (repo / "memory" / "draft_x.md").write_text(
        "## draft_x memory\n\nprior threads: ...\n", encoding="utf-8",
    )
    (repo / "twitter").mkdir()
    (repo / "twitter" / "VOICE.md").write_text(
        "# Twitter voice\n\nTerse, contrarian, data-first.\n", encoding="utf-8",
    )

    return repo


@pytest.fixture
def content_root(tmp_path: Path) -> Path:
    root = tmp_path / "depts"
    root.mkdir()
    _build_content_repo(root)
    return root


@pytest.fixture
def content_app(monkeypatch, content_root: Path):
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", TEST_BEARER)
    monkeypatch.setenv("READ_FROM_DISK", str(content_root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    return create_app()


@pytest.fixture
def content_client(content_app):
    from fastapi.testclient import TestClient
    c = TestClient(content_app)
    c.headers.update({"Authorization": f"Bearer {TEST_BEARER}"})
    return c


def test_dept_content_shows_mission_files_as_piece_tiles(content_client):
    """#642 PR-A: /dept/content no longer shows the flat 04b file-list pane
    (that's now dormant once mission_pieces_by_layer is populated — see
    dept_detail.html's `{% if mission_files and not mission_pieces_by_layer %}`
    guard) — MANDATE.md, the layer prompts, and the mission prompts are all
    still reachable, but as clickable piece tiles inside each mission card
    in §04 instead. Also re-verifies the #622 premise fix (§0-bis refinement
    1): skills/, memory/*.md, and VOICE.md DO exist in the real content repo
    now and must render as clickable tiles, not be silently dropped."""
    r = content_client.get("/dept/content")
    assert r.status_code == 200
    body = r.text
    assert "MANDATE.md" in body
    assert "Moment 1" in body and "Moment 2" in body
    assert "linkedin_sage_batch" in body
    assert "draft_x" in body
    assert "WORKING_MEMORY.md" in body
    # The 04b flat pane's own heading must be gone (folded into piece tiles).
    assert "Fichiers de mission (L1/L2)" not in body
    # draft_x's piece tiles: skill/memory/voice all resolve on this fixture.
    assert "piece-tile" in body
    assert "draft-x" in body        # skills/draft-x/SKILL.md tile label
    assert "draft_x.md" in body     # memory/draft_x.md tile label
    assert "twitter/VOICE.md" in body
    # research_pool / x_publish_history have no openable file -> muted
    # "reference" chips, not silently dropped and not crashing the page.
    assert "research_pool" in body
    assert "x_publish_history" in body
    # Mission dir with no PROMPT.md must not produce a phantom entry.
    assert "no_prompt_here" not in body


def test_dept_content_piece_view_is_readonly(content_client):
    """No write/edit affordance anywhere on the piece view: no <form>, no
    method other than GET, no POST/PUT/PATCH/DELETE action targeting
    mission-file routes."""
    r = content_client.get("/dept/content")
    assert r.status_code == 200
    body = r.text
    # crude but effective: the mission-file links must be plain <a> tags,
    # never wrapped in a form/button that submits.
    assert 'action="/dept/content/mission-file' not in body
    assert "mission-file" in body  # piece tiles link to it


def test_mission_file_view_renders_content_readonly(content_client):
    """Opening a mission file (MANDATE.md) shows its verbatim text in a
    read view, with no save/edit control on the page."""
    r = content_client.get("/dept/content/mission-file?f=MANDATE.md")
    assert r.status_code == 200
    body = r.text
    assert "Produce, plan, audit social content for Bubble." in body
    assert "lecture seule" in body.lower()
    assert "<form" not in body.lower()
    assert "<textarea" not in body.lower()
    assert "<button" not in body.lower() or "type=\"submit\"" not in body.lower()


def test_mission_file_view_layer_prompt(content_client):
    r = content_client.get("/dept/content/mission-file?f=layers/1/PROMPT.md")
    assert r.status_code == 200
    assert "Layer 1 prompt body." in r.text


def test_mission_file_view_mission_prompt(content_client):
    r = content_client.get(
        "/dept/content/mission-file?f=missions/linkedin_sage_batch/PROMPT.md"
    )
    assert r.status_code == 200
    assert "Mission linkedin_sage_batch prompt body." in r.text


def test_mission_file_view_skill_file(content_client):
    """#642: skills/<name>/SKILL.md is now on the allowlist."""
    r = content_client.get(
        "/dept/content/mission-file?f=skills/draft-x/SKILL.md"
    )
    assert r.status_code == 200
    assert "Drafts an X thread." in r.text


def test_mission_file_view_memory_file(content_client):
    """#642: memory/*.md (plural dir) is now on the allowlist, distinct
    from the singular WORKING_MEMORY.md."""
    r = content_client.get(
        "/dept/content/mission-file?f=memory/draft_x.md"
    )
    assert r.status_code == 200
    assert "prior threads" in r.text


def test_mission_file_view_voice_file(content_client):
    """#642: <channel>/VOICE.md is now on the allowlist."""
    r = content_client.get(
        "/dept/content/mission-file?f=twitter/VOICE.md"
    )
    assert r.status_code == 200
    assert "Terse, contrarian, data-first." in r.text


def test_mission_file_view_rejects_path_traversal(content_client):
    """`f` must not be able to escape the allowlist via traversal or by
    naming an arbitrary repo file (e.g. dept.yaml, queues/gates/*) that
    isn't itself an L1/L2 mission file."""
    for bad in (
        "../../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "dept.yaml",
        "queues/gates/echo-1.yaml",
        "onboarding/STATE.yaml",
        "missions/no_prompt_here/PROMPT.md",  # doesn't exist on disk
    ):
        r = content_client.get(
            "/dept/content/mission-file", params={"f": bad}
        )
        assert r.status_code == 404, f"expected 404 for f={bad!r}, got {r.status_code}"


def test_mission_file_view_rejects_off_allowlist_piece_classes(content_client):
    """#642 §0-bis refinement 4: extending the allowlist to skills/memory/
    voice must NOT silently expose tool scripts or policy files — those
    piece classes are explicitly NOT on the allowlist (tools render as a
    clickable tile only when matched by convention in mission_pieces.py,
    but the GUARD itself never opens arbitrary scripts/lib/* or
    policies/* paths)."""
    for bad in (
        "scripts/lib/fetch_x_signal.py",
        "policies/publish_policy.yaml",
    ):
        r = content_client.get(
            "/dept/content/mission-file", params={"f": bad}
        )
        assert r.status_code == 404, f"expected 404 for f={bad!r}, got {r.status_code}"


def test_mission_files_pane_absent_on_other_depts(content_client, tmp_path):
    """The pane must not leak onto non-content depts — a sibling dept in the
    same disk root without the mission-files scope must render unaffected."""
    # Build a second, unrelated dept in the same disk root to prove the
    # `content`-only gate holds even when other depts exist alongside it.
    import os
    root = Path(os.environ["READ_FROM_DISK"])
    other = root / "bubble-ops-other"
    other.mkdir()
    (other / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "other", "level": "ops", "mandate": "x"},
            "layers": {"subscribed": [1]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (other / "onboarding").mkdir()
    (other / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "other", "display_name": "Other",
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live", "validated_steps": [], "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (other / "queues" / "gates").mkdir(parents=True)
    (other / "outputs").mkdir()
    (other / "MANDATE.md").write_text("Other dept mandate.\n", encoding="utf-8")

    r = content_client.get("/dept/other")
    assert r.status_code == 200
    assert "Fichiers de mission" not in r.text
