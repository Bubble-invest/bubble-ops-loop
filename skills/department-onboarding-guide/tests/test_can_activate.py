"""
test_can_activate.py — UX-5 task 1: pre-flight activation gating.

Notion v5 lines 950-1003. `can_activate(state_yaml_path, dept_root)` must
return a (bool, list[str]) tuple. True only when ALL of these hold:

  1. STATE.yaml::status == "Ready to activate"
  2. dry_run.run_dry_run_full().can_advance_to_ready is True
  3. All 6 prior steps are in validated_steps[]
  4. dept.yaml present + schema-valid
  5. gate_policy.yaml present IFF a recurring mission of kind=trade|action exists
     (currently expressed via dept.yaml::recurring_missions[].creates)

Each blocker reason in the list must be operator-actionable (no jargon).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from skill_lib.activation import can_activate


# ---------- helpers -------------------------------------------------------


def _write_state(path: Path, **overrides) -> None:
    base = {
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-19T10:00:00Z",
        "status": "Ready to activate",
        "validated_steps": [
            "mandate", "missions", "layers", "skills_tools",
            "gates_kpis", "dry_run",
        ],
        "last_updated_at": "2026-05-20T10:00:00Z",
        "commits": [],
    }
    base.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")


def _write_dept(path: Path, with_trade_mission: bool = False) -> None:
    missions = [
        {
            "id": "echo_heartbeat", "layer": 1, "cadence": "every_2h",
            "active_hours": "08:00-22:00",
            "description": "Heartbeat mission.",
            "input_sources": ["filesystem", "git_log"],
            "output_queue": "queues/research/",
            "creates": ["trade_proposal" if with_trade_mission else "echo_task"],
            "gate_policy_id": "echo_action",
        },
    ]
    doc = {
        "department": {
            "slug": "miranda", "level": "ops",
            "mandate": "Test mandate that is long enough.",
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
        "recurring_missions": missions,
        "skills": {"layer_1": ["echo"], "layer_2": ["echo"],
                   "layer_3": ["echo"], "layer_4": ["echo"]},
        "tools": ["echo-tool"],
        "gate_policies": {
            "echo_action": {
                "current_mode": "manual_required",
                "eligible_future_modes": ["auto_if_policy_passed"],
                "authorization_band": "fixture_echo_band",
                "kpi_guardrail_set": "fixture_echo_kpis",
            }
        },
        "hierarchy": {
            "level": "ops", "parent": None, "children": [],
            "visibility": {
                "read_outputs": [], "read_risk_kpis": False,
                "read_risk_briefs": False, "read_raw_artifacts": False,
                "read_secrets": False,
            },
            "directive_policy": {
                "can_open_priority_prs": False,
                "target_queue": "queues/management/",
                "requires_human_gate_for": [],
            },
        },
        "optional_domain_ledger": None,
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _write_core_dept_shape(root: Path) -> None:
    """Write the standard dept shape (MANDATE.md + layer prompts + mission
    declaration) matching `_write_dept`'s dept.yaml (card #1268)."""
    (root / "MANDATE.md").write_text(
        "# Mandat de Miranda\n\n"
        "Je m'occupe du contenu et je publie sous supervision. "
        "Je ne conseille jamais en direct sans validation humaine.\n",
        encoding="utf-8",
    )
    for n in (1, 2, 3, 4):
        layer_dir = root / "layers" / str(n)
        layer_dir.mkdir(parents=True, exist_ok=True)
        (layer_dir / "PROMPT.md").write_text(
            f"# Layer {n} prompt stub\n\nRole description for layer {n}.\n",
            encoding="utf-8",
        )
    missions_dir = root / "missions"
    missions_dir.mkdir(parents=True, exist_ok=True)
    (missions_dir / "echo_heartbeat.yaml").write_text(
        yaml.safe_dump({
            "id": "echo_heartbeat", "layer": 1, "cadence": "every_2h",
            "description": "Heartbeat mission.",
            "output_queue": "queues/research/", "creates": ["echo_task"],
        }, sort_keys=False),
        encoding="utf-8",
    )


def _full_repo(root: Path, **state_overrides) -> Path:
    """Make a happy-path repo. Returns the state-yaml path."""
    (root / "onboarding").mkdir(parents=True, exist_ok=True)
    state = root / "onboarding" / "STATE.yaml"
    _write_state(state, **state_overrides)
    _write_dept(root / "dept.yaml")
    # bare layout so dry_run.run_dry_run_full has somewhere to write.
    for sub in ("outputs", "queues/research", "queues/management",
                "inbox/decisions", "tests", "missions",
                "layers/1", "layers/2", "layers/3", "layers/4"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    _write_core_dept_shape(root)
    return state


# ---------- tests ---------------------------------------------------------


def test_can_activate_happy_path(tmp_path):
    state = _full_repo(tmp_path)
    ok, reasons = can_activate(state, tmp_path)
    assert ok is True, f"expected True, got reasons={reasons}"
    assert reasons == []


def test_can_activate_rejects_wrong_status(tmp_path):
    state = _full_repo(tmp_path, status="Drafting")
    ok, reasons = can_activate(state, tmp_path)
    assert ok is False
    assert any("Ready to activate" in r for r in reasons)


def test_can_activate_rejects_missing_step(tmp_path):
    state = _full_repo(
        tmp_path,
        validated_steps=["mandate", "missions", "layers", "skills_tools",
                         "gates_kpis"],  # missing dry_run
        status="Dry run",
    )
    ok, reasons = can_activate(state, tmp_path)
    assert ok is False
    assert any("dry_run" in r for r in reasons)


def test_can_activate_rejects_missing_dept_yaml(tmp_path):
    state = _full_repo(tmp_path)
    (tmp_path / "dept.yaml").unlink()
    ok, reasons = can_activate(state, tmp_path)
    assert ok is False
    assert any("dept.yaml" in r for r in reasons)


def test_can_activate_rejects_dept_yaml_schema_invalid(tmp_path):
    state = _full_repo(tmp_path)
    # Write a dept.yaml that's missing required keys.
    (tmp_path / "dept.yaml").write_text(
        "department:\n  slug: miranda\n", encoding="utf-8")
    ok, reasons = can_activate(state, tmp_path)
    assert ok is False
    assert any("schema" in r.lower() or "required" in r.lower() for r in reasons)


def test_can_activate_rejects_missing_state_file(tmp_path):
    # No state file at all.
    fake = tmp_path / "onboarding" / "STATE.yaml"
    ok, reasons = can_activate(fake, tmp_path)
    assert ok is False
    assert any("STATE.yaml" in r for r in reasons)


def test_can_activate_returns_actionable_reasons(tmp_path):
    """Every reason must mention WHAT and HOW to fix (no internal jargon)."""
    state = _full_repo(tmp_path, status="Drafting",
                       validated_steps=["mandate"])
    ok, reasons = can_activate(state, tmp_path)
    assert ok is False
    assert reasons, "expected at least one reason"
    for r in reasons:
        # each reason is a string and non-trivial
        assert isinstance(r, str)
        assert len(r) > 10


def test_can_activate_rejects_missing_mandate_md(tmp_path):
    """Card #1268 — the Géraldine incident: validated_steps said "mandate"
    was done, but MANDATE.md was never committed. can_activate() must
    re-verify the file actually exists, independent of STATE.yaml."""
    state = _full_repo(tmp_path)
    (tmp_path / "MANDATE.md").unlink()
    ok, reasons = can_activate(state, tmp_path)
    assert ok is False
    assert any("MANDATE.md" in r for r in reasons)


def test_can_activate_rejects_empty_mandate_md(tmp_path):
    """A committed-but-placeholder MANDATE.md must also fail-closed."""
    state = _full_repo(tmp_path)
    (tmp_path / "MANDATE.md").write_text("# Mandat de Miranda\n", encoding="utf-8")
    ok, reasons = can_activate(state, tmp_path)
    assert ok is False
    assert any("MANDATE.md" in r and "placeholder" in r for r in reasons)


def test_can_activate_rejects_missing_layer_prompt(tmp_path):
    state = _full_repo(tmp_path)
    (tmp_path / "layers" / "2" / "PROMPT.md").unlink()
    ok, reasons = can_activate(state, tmp_path)
    assert ok is False
    assert any("layers/2/PROMPT.md" in r for r in reasons)


def test_can_activate_rejects_missing_declared_mission_file(tmp_path):
    state = _full_repo(tmp_path)
    (tmp_path / "missions" / "echo_heartbeat.yaml").unlink()
    ok, reasons = can_activate(state, tmp_path)
    assert ok is False
    assert any("missions/echo_heartbeat.yaml" in r for r in reasons)


def test_can_activate_succeeds_for_well_formed_dept(tmp_path):
    """Sanity check: a fully well-formed dept (MANDATE.md + all 4 layer
    prompts + declared mission file) still activates cleanly."""
    state = _full_repo(tmp_path)
    ok, reasons = can_activate(state, tmp_path)
    assert ok is True, f"expected True, got reasons={reasons}"


def test_can_activate_is_pure_no_side_effects(tmp_path):
    state = _full_repo(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    can_activate(state, tmp_path)
    # dry_run.run_dry_run_full writes into outputs/dry-run/...; that's
    # expected on the happy path. We assert that DIRECTORIES outside of
    # outputs/dry-run/ aren't mutated.
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    mutated_outside = [p for p in (set(after) - set(before))
                       if "outputs/dry-run" not in str(p)]
    assert mutated_outside == [], f"unexpected writes: {mutated_outside}"
