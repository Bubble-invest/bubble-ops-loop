"""
test_activation_runs_real_in_production.py — Sprint Maya-blocker Fix 1.

SENTINEL TEST. Pinned by Notion v5 lines 947-1003 ("Activation").

Bug history: Phase G shipped `_run_activation_script` with `--dry-run`
HARDCODED into the subprocess command (`activation.py:84`). That made
tests pass (the script's dry-run path is observable + cheap), but it
also meant that when the operator typed "approuve" in production:
  1. The dry-run script ran (no GitHub PR opened, no Morty deploy).
  2. ActivationRunner._do_activate() flipped STATE.yaml::status -> "Live"
     anyway, because the runner only checks rc==0.
Result: the dept was marked "Live" in the console, but nothing was
actually deployed. Silent broken activation — the worst possible
failure mode for the first live Maya onboarding.

This test exercises the operator-facing "approuve" path end-to-end and
ASSERTS that the subprocess command does NOT contain `--dry-run`. If
anyone ever re-adds it (or refactors and accidentally hardcodes it
again), this sentinel fires with a loud French message.

The script itself is mocked at the subprocess.run boundary (CI must
never shell out to the real `activate-dept.sh`), but the COMMAND list
the runner would have passed is captured + asserted.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

# Ensure the skill is importable.
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


def test_approve_invokes_real_activation_no_dry_run_flag(tmp_path):
    """SENTINEL: the production "approuve" path must NOT pass --dry-run.

    Drives the activation runner via on_answer("approuve") and inspects
    the captured subprocess.run argv. Fails loudly if `--dry-run` appears
    in the command list — that would mean the operator's approval triggers
    a fake activation while the runner flips STATE.yaml to "Live" anyway
    (silent broken activation).

    We mock at the subprocess.run boundary AND force the script-exists
    check to True so the runner takes the production code path
    (otherwise it short-circuits to rc=0 because the real script is
    absent in the temp tree).
    """
    from skill_lib.step_runners import Action, get_runner

    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)

    runner = get_runner("activation")
    runner.start(state, draft)

    captured_cmds: list[list[str]] = []

    def fake_subprocess_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    # Force the runner's `script.exists()` check to True so we exercise
    # the subprocess.run path even though no real script is on disk.
    with patch(
        "skill_lib.step_runners.activation.subprocess.run",
        side_effect=fake_subprocess_run,
    ), patch.object(Path, "exists", return_value=True):
        action = runner.on_answer("approuve")

    assert action == Action.DONE, (
        f"runner did not transition to DONE on approve; action={action!r}"
    )
    assert captured_cmds, (
        "subprocess.run was never called — the runner short-circuited. "
        "The sentinel needs to actually exercise the script path."
    )
    cmd = captured_cmds[0]
    # The bite assertion.
    assert "--dry-run" not in cmd, (
        f"REGRESSION: ActivationRunner passed --dry-run to "
        f"scripts/activate-dept.sh in the production approve path. "
        f"This would silently skip the real activation (no PR, no Morty "
        f"deploy) while flipping STATE.yaml to 'Live'. "
        f"Captured cmd: {cmd!r}"
    )
    # Positive sanity: the command still targets the activate script and
    # carries the slug + repo-dir args.
    assert any("activate-dept.sh" in part for part in cmd), (
        f"cmd does not invoke activate-dept.sh: {cmd!r}"
    )
    assert any(part.startswith("--slug=") for part in cmd), (
        f"cmd missing --slug=: {cmd!r}"
    )
    assert any(part.startswith("--repo-dir=") for part in cmd), (
        f"cmd missing --repo-dir=: {cmd!r}"
    )


def test_dry_run_param_is_explicit_opt_in():
    """If a caller WANTS the dry-run variant (e.g. an E2E test harness),
    they must opt in explicitly via a kwarg.

    This pins the refactored signature: dry_run defaults to False; the
    operator-facing path never accidentally inherits dry-run mode.
    """
    from skill_lib.step_runners.activation import _run_activation_script
    import inspect
    sig = inspect.signature(_run_activation_script)
    params = sig.parameters
    assert "dry_run" in params, (
        "_run_activation_script must expose an explicit `dry_run` kwarg "
        "so callers opt in to dry-run mode rather than inheriting it "
        "as a hidden default. (Fix 1 of Sprint Maya-blocker.)"
    )
    assert params["dry_run"].default is False, (
        f"`dry_run` must default to False (production-safe). "
        f"Current default: {params['dry_run'].default!r}"
    )


def test_dry_run_true_adds_flag(tmp_path):
    """Opt-in dry-run kwarg adds --dry-run to the cmd.

    Ensures the dry-run capability is preserved (test harnesses can still
    use it) — we're not eliminating dry-run, just making the operator
    path production-safe by default.
    """
    from skill_lib.step_runners.activation import _run_activation_script

    captured_cmds: list[list[str]] = []

    def fake_subprocess_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch(
        "skill_lib.step_runners.activation.subprocess.run",
        side_effect=fake_subprocess_run,
    ), patch.object(Path, "exists", return_value=True):
        rc = _run_activation_script("miranda", tmp_path, dry_run=True)

    assert rc == 0
    assert captured_cmds, "subprocess.run was not invoked"
    assert "--dry-run" in captured_cmds[0], (
        f"dry_run=True did NOT add --dry-run to cmd: {captured_cmds[0]!r}"
    )
