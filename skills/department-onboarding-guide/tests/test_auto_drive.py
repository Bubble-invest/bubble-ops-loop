"""
Phase G2 — tests for skill_lib/auto_drive.py.

The auto_drive module gives the eclosing agent the building blocks to drive
its own 7-step onboarding: which step is next, what prompt to send, how to
record completion.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _write_state(path: Path, *, status: str, validated: list) -> Path:
    """Helper to write a minimal STATE.yaml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": 1,
        "slug": "smoke",
        "display_name": "Smoke",
        "owner": "joris",
        "created_at": "2026-05-21T00:00:00Z",
        "status": status,
        "validated_steps": validated,
        "last_updated_at": "2026-05-21T00:00:00Z",
        "commits": [],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# get_current_step — boundary cases
# ---------------------------------------------------------------------------

def test_get_current_step_returns_mandate_at_init(tmp_path: Path) -> None:
    """A freshly bootstrapped dept (status=Idea, no validated steps) should
    have 'mandate' as the next step."""
    from skill_lib.auto_drive import get_current_step
    p = _write_state(tmp_path / "onboarding" / "STATE.yaml",
                     status="Idea", validated=[])
    assert get_current_step(p) == "mandate"


def test_get_current_step_skips_validated_steps(tmp_path: Path) -> None:
    """If mandate + missions are validated, the next step is 'layers'."""
    from skill_lib.auto_drive import get_current_step
    p = _write_state(tmp_path / "onboarding" / "STATE.yaml",
                     status="Drafting", validated=["mandate", "missions"])
    assert get_current_step(p) == "layers"


def test_get_current_step_returns_activation_when_all_validated(tmp_path: Path) -> None:
    """When all 6 work-steps are validated, the next step is 'activation'."""
    from skill_lib.auto_drive import get_current_step
    p = _write_state(
        tmp_path / "onboarding" / "STATE.yaml",
        status="Ready to activate",
        validated=["mandate", "missions", "layers", "skills_tools",
                   "gates_kpis", "dry_run"],
    )
    assert get_current_step(p) == "activation"


# ---------------------------------------------------------------------------
# get_step_prompt — must return a human-facing FR prompt with concrete options
# ---------------------------------------------------------------------------

def test_get_step_prompt_mandate_has_3_options() -> None:
    """The mandate step prompt must contain 3 concrete options."""
    from skill_lib.auto_drive import get_step_prompt
    msg = get_step_prompt("mandate")
    assert isinstance(msg, str) and len(msg) > 50
    # Must be French Bureau-de-Cadre voice (asks {{OPERATOR}} a concrete choice).
    low = msg.lower()
    assert "mandat" in low or "mandate" in low
    # 3 options structure (numbered list or A/B/C).
    assert ("1." in msg and "2." in msg and "3." in msg) or \
           ("A)" in msg and "B)" in msg and "C)" in msg) or \
           ("1)" in msg and "2)" in msg and "3)" in msg)


def test_get_step_prompt_for_each_of_the_7_steps_returns_text() -> None:
    """Every step name (1..7) must yield a non-empty FR prompt."""
    from skill_lib.auto_drive import get_step_prompt
    for step in ("mandate", "missions", "layers", "skills_tools",
                 "gates_kpis", "dry_run", "activation"):
        msg = get_step_prompt(step)
        assert isinstance(msg, str) and len(msg) > 30, \
            f"step '{step}' returned empty prompt"


def test_get_step_prompt_raises_for_unknown_step() -> None:
    """Unknown step names raise ValueError."""
    from skill_lib.auto_drive import get_step_prompt
    with pytest.raises(ValueError):
        get_step_prompt("not-a-step")


# ---------------------------------------------------------------------------
# record_step_completion — appends, idempotent, transitions status
# ---------------------------------------------------------------------------

def test_record_step_completion_appends_to_validated_steps(tmp_path: Path) -> None:
    """Calling record_step_completion adds the step + advances status."""
    from skill_lib.auto_drive import record_step_completion
    p = _write_state(tmp_path / "onboarding" / "STATE.yaml",
                     status="Idea", validated=[])
    record_step_completion(p, "mandate",
                           artifact_paths=[tmp_path / "MANDATE.md"])
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "mandate" in doc["validated_steps"]
    assert doc["status"] == "Configuring"


def test_record_step_completion_is_idempotent(tmp_path: Path) -> None:
    """Calling record_step_completion twice for the same step is a no-op
    the second time (no duplicate in validated_steps)."""
    from skill_lib.auto_drive import record_step_completion
    p = _write_state(tmp_path / "onboarding" / "STATE.yaml",
                     status="Idea", validated=[])
    record_step_completion(p, "mandate", artifact_paths=[])
    record_step_completion(p, "mandate", artifact_paths=[])
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    # Exactly one occurrence of 'mandate'.
    assert doc["validated_steps"].count("mandate") == 1


def test_record_step_completion_full_progression(tmp_path: Path) -> None:
    """Walking through all 6 work-steps drives status Idea -> Ready to activate."""
    from skill_lib.auto_drive import record_step_completion, get_current_step
    p = _write_state(tmp_path / "onboarding" / "STATE.yaml",
                     status="Idea", validated=[])
    expected_after = [
        ("mandate", "Configuring"),
        ("missions", "Drafting"),
        ("layers", "Drafting"),
        ("skills_tools", "Needs validation"),
        ("gates_kpis", "Dry run"),
        ("dry_run", "Ready to activate"),
    ]
    for step, expected_status in expected_after:
        assert get_current_step(p) == step, \
            f"expected next step '{step}', got '{get_current_step(p)}'"
        record_step_completion(p, step, artifact_paths=[])
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert doc["status"] == expected_status, \
            f"after '{step}', expected status '{expected_status}', got '{doc['status']}'"
    # After all 6, the next step is activation
    assert get_current_step(p) == "activation"
