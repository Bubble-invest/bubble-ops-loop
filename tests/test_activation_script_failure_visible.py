"""
test_activation_script_failure_visible.py — Sprint Maya-blocker Fix 3.

Pins ActivationRunner's behavior when `scripts/activate-dept.sh` exits
non-zero: the operator must SEE an explicit French error message on the
next prompt. Previously, the runner silently re-emitted the same
lettre d'arrivée — operator typed "approuve", saw the same letter, and
concluded that nothing happened (or worse, that the system was broken).

Mirrors the pattern from Sprint correctif Fix 4 on the gates_kpis
runner (one-shot _last_rejection_reason). The error blurb names:
  - the exit code (so operator can grep journald),
  - a captured stderr summary (the actual failure mode),
  - the recovery hint (`sudo journalctl -u ops-loop-<slug>.service -n 30`).

Notion v5 lines 947-1003 require activation to be reversible / re-tryable;
this fix preserves that by NOT flipping is_done() when the script fails.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

SKILL_ROOT = (
    Path(__file__).resolve().parent.parent
    / "skills" / "department-onboarding-guide"
)
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Ready to activate",
        "validated_steps": [
            "mandate", "missions", "layers",
            "skills_tools", "gates_kpis", "dry_run",
        ],
        "last_updated_at": "2026-05-21T09:00:00Z",
        "commits": [
            {"step": "dry_run", "commit_sha": "abcdef0",
             "validated_at": "2026-05-21T08:50:00Z"},
        ],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "level": "ops",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "owner": "operator",
            "status": "onboarding",
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
        "recurring_missions": [],
        "skills": {"layer_1": ["content-signal-scanner"]},
        "tools": [],
        "gate_policies": {},
    }, sort_keys=False), encoding="utf-8")
    promoted = tmp_path / "dept.yaml"
    promoted.write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
    return draft


def test_exit_1_surfaces_french_error_with_code(tmp_path):
    """rc=1 → the next_prompt is prefixed with a French failure message
    that names exit code 1 + a recovery hint."""
    from skill_lib.step_runners import get_runner

    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)

    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=1,
    ):
        runner.on_answer("approuve")

    assert runner.is_done() is False, (
        "runner must NOT flip is_done when activation script failed"
    )
    p = runner.next_prompt()
    assert p is not None
    # The prompt is "failure_message + separator + lettre d'arrivée body".
    # We assert on the FAILURE MESSAGE portion only (before the ---
    # separator), because the lettre body legitimately mentions Morty
    # in its operator checklist ("l'équipe technique a installé ce qu'il
    # faut sur Morty"). The new persona-aware copy applies to the failure
    # surface (msg 2770, 2026-05-21).
    failure_part = p.split("---", 1)[0]
    # Hard requirements on the failure surface: persona-aware copy — NO
    # sysadmin jargon. Operator-facing text refers operator to "votre
    # équipe technique" (a neutral phrasing that internally maps to
    # Rick (R&D) but stays reusable for future client-facing agents).
    assert "code 1" in failure_part, (
        f"exit code 1 missing from failure surface: {failure_part!r}"
    )
    fpl = failure_part.lower()
    assert "erreur" in fpl or "échou" in fpl or "scrip" in fpl, (
        f"no French error keyword in failure surface: {failure_part!r}"
    )
    # Anti-jargon assertions on the failure message itself.
    assert "journalctl" not in failure_part, (
        f"sysadmin command leaked to operator: {failure_part!r}"
    )
    assert "sudo" not in fpl, (
        f"sudo command leaked to operator: {failure_part!r}"
    )
    assert "morty" not in fpl, (
        f"VPS hostname leaked to failure surface: {failure_part!r}"
    )
    # Persona-aware referral: must point operator to "équipe technique"
    assert "équipe technique" in fpl, (
        f"recovery hint should refer operator to 'équipe technique': "
        f"{failure_part!r}"
    )


def test_exit_2_surfaces_french_error_with_code(tmp_path):
    """rc=2 (pre-flight gate refused) — distinct code surfaced."""
    from skill_lib.step_runners import get_runner

    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)

    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=2,
    ):
        runner.on_answer("approuve")

    assert runner.is_done() is False
    p = runner.next_prompt()
    assert p is not None
    assert "code 2" in p, f"exit code 2 missing: {p[:300]!r}"


def test_exit_127_surfaces_french_error_with_code(tmp_path):
    """rc=127 (command not found) — high code surfaced verbatim."""
    from skill_lib.step_runners import get_runner

    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)

    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=127,
    ):
        runner.on_answer("approuve")

    assert runner.is_done() is False
    p = runner.next_prompt()
    assert p is not None
    assert "code 127" in p, f"exit code 127 missing: {p[:300]!r}"


def test_failure_message_is_one_shot_cleared_after_render(tmp_path):
    """The error blurb must be one-shot — surfaced once, then cleared.

    Mirrors Sprint correctif Fix 4 (gates_kpis _last_rejection_reason).
    The operator should NOT see the error message lingering on every
    subsequent prompt — only on the next prompt after the failure.
    """
    from skill_lib.step_runners import get_runner

    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)

    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=1,
    ):
        runner.on_answer("approuve")

    p1 = runner.next_prompt()
    assert "code 1" in p1
    p2 = runner.next_prompt()
    assert p2 is not None
    assert "code 1" not in p2, (
        f"failure message lingered onto second prompt: {p2[:300]!r}"
    )


def test_success_does_not_emit_failure_message(tmp_path):
    """rc=0 → no failure blurb at all (regression guard)."""
    from skill_lib.step_runners import Action, get_runner

    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)

    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=0,
    ):
        action = runner.on_answer("approuve")
    assert action == Action.DONE
    # is_done True → next_prompt returns None per the runner contract
    assert runner.next_prompt() is None
