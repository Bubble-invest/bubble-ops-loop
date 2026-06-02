"""
test_loop_dispatch_layer1.py — TDD tests for STEP C.0 (Layer 1 cadence-
materialization rule).

Context (Joris msg 3129, 2026-05-24):
  Layer 1 is currently absent from STEP C of CLAUDE_MD_OPERATING_TEMPLATE.
  The dispatch tree only handles:
    C.1 — Layer 4 in 22:00–22:30 UTC window
    C.2 — Layer 2 if queues/research/ has items
    C.3 — Layer 3 if inbox/decisions/ has items
    C.4 — heartbeat-only
  This file drives the NEW STEP C.0 rule:
    C.0 — Layer 1 (morning / data refresh subagent) fires when ALL other
          layer triggers are FALSE (idle gate) AND each of L2/L3/L4 have
          completed ≥ N rounds since L1's last fire (default N=1, configurable
          via dept.yaml::layer_1.fire_after_rounds).
          When fired, Layer 1 reads dept.yaml::recurring_missions, and for
          each mission whose cadence is due, materializes a queue item into
          mission.output_queue with kind from mission.creates[].

What this file CAN test (per the brief, constraint #5):
  - Template text contains the new STEP C.0 rule
  - Helper functions are unit-tested
  - Schema accepts the new layer_1.fire_after_rounds field
  - The existing daily_risk_audit (Layer 4) mission coexists cleanly

What this file CANNOT test:
  - The dispatch reasoning itself (it lives in the agent's natural-language
    interpretation of STEP C in CLAUDE.md). We test the artifacts and the
    helpers, not the LLM's decision.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path surgery (matches sibling test files).
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent
SCRIPTS_DIR = SCRIPTS_LIB.parent
PROJECT_ROOT = SCRIPTS_DIR.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
for p in (str(SKILL_ROOT), str(SCRIPTS_LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

import scaffold  # noqa: E402

# Helper module under test (created in this same step).
try:
    import dispatch_helpers  # noqa: E402
except ImportError:
    dispatch_helpers = None  # surfaces as a clean failure in the tests below


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_outputs_root(tmp_path: Path) -> Path:
    """A fresh outputs/ tree rooted at tmp_path/outputs."""
    p = tmp_path / "outputs"
    p.mkdir()
    return p


def _today_dir(outputs_root: Path, d: date | None = None) -> Path:
    """Return outputs/<today> as a freshly-created Path."""
    d = d or date.today()
    sub = outputs_root / d.isoformat()
    sub.mkdir(parents=True, exist_ok=True)
    return sub


# ============================================================================
# SECTION 1 — Helper unit tests (dispatch_helpers.py)
# ============================================================================

def test_dispatch_helpers_module_exists():
    """The dispatch_helpers module is the small home for the new helpers."""
    assert dispatch_helpers is not None, (
        "scripts/lib/dispatch_helpers.py must exist and be importable. "
        "It holds round-counter file I/O, .last-run readers, and the "
        "cadence-due check used by STEP C.0 reasoning."
    )


# ---------- .last-run -------------------------------------------------------

def test_read_last_run_returns_none_when_file_absent(tmp_outputs_root):
    today = _today_dir(tmp_outputs_root)
    layer_dir = today / "1"
    layer_dir.mkdir()
    out = dispatch_helpers.read_last_run(layer_dir)
    assert out is None


def test_read_last_run_parses_iso_timestamp(tmp_outputs_root):
    today = _today_dir(tmp_outputs_root)
    layer_dir = today / "1"
    layer_dir.mkdir()
    iso = "2026-05-24T07:01:23+00:00"
    (layer_dir / ".last-run").write_text(iso + "\n", encoding="utf-8")
    out = dispatch_helpers.read_last_run(layer_dir)
    assert isinstance(out, datetime)
    assert out.tzinfo is not None
    # Round-trip the ISO string.
    assert out.isoformat() == iso


def test_write_last_run_creates_iso_timestamp(tmp_outputs_root):
    today = _today_dir(tmp_outputs_root)
    layer_dir = today / "1"
    layer_dir.mkdir()
    dispatch_helpers.write_last_run(layer_dir, datetime(2026, 5, 24, 7, 1, 23,
                                                       tzinfo=timezone.utc))
    body = (layer_dir / ".last-run").read_text(encoding="utf-8").strip()
    assert body == "2026-05-24T07:01:23+00:00"


# ---------- round counter ---------------------------------------------------

def test_round_counter_starts_empty_when_file_absent(tmp_outputs_root):
    today = _today_dir(tmp_outputs_root)
    counts = dispatch_helpers.read_round_counter(today)
    # Convention: missing layers return 0.
    for n in (1, 2, 3, 4):
        assert counts.get(str(n), 0) == 0


def test_round_counter_increment_persists_to_disk(tmp_outputs_root):
    today = _today_dir(tmp_outputs_root)
    dispatch_helpers.increment_round_counter(today, layer=2)
    dispatch_helpers.increment_round_counter(today, layer=2)
    dispatch_helpers.increment_round_counter(today, layer=3)
    counts = dispatch_helpers.read_round_counter(today)
    assert counts["2"] == 2
    assert counts["3"] == 1
    assert counts.get("1", 0) == 0
    # File on disk has correct JSON shape.
    raw = json.loads((today / "round_counter.json").read_text())
    assert raw["2"] == 2
    assert raw["3"] == 1


def test_round_counter_resets_at_midnight(tmp_outputs_root):
    """The counter is rooted at outputs/<today>/. A new day = a new dir = a
    fresh counter (no carry-over). This is the 'resets at midnight'
    semantics."""
    y_dir = _today_dir(tmp_outputs_root, d=date(2026, 5, 23))
    t_dir = _today_dir(tmp_outputs_root, d=date(2026, 5, 24))
    dispatch_helpers.increment_round_counter(y_dir, layer=2)
    dispatch_helpers.increment_round_counter(y_dir, layer=2)
    # Today starts at zero.
    assert dispatch_helpers.read_round_counter(t_dir).get("2", 0) == 0
    # Yesterday is untouched.
    assert dispatch_helpers.read_round_counter(y_dir)["2"] == 2


def test_round_counter_increments_on_layer_completion(tmp_outputs_root):
    """Round counter records 'completed rounds' — incremented as the LAST
    action of a layer's dispatch. Test the contract: call increment,
    re-read, observe +1."""
    today = _today_dir(tmp_outputs_root)
    before = dispatch_helpers.read_round_counter(today).get("2", 0)
    dispatch_helpers.increment_round_counter(today, layer=2)
    after = dispatch_helpers.read_round_counter(today).get("2", 0)
    assert after == before + 1


# ---------- L1 gate computation ---------------------------------------------

def test_layer_1_gate_satisfied_when_other_layers_have_n_rounds(tmp_outputs_root):
    """L1 is gated on L2 AND L3 AND L4 having completed ≥ N rounds since
    L1's last fire. With N=1 and L1 never having fired, all 3 just need ≥1."""
    today = _today_dir(tmp_outputs_root)
    # L2, L3, L4 each completed 1 round.
    for layer in (2, 3, 4):
        dispatch_helpers.increment_round_counter(today, layer=layer)
    gated = dispatch_helpers.layer_1_gate_satisfied(today, fire_after_rounds=1)
    assert gated is True


def test_layer_1_gate_not_satisfied_when_other_layers_below_threshold(tmp_outputs_root):
    today = _today_dir(tmp_outputs_root)
    # Only L2 has run.
    dispatch_helpers.increment_round_counter(today, layer=2)
    gated = dispatch_helpers.layer_1_gate_satisfied(today, fire_after_rounds=1)
    # L3 and L4 are still at zero — gate not satisfied.
    assert gated is False


def test_layer_1_gate_respects_configurable_threshold(tmp_outputs_root):
    today = _today_dir(tmp_outputs_root)
    for layer in (2, 3, 4):
        dispatch_helpers.increment_round_counter(today, layer=layer)
        dispatch_helpers.increment_round_counter(today, layer=layer)
    # 2 rounds each — threshold of 3 not satisfied.
    assert dispatch_helpers.layer_1_gate_satisfied(today, fire_after_rounds=3) is False
    # Threshold of 2 satisfied.
    assert dispatch_helpers.layer_1_gate_satisfied(today, fire_after_rounds=2) is True


# ---------- cadence-due check -----------------------------------------------

def test_cadence_due_daily_before_time(tmp_outputs_root):
    """A mission with cadence=daily, time=07:00 is NOT due at 06:30 Paris."""
    mission = {"id": "m", "cadence": "daily", "time": "07:00"}
    # 06:30 Paris on 2026-05-24 (CEST = +02:00, so 04:30 UTC).
    now_utc = datetime(2026, 5, 24, 4, 30, tzinfo=timezone.utc)
    assert dispatch_helpers.is_mission_due(mission, now=now_utc, last_fired=None) is False


def test_cadence_due_daily_after_time_never_fired(tmp_outputs_root):
    """Daily 07:00 mission at 08:00 Paris = due (first time today)."""
    mission = {"id": "m", "cadence": "daily", "time": "07:00"}
    # 08:00 Paris on 2026-05-24 = 06:00 UTC.
    now_utc = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)
    assert dispatch_helpers.is_mission_due(mission, now=now_utc, last_fired=None) is True


def test_cadence_due_daily_after_time_already_fired_today(tmp_outputs_root):
    """Daily 07:00 mission that already fired at 07:15 = NOT due again at 09:00."""
    mission = {"id": "m", "cadence": "daily", "time": "07:00"}
    last_fired = datetime(2026, 5, 24, 5, 15, tzinfo=timezone.utc)  # 07:15 Paris
    now_utc = datetime(2026, 5, 24, 7, 0, tzinfo=timezone.utc)      # 09:00 Paris
    assert dispatch_helpers.is_mission_due(mission, now=now_utc, last_fired=last_fired) is False


def test_cadence_due_daily_yesterdays_fire_re_fires_today(tmp_outputs_root):
    """A mission fired yesterday IS due today (idempotence is per-UTC-day)."""
    mission = {"id": "m", "cadence": "daily", "time": "07:00"}
    last_fired = datetime(2026, 5, 23, 5, 15, tzinfo=timezone.utc)  # yesterday
    now_utc = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)      # today 08:00 Paris
    assert dispatch_helpers.is_mission_due(mission, now=now_utc, last_fired=last_fired) is True


def test_cadence_due_weekly_wrong_day(tmp_outputs_root):
    """Weekly monday mission is NOT due on a sunday."""
    mission = {"id": "m", "cadence": "weekly", "time": "07:00", "day": "monday"}
    # 2026-05-24 is a Sunday.
    sunday_now = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)
    assert dispatch_helpers.is_mission_due(mission, now=sunday_now, last_fired=None) is False


def test_cadence_due_weekly_right_day_after_time(tmp_outputs_root):
    """Weekly monday 07:00 mission IS due on monday at 08:00 Paris."""
    mission = {"id": "m", "cadence": "weekly", "time": "07:00", "day": "monday"}
    # 2026-05-25 is a Monday.
    monday_now = datetime(2026, 5, 25, 6, 0, tzinfo=timezone.utc)
    assert dispatch_helpers.is_mission_due(mission, now=monday_now, last_fired=None) is True


# ============================================================================
# SECTION 2 — Template text (STEP C.0) — operating CLAUDE.md
# ============================================================================
#
# These tests assert the TEMPLATE text contains the new STEP C.0 rule.
# We don't test the agent's reasoning — we test the prompt artifact that
# guides it.
# ============================================================================

def _ops_dept_yaml(layers=(1, 2, 3, 4)) -> dict:
    return {
        "department": {
            "slug": "maya",
            "display_name": "Maya",
            "mandate": "Sourcer, qualifier, amener à maturité commerciale les prospects LinkedIn.",
            "level": "ops",
        },
        "layers": {"subscribed": list(layers)},
        "gate_policies": {},
    }


def test_template_contains_step_c0_marker():
    """Post msg-3160 refactor: STEP C.0..C.4 enumeration moved out of
    CLAUDE.md prose into dispatch_helpers.decide_dispatch(). The
    template no longer contains 'C.0' as a literal — instead it says
    'call decide_dispatch which encodes the full priority tree'.

    The Layer-1 idle gate IS still encoded (in dispatch_helpers +
    layer_1_gate_satisfied helper); the BEHAVIOR is preserved (see
    test_template_dispatch_behavior_still_complete below). The
    Layer-1 dispatch outcome 'layer_1' is reachable per
    test_layer_1_fires_when_all_other_layers_idle_and_rounds_satisfied.
    """
    tpl = scaffold.CLAUDE_MD_OPERATING_TEMPLATE
    # The helper itself must be referenced — the prose delegates here.
    assert "decide_dispatch" in tpl, (
        "operating CLAUDE.md must reference decide_dispatch — the "
        "deterministic helper that replaces STEP C.0..C.4 prose."
    )
    # And Layer-1 must STILL appear in the template (somewhere — likely
    # in the per-layer Moments listing).
    assert "Layer 1" in tpl or "layer_1" in tpl, (
        "Layer 1 must still be named in the operating CLAUDE.md"
    )


def test_template_step_c0_mentions_layer_1_and_idle_gate():
    """STEP C.0 must explicitly describe the idle gate (C.1/C.2/C.3 false)
    AND the round-counter gate."""
    tpl = scaffold.CLAUDE_MD_OPERATING_TEMPLATE
    # Render with a concrete dept to ensure no Jinja-style braces leak.
    out = scaffold.render_claude_md_operating(_ops_dept_yaml())
    # Layer 1 is named.
    assert "Layer 1" in out
    # Idle gate / round-counter language must be present (we accept any of
    # a small set of canonical phrasings — the brief said "all 3 other
    # layers have completed 1 (or configurable) round").
    body = out.lower()
    gate_markers = [
        "idle", "round_counter", "round counter", "tour", "rounds",
        "fire_after_rounds",
    ]
    assert any(m in body for m in gate_markers), (
        f"STEP C.0 must mention the round-counter idle gate. Looked "
        f"for {gate_markers} and found none."
    )


def test_template_step_c0_mentions_recurring_missions_and_cadence():
    """Post msg-3160 refactor: the recurring_missions/cadence-handling
    detail moved out of CLAUDE.md prose into layers/1/PROMPT.md (which
    main loads + passes to the L1 subagent via Agent tool). The
    CLAUDE.md operating template only references the helper module —
    it does NOT need to mention 'recurring_missions' or 'cadence' in
    prose anymore. Behavioral coverage is in test_layer_1_*
    integration tests + the materialize_due_missions helper tests."""
    out = scaffold.render_claude_md_operating(_ops_dept_yaml())
    # The helper module that contains is_mission_due +
    # materialize_due_missions must be referenced.
    assert "dispatch_helpers" in out, (
        "operating CLAUDE.md must reference dispatch_helpers.py so the "
        "agent knows where is_mission_due / materialize_due_missions "
        "live (called by L1's PROMPT.md)"
    )
    # The agent must be told to read layers/<N>/PROMPT.md.
    assert "layers/" in out and "PROMPT.md" in out, (
        "operating CLAUDE.md must instruct main to read layers/<N>/"
        "PROMPT.md for per-layer instructions"
    )


def test_template_step_c0_writes_last_run_first_action():
    """The 'write .last-run as first action' idempotence convention is
    universal for all 4 layers. Post msg-3160 refactor, this convention
    is enforced in each `layers/<N>/PROMPT.md` (not in the operating
    CLAUDE.md prose). The CLAUDE.md just references the convention +
    points at the helpers; PROMPT.md files own the per-layer wiring."""
    out = scaffold.render_claude_md_operating(_ops_dept_yaml())
    body = out
    # The CLAUDE.md must STILL describe the .last-run convention at a
    # high level (so the agent knows it exists), but the per-layer
    # specifics live in PROMPT.md.
    assert ".last-run" in body or "last_run" in body, (
        "operating CLAUDE.md must reference the .last-run idempotence "
        "convention. Per-layer specifics live in layers/<N>/PROMPT.md."
    )


def test_template_dispatch_helper_referenced():
    """Post msg-3160 refactor: the operating CLAUDE.md delegates the
    full dispatch tree (formerly STEP C.0..C.4) to the deterministic
    helper `decide_dispatch()` in scripts/lib/dispatch_helpers.py.
    The prose must NAME the helper so the agent knows to call it."""
    out = scaffold.render_claude_md_operating(_ops_dept_yaml())
    assert "decide_dispatch" in out, (
        "operating CLAUDE.md must reference decide_dispatch() — the "
        "deterministic helper that replaces the long STEP C.0..C.4 "
        "prose tree (msg 3160)."
    )


def test_template_dispatch_behavior_still_complete():
    """The dispatch BEHAVIOR (L1 idle gate / L4 22h window / L2 research
    queue / L3 inbox decisions / heartbeat fallback) must still be
    exhaustively encoded — just in dispatch_helpers.decide_dispatch()
    rather than in the CLAUDE.md prose. Verify all 5 outcomes are
    reachable via the helper's contract."""
    import dispatch_helpers as dh
    from datetime import datetime, timezone

    _ran = datetime(2026, 5, 25, 5, 0, tzinfo=timezone.utc)

    def _ctx(**overrides):
        base = {
            "now_utc": datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc),
            "has_research_items": False,
            "has_inbox_decisions": False,
            "layer_4_last_run_today": None,
            # Canonical default for these BEHAVIOR cases: L1 has already run
            # today, so the daily floor is satisfied and we exercise the
            # cycle-gate / priority branches (not the floor).
            "layer_1_last_run_today": _ran,
            "round_counter": {},
            "layer_1_baseline_counter": {},
            "fire_after_rounds": 1,
        }
        base.update(overrides)
        return base

    # daily floor — L1 has NOT run today → fires L1 even with empty queues
    assert dh.decide_dispatch(_ctx(layer_1_last_run_today=None)) == "layer_1"
    # L4 window (22:00-22:30 UTC, no .last-run today yet)
    assert dh.decide_dispatch(_ctx(
        now_utc=datetime(2026, 5, 25, 22, 5, tzinfo=timezone.utc),
    )) == "layer_4"
    # L2 — research queue has items (beats C.0)
    assert dh.decide_dispatch(_ctx(has_research_items=True)) == "layer_2"
    # L3 — inbox decisions
    assert dh.decide_dispatch(_ctx(has_inbox_decisions=True)) == "layer_3"
    # L1 — already ran today, but a full cycle (L2/L3/L4 each advanced past the
    # baseline) completed → re-fire.
    assert dh.decide_dispatch(_ctx(
        layer_4_last_run_today=datetime(2026, 5, 25, 22, 5,
                                          tzinfo=timezone.utc),
        round_counter={"2": 1, "3": 1, "4": 1},
        layer_1_baseline_counter={},
    )) == "layer_1"
    # heartbeat — already ran today, no fresh cycle (counters at 0)
    assert dh.decide_dispatch(_ctx(
        layer_4_last_run_today=datetime(2026, 5, 25, 22, 5,
                                          tzinfo=timezone.utc),
    )) == "heartbeat"


# ============================================================================
# SECTION 3 — Dispatch decision tree (semantic combinations)
#
# These exercise a small pure-function helper `decide_dispatch()` that
# captures the C.0/C.1/C.2/C.3/C.4 priority order. It is NOT the LLM's
# reasoning — it's a deterministic helper the agent can call via a tool
# or that the test suite uses to lock down the contract that the prompt
# must describe.
# ============================================================================

_L1_RAN = datetime(2026, 5, 24, 5, 0, tzinfo=timezone.utc)


def _ctx(now_utc, has_research=False, has_decisions=False,
         l4_last_run=None, rounds=None, fire_after_rounds=1,
         l1_last_run=_L1_RAN, l1_baseline=None):
    return {
        "now_utc": now_utc,
        "has_research_items": has_research,
        "has_inbox_decisions": has_decisions,
        "layer_4_last_run_today": l4_last_run,
        # Default: L1 already ran today so these cases exercise the cycle gate
        # (the daily floor is covered separately).
        "layer_1_last_run_today": l1_last_run,
        "round_counter": rounds or {},
        "layer_1_baseline_counter": l1_baseline or {},
        "fire_after_rounds": fire_after_rounds,
    }


def test_layer_1_fires_when_all_other_layers_idle_and_rounds_satisfied():
    """The canonical C.0 win condition: L1 already ran today, no research, no
    decisions, outside L4 window, and L2/L3/L4 have each done ≥ N rounds since
    L1's baseline → a fresh cycle re-fires L1."""
    now = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)  # 08:00 Paris, NOT L4 window
    ctx = _ctx(now, has_research=False, has_decisions=False,
               l4_last_run=None,
               rounds={"2": 1, "3": 1, "4": 1},
               fire_after_rounds=1)
    decision = dispatch_helpers.decide_dispatch(ctx)
    assert decision == "layer_1"


def test_layer_1_daily_floor_fires_when_not_run_today():
    """Canonical daily floor: L1 has NOT run today → fires even with empty
    queues and zero rounds (Joris 2026-06-01: at least once per day)."""
    now = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)
    ctx = _ctx(now, l1_last_run=None, rounds={})
    assert dispatch_helpers.decide_dispatch(ctx) == "layer_1"


def test_layer_1_does_not_fire_when_research_queue_has_items():
    """C.2 wins over C.0."""
    now = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)
    ctx = _ctx(now, has_research=True, has_decisions=False,
               rounds={"2": 1, "3": 1, "4": 1})
    decision = dispatch_helpers.decide_dispatch(ctx)
    assert decision == "layer_2"


def test_layer_1_does_not_fire_when_inbox_decisions_has_items():
    """C.3 wins over C.0."""
    now = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)
    ctx = _ctx(now, has_research=False, has_decisions=True,
               rounds={"2": 1, "3": 1, "4": 1})
    decision = dispatch_helpers.decide_dispatch(ctx)
    assert decision == "layer_3"


def test_layer_1_does_not_fire_in_l4_window():
    """C.1 wins over C.0."""
    # 22:10 UTC on 2026-05-24 — inside the L4 window AND L4 hasn't run yet.
    now = datetime(2026, 5, 24, 22, 10, tzinfo=timezone.utc)
    ctx = _ctx(now, has_research=False, has_decisions=False,
               l4_last_run=None,
               rounds={"2": 1, "3": 1, "4": 1})
    decision = dispatch_helpers.decide_dispatch(ctx)
    assert decision == "layer_4"


def test_layer_1_does_not_fire_when_rounds_not_satisfied():
    """Once L1 has run today (daily floor satisfied), it won't re-fire until the
    other layers complete a fresh cycle — the gate Joris described. Here L1 ran
    today and no other layer advanced → heartbeat (NOT another layer_1)."""
    now = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)
    ctx = _ctx(now, has_research=False, has_decisions=False,
               l4_last_run=None,
               l1_last_run=_L1_RAN,
               rounds={"2": 0, "3": 0, "4": 0},
               fire_after_rounds=1)
    decision = dispatch_helpers.decide_dispatch(ctx)
    assert decision == "heartbeat"


# ============================================================================
# SECTION 4 — Materialization helper
# ============================================================================

def test_layer_1_materializes_daily_mission_when_time_is_past():
    """Given a daily 07:00 mission, when called past 07:00 with no prior
    fire today, the helper returns a list of queue items to materialize."""
    mission = {
        "id": "discovery_feed_scan",
        "layer": 1,
        "cadence": "daily",
        "time": "07:00",
        "description": "Scan LinkedIn discovery feed each morning.",
        "output_queue": "queues/research/",
        "creates": ["prospect_research"],
    }
    now_utc = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)  # 08:00 Paris
    items = dispatch_helpers.materialize_due_missions(
        [mission], now=now_utc, last_fired_per_mission={}
    )
    assert len(items) == 1
    item = items[0]
    assert item["mission_id"] == "discovery_feed_scan"
    assert item["output_queue"] == "queues/research/"
    assert item["kind"] == "prospect_research"


def test_layer_1_skips_mission_if_already_fired_today():
    """Idempotence via .last-run: a mission already fired today is skipped."""
    mission = {
        "id": "discovery_feed_scan",
        "layer": 1,
        "cadence": "daily",
        "time": "07:00",
        "description": "Scan LinkedIn discovery feed each morning.",
        "output_queue": "queues/research/",
        "creates": ["prospect_research"],
    }
    now_utc = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)
    # Already fired today at 07:15 Paris.
    last_fired = {"discovery_feed_scan": datetime(2026, 5, 24, 5, 15, tzinfo=timezone.utc)}
    items = dispatch_helpers.materialize_due_missions(
        [mission], now=now_utc, last_fired_per_mission=last_fired
    )
    assert items == []


def test_layer_1_skips_layer_4_missions_even_if_due():
    """STEP C.0 is for Layer 1. Layer-4 missions (like daily_risk_audit)
    must NOT be materialized by L1 — C.1 owns them."""
    layer_4_mission = {
        "id": "daily_risk_audit",
        "layer": 4,
        "cadence": "daily",
        "time": "22:00",
        "description": "Daily Layer-4 self-audit.",
        "output_queue": "queues/gates/",
        "creates": ["risk_audit"],
    }
    now_utc = datetime(2026, 5, 24, 22, 30, tzinfo=timezone.utc)
    items = dispatch_helpers.materialize_due_missions(
        [layer_4_mission], now=now_utc, last_fired_per_mission={}
    )
    assert items == [], (
        "Layer-4 missions are owned by C.1 (22:00 UTC time window), "
        "not by C.0. The materialize helper must filter on layer==1."
    )


# ============================================================================
# SECTION 5 — Last-run as first action convention (artifact contract)
# ============================================================================

def test_layer_1_writes_last_run_as_first_action(tmp_outputs_root):
    """Round-trip the convention: write_last_run is the documented first
    action of a layer's dispatch. We don't have the agent here — we just
    test the helper does write the file."""
    today = _today_dir(tmp_outputs_root)
    layer_dir = today / "1"
    layer_dir.mkdir()
    # Simulate the FIRST action.
    dispatch_helpers.write_last_run(layer_dir,
                                    datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc))
    # File exists.
    assert (layer_dir / ".last-run").exists()
    # Round-trip: reader sees what writer wrote.
    out = dispatch_helpers.read_last_run(layer_dir)
    assert out == datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)


# ============================================================================
# SECTION 6 — Coexistence with existing daily_risk_audit (Layer 4)
# ============================================================================

def test_daily_risk_audit_still_in_default_ops_yaml():
    """The Layer-4 daily_risk_audit mission (GAP-10 fix) must still be
    injected by scaffold even after we add the L1 mechanism."""
    rendered = scaffold.render_dept_yaml_draft(
        slug="maya", display_name="Maya", owner="joris", level="ops"
    )
    import yaml as _yaml
    doc = _yaml.safe_load(rendered)
    missions = doc.get("recurring_missions") or []
    risk_audit = [m for m in missions if m.get("id") == "daily_risk_audit"]
    assert risk_audit, (
        "daily_risk_audit Layer-4 mission missing from default ops dept.yaml"
    )
    assert risk_audit[0]["layer"] == 4
    assert risk_audit[0]["cadence"] == "daily"
    assert risk_audit[0]["time"] == "22:00"


def test_materialize_skips_daily_risk_audit_when_called_at_07h():
    """End-to-end: feed the materialize helper a dept whose recurring_missions
    contains only daily_risk_audit (Layer 4). At 08:00 Paris, no L1 items
    are produced — because the only mission is L4."""
    daily_risk_audit = {
        "id": "daily_risk_audit",
        "layer": 4,
        "cadence": "daily",
        "time": "22:00",
        "description": "Daily Layer-4 self-audit.",
        "output_queue": "queues/gates/",
        "creates": ["risk_audit"],
    }
    now_utc = datetime(2026, 5, 24, 6, 0, tzinfo=timezone.utc)  # 08:00 Paris
    items = dispatch_helpers.materialize_due_missions(
        [daily_risk_audit], now=now_utc, last_fired_per_mission={}
    )
    assert items == []
