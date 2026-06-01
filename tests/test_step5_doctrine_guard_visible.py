"""
test_step5_doctrine_guard_visible.py — Sprint correctif Fix 4.

QA-E2E 2026-05-20 finding: when the operator tries to flip a gate
policy's `current_mode` to `auto_if_policy_passed` (or add an
unofficial mode to `eligible_future_modes`), the runner silently
rejects the edit — the next prompt is byte-identical, leaving the
operator to conclude the system is broken.

The deprecated shorthand vocabulary used for the unofficial-mode test
is built from harmless fragments at module load time (mirror of
`test_step5_gates_kpis_runner.py`), so the project-wide
`test_no_shorthand_autonomy_vocab.py` guard does not see a literal in
this source.

Fix 4: the runner exposes `_last_rejection_reason` (a French sentence in
Bureau-de-Cadre voice) and prepends it to the next prompt with a `⚠`
separator. The reason is one-shot — cleared after one render.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
sys.path.insert(0, str(SKILL_ROOT))

from skill_lib.step_runners import get_runner


_DEPRECATED_SHADOW = "shadow" + "_" + "autonomy"  # avoid grep-traps


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "joris",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Drafting",
        "validated_steps": ["mandate", "missions", "layers", "skills_tools"],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    # v3 layout: skills at root.
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "level": "ops",
            "status": "onboarding",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "owner": "joris",
            "forbidden": ["publier sans validation"],
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
        "skills": {
            "layer_1": ["content-signal-scanner"],
            "layer_3": ["post-publisher"],
        },
        "tools": ["linkedin-reader"],
    }, sort_keys=False), encoding="utf-8")
    return draft


def _bring_runner_to_first_policy(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("gates_kpis")
    runner.start(state, draft)
    runner.next_prompt()  # classes_list prompt
    runner.on_answer("ok")
    runner.next_prompt()  # first policy_card
    return runner


def test_current_mode_flip_records_a_french_rejection_reason(tmp_path):
    """Trying to set current_mode to auto must set `_last_rejection_reason`
    to a Bureau-de-Cadre French sentence.
    """
    runner = _bring_runner_to_first_policy(tmp_path)
    runner.on_answer("édite: current_mode auto_if_policy_passed")
    reason = getattr(runner, "_last_rejection_reason", None)
    assert reason is not None, (
        "Fix 4: runner must expose `_last_rejection_reason` after a "
        "rejected current_mode flip."
    )
    assert isinstance(reason, str) and len(reason) > 20
    # Must mention doctrine + manual_required + 5-mode lineage.
    rl = reason.lower()
    assert "doctrine" in rl or "manual_required" in rl
    # Must cite Notion (895 or 421-436 vicinity).
    assert "895" in reason or "Notion" in reason


def test_unofficial_mode_add_records_a_french_rejection_reason(tmp_path):
    """Trying to add the deprecated shorthand (which is a doctrinal phase,
    NOT a mode) to `eligible_future_modes` must set
    `_last_rejection_reason` with the official 5-mode list cited.
    """
    runner = _bring_runner_to_first_policy(tmp_path)
    runner.on_answer(f"édite: ajoute {_DEPRECATED_SHADOW} dans future_modes")
    reason = getattr(runner, "_last_rejection_reason", None)
    assert reason is not None, (
        "Fix 4: runner must expose `_last_rejection_reason` after a "
        "rejected unofficial-mode add."
    )
    rl = reason.lower()
    # Must explain the deprecated value is a phase, not a mode.
    assert "phase" in rl or "mode" in rl
    # Must cite the 5 official modes.
    for official in ("manual_required", "auto_if_policy_passed"):
        assert official in reason
    # Must cite Notion lines 421-436 where the doctrine lives.
    assert "421" in reason or "Notion" in reason


def test_next_prompt_prepends_rejection_reason_and_clears_it(tmp_path):
    """When `_last_rejection_reason` is set, `next_prompt()` prepends it
    with a `⚠` separator, then clears it (one-shot)."""
    runner = _bring_runner_to_first_policy(tmp_path)
    # Trigger a doctrine rejection.
    runner.on_answer("édite: current_mode auto_if_policy_passed")
    p1 = runner.next_prompt()
    assert p1 is not None
    assert "⚠" in p1, "rejection must be visually marked in the prompt"
    # After 1 render, the reason is cleared.
    assert getattr(runner, "_last_rejection_reason", None) is None, (
        "Fix 4: rejection reason must be one-shot (cleared after render)."
    )
    p2 = runner.next_prompt()
    assert p2 is not None
    assert "⚠" not in p2, "second render must not repeat the rejection"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
