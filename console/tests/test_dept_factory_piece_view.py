"""
test_dept_factory_piece_view.py — card #688 (DEPT FACTORY emits #642
conventions natively).

End-to-end evaluation bar per the card: scaffold a fixture dept via the
REAL factory entry point (scripts/lib/scaffold.py::scaffold(), the same
function bootstrap-dept.sh calls) with a starter mission, then render its
cockpit page through the REAL console route/template pipeline (not a
hand-built fixture dict) and assert the FULL #642 architecture view
(groups, bands, tiles) renders — with ZERO manual file additions beyond
what `scaffold()` itself wrote.

This deliberately does NOT hand-write missions/<id>/PROMPT.md,
config/*.yaml, skills/*/SKILL.md, docs/CONTEXT_POOL_SCHEMA.md the way
test_dept_piece_view_fleet.py's `fleet_root` fixture does — that would
only prove the CONSUMER (mission_pieces.py) can render a well-shaped
dept, which PR#272 already covers. This file proves the PRODUCER
(scaffold.py) emits that shape by itself.

`test_without_mission_scaffold_pieces_view_degrades` is the required
"goes RED without the change" control: it scaffolds the same fixture but
skips the mission_scaffold.py emission step (monkeypatched out), proving
the piece view genuinely depends on what card #688 added — a dept that
only got the pre-#688 bare missions/<id>.yaml renders the leanest
degradation (reference chips only, no config/own-skill/memory tiles),
not the full view.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from console.tests.conftest import TEST_BEARER

# ---------------------------------------------------------------------------
# Import the REAL factory (scripts/lib/scaffold.py + mission_scaffold.py) —
# same path-surgery pattern scaffold.py's own tests use
# (scripts/lib/tests/test_scaffold_management.py).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent          # console/tests/
_CONSOLE_ROOT = _HERE.parent                       # console/
_PROJECT_ROOT = _CONSOLE_ROOT.parent               # bubble-ops-loop/
_SCRIPTS_LIB = _PROJECT_ROOT / "scripts" / "lib"
_SKILL_ROOT = _PROJECT_ROOT / "skills" / "department-onboarding-guide"

for _p in (str(_SKILL_ROOT), str(_SCRIPTS_LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scaffold  # noqa: E402
import mission_scaffold  # noqa: E402


# A single starter mission that declares one input_sources key per piece
# class (skill/config/memory/voice), so a successful scaffold should
# produce every tile the #642 view knows how to render, per
# console/services/mission_pieces.py's own doctrine — the same shape
# test_dept_piece_view_fleet.py's content-shaped fixture uses, but
# produced by the FACTORY, not hand-written.
_STARTER_MISSION = {
    "id": "draft_x",
    "layer": 2,
    "cadence": "daily",
    "time": "09:00",
    "description": "Reads the pool and drafts an X thread using the brand voice.",
    "output_queue": "queues/gates/",
    "creates": ["draft", "publish_proposal"],
    "input_sources": ["twitter_voice", "draft_x_memory"],
}

# A companion L1 mission so the pool band (between Moment 1 and Moment 2)
# has something to resolve — resolve_layer_band reads L1's declared
# output_queue, and the pool-band's schema deep-link only appears when
# docs/CONTEXT_POOL_SCHEMA.md exists (scaffold_pool_schema, card #688).
_L1_MISSION = {
    "id": "gather_x_timeline",
    "layer": 1,
    "cadence": "daily",
    "time": "07:00",
    "description": "Gathers the X timeline into the research pool.",
    "output_queue": "queues/research/",
    "creates": ["context_pool_item"],
    "input_sources": ["x_timeline"],
}


def _scaffold_fixture_dept(root: Path, *, with_pieces: bool = True) -> Path:
    """Scaffold bubble-ops-x99 via the REAL factory entry point.

    `with_pieces=False` monkeypatches out mission_scaffold's emission
    step (but keeps scaffold() otherwise identical) — this is the "RED
    without the change" control.
    """
    dept_root = root / "bubble-ops-x99"
    dept_root.mkdir(parents=True)
    scaffold.scaffold(
        root=dept_root,
        slug="x99",
        display_name="X99",
        owner="operator",
        level="ops",
        starter_missions=[_L1_MISSION, _STARTER_MISSION],
    )
    # Flip status to Live so dept_registry treats this as a live dept
    # (scaffold() always initializes status=Idea — the onboarding start
    # state — same as every fresh eclosion; the fleet fixture in
    # test_dept_piece_view_fleet.py does the equivalent by constructing
    # STATE.yaml with status="Live" directly).
    state_path = dept_root / "onboarding" / "STATE.yaml"
    state_doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    state_doc["status"] = "Live"
    state_doc["validated_steps"] = ["mandate", "missions", "layers"]
    state_path.write_text(yaml.safe_dump(state_doc, sort_keys=False), encoding="utf-8")
    return dept_root


@pytest.fixture
def factory_root(tmp_path: Path) -> Path:
    root = tmp_path / "depts"
    root.mkdir()
    _scaffold_fixture_dept(root, with_pieces=True)
    return root


@pytest.fixture
def factory_root_no_pieces(tmp_path: Path, monkeypatch) -> Path:
    """Same fixture dept, but with mission_scaffold's piece emission
    short-circuited to a no-op — reproduces the PRE-#688 factory output
    (bare missions/<id>.yaml only, no PROMPT.md/config/skill/memory/docs)."""
    monkeypatch.setattr(mission_scaffold, "scaffold_mission_pieces", lambda *a, **k: [])
    monkeypatch.setattr(mission_scaffold, "scaffold_pool_schema", lambda *a, **k: None)
    root = tmp_path / "depts"
    root.mkdir()
    _scaffold_fixture_dept(root, with_pieces=False)
    return root


def _client_for(monkeypatch, root: Path):
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", TEST_BEARER)
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": f"Bearer {TEST_BEARER}"})
    return c


@pytest.fixture
def factory_client(monkeypatch, factory_root: Path):
    return _client_for(monkeypatch, factory_root)


@pytest.fixture
def factory_client_no_pieces(monkeypatch, factory_root_no_pieces: Path):
    return _client_for(monkeypatch, factory_root_no_pieces)


# ---------------------------------------------------------------------------
# 1. Filesystem-level proof: scaffold() alone (no console involved) wrote
#    every #642-convention file this mission's shape implies.
# ---------------------------------------------------------------------------


def test_scaffold_writes_642_convention_files_on_disk(tmp_path):
    root = tmp_path / "depts"
    root.mkdir()
    dept_root = _scaffold_fixture_dept(root)

    assert (dept_root / "missions" / "draft_x" / "PROMPT.md").is_file()
    assert (dept_root / "missions" / "gather_x_timeline" / "PROMPT.md").is_file()
    assert (dept_root / "skills" / "draft-x" / "SKILL.md").is_file()
    assert (dept_root / "config" / "x.yaml").is_file()  # draft_x -> config/x.yaml
    assert (dept_root / "memory" / "draft_x.md").is_file()
    assert (dept_root / "twitter" / "VOICE.md").is_file()
    assert (dept_root / "docs" / "CONTEXT_POOL_SCHEMA.md").is_file()
    # scaffold()'s --starter-missions path folds the mission straight into
    # dept.yaml.draft::recurring_missions (inline) rather than also
    # writing a redundant flat missions/<id>.yaml — list_missions_full()
    # merges both sources, inline included, so this is not a gap. (The
    # conversational Step-2 "+ add mission" path, MissionsRunner, DOES
    # additionally write the flat yaml — see test_step2_missions_runner.py.)
    draft = yaml.safe_load((dept_root / "dept.yaml.draft").read_text(encoding="utf-8"))
    mission_ids = {m["id"] for m in draft["recurring_missions"]}
    assert {"draft_x", "gather_x_timeline", "daily_risk_audit"} <= mission_ids


# ---------------------------------------------------------------------------
# 2. THE evaluation bar: render the scaffolded fixture through the real
#    console route and assert the FULL architecture view (groups, bands,
#    tiles) — zero manual additions beyond what scaffold() wrote.
# ---------------------------------------------------------------------------


def test_freshly_scaffolded_dept_renders_full_architecture_view(factory_client):
    r = factory_client.get("/dept/x99")
    assert r.status_code == 200
    body = r.text

    # Groups (entrées / cœur / sortie) — PR#272 item 2.
    assert "entrées" in body
    assert "cœur" in body
    assert "sortie" in body

    # Bands — pool (L1 -> L2) + gate (draft_x writes queues/gates/).
    assert "pool-band" in body
    assert "gate-band" in body
    assert "LE POOL" in body
    assert "Gate humaine" in body
    # Pool schema deep-link resolves because scaffold_pool_schema wrote
    # docs/CONTEXT_POOL_SCHEMA.md.
    assert "CONTEXT_POOL_SCHEMA" in body

    # Tiles: every piece class the starter mission's shape implies.
    assert "piece-tile" in body or "piece-groups" in body
    assert "mission-file?f=missions%2Fdraft_x%2FPROMPT.md" in body or "missions/draft_x/PROMPT.md" in body
    assert "skills/draft-x/SKILL.md" in body
    assert "config/x.yaml" in body
    assert "memory/draft_x.md" in body
    assert "twitter/VOICE.md" in body

    # Sanity: no unresolved-key Jinja crash artifacts, no cross-dept bleed.
    assert "Undefined" not in body


def test_freshly_scaffolded_dept_factory_tests_pass_end_to_end(factory_client):
    """Belt-and-suspenders HTTP smoke matching test_dept_piece_view_fleet.py's
    own bar for a fleet dept — a factory-scaffolded dept must clear the SAME
    200-render bar the fleet fixtures do."""
    r = factory_client.get("/dept/x99")
    assert r.status_code == 200
    assert "draft_x" in r.text
    assert "gather_x_timeline" in r.text


# ---------------------------------------------------------------------------
# 3. RED-without-the-change control: same fixture, mission_scaffold's
#    emission short-circuited -> the view must NOT render the full
#    architecture (proves the view genuinely depends on card #688's
#    factory change, not just on scaffold()'s pre-existing skeleton).
# ---------------------------------------------------------------------------


def test_without_mission_scaffold_pieces_view_degrades(factory_client_no_pieces):
    r = factory_client_no_pieces.get("/dept/x99")
    assert r.status_code == 200  # still must not crash — graceful degradation
    body = r.text

    # The groups/entrées-cœur-sortie scaffolding is a template-level
    # concern (unconditional once missions_by_layer is non-empty), so it
    # still renders — but the RICH tiles (config/skill/memory/voice/pool
    # schema) that only exist because mission_scaffold wrote files must
    # be ABSENT.
    assert "config/x.yaml" not in body
    assert "memory/draft_x.md" not in body
    assert "twitter/VOICE.md" not in body
    assert "skills/draft-x/SKILL.md" not in body
    assert "CONTEXT_POOL_SCHEMA" not in body
    # The mission core itself also degrades: with piece scaffolding
    # disabled, the starter mission exists ONLY inline in
    # dept.yaml.draft::recurring_missions — no missions/draft_x/PROMPT.md
    # and no missions/draft_x.yaml on disk at all — so the core tile is
    # non-clickable (mission_pieces._mission_core_piece's fully-degraded
    # branch), not just demoted to the flat-yaml fallback.
    assert "missions/draft_x/PROMPT.md" not in body
    assert "missions/draft_x.yaml" not in body
    # The mission still SHOWS UP (id + description render from the inline
    # dept.yaml.draft dict) — it's the piece tiles that are missing, not
    # the mission itself. This is the "leanest degradation, not a crash"
    # bar PLAN-642 §5 sets for a lean dept.
    assert "draft_x" in body
