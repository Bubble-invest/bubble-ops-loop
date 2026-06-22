"""
test_qa_e2e_with_real_dry_run.py — Polish Fix 1 + Fix 2 (2026-05-21).

Sibling integration sentinel to test_qa_e2e_full_walk.py. The original
sentinel mocks `_invoke_dry_run` and never drives Step 7. This sentinel
walks the SAME 5 runners (steps 1-5), promotes dept.yaml.draft →
dept.yaml (as the activation script would), then:

  Fix 1: calls `run_dry_run_full()` for REAL end-to-end to catch
         contract drift between runner output and the simulator's
         expectations.

  Fix 2: drives the ActivationRunner with a mocked activate-dept.sh,
         submits an "approuve" answer, and verifies the runner
         returns DONE + STATE.yaml status flips to "Live".

Per Notion v5 lines 925-946:
  fake data
  → Layer 1 creates queue item
  → Layer 2 drafts / plans
  → Gate created
  → Fake approval
  → Layer 3 fake execution
  → Layer 4 risk brief

Per Notion v5 lines 947-1003: Activation = PR body + status flip +
deployment.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
sys.path.insert(0, str(SKILL_ROOT))


# ----- helpers (mirror test_qa_e2e_full_walk.py) -----


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Configuring",
        "validated_steps": [],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text("", encoding="utf-8")
    return draft


def _drive_mandate(tmp_path: Path) -> None:
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("mandate")
    runner.start(state, draft)
    runner.on_answer("1")
    runner.on_answer("approuve")
    body = (
        "publier sans validation, nommer clients, conseil financier\n"
        "ops\n"
        "operator"
    )
    runner.on_answer(body)
    runner.on_answer("approuve")


def _drive_missions(tmp_path: Path) -> None:
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("missions")
    runner.start(state, draft)
    runner.on_answer("signal_scan")
    runner.on_answer("approuve")
    runner.on_answer("non")


def _drive_layers(tmp_path: Path) -> None:
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("layers")
    runner.start(state, draft)
    runner.on_answer("tous")
    for _ in range(4):
        runner.next_prompt()
        runner.on_answer("approuve")


def _drive_skills_tools(tmp_path: Path) -> None:
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    safety = 0
    while not runner.is_done() and safety < 100:
        safety += 1
        p = runner.next_prompt()
        if p is None:
            break
        lp = p.lower()
        if "autres" in lp and ("layer" in lp or "skill" in lp or "tool" in lp):
            runner.on_answer("non")
        elif ("tool" in lp and "besoin" in lp) or ("skill" in lp and "besoin" in lp):
            runner.on_answer("ok")
        else:
            runner.on_answer("approuve")


def _drive_gates_kpis(tmp_path: Path) -> None:
    from skill_lib.step_runners import get_runner
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    runner = get_runner("gates_kpis")
    runner.start(state, draft)
    runner.next_prompt()
    runner.on_answer("ok")
    safety = 0
    while not runner.is_done() and safety < 30:
        safety += 1
        p = runner.next_prompt()
        if p is None:
            break
        runner.on_answer("approuve")


def _promote_draft_to_dept_yaml(repo: Path) -> Path:
    """Mirror the activation script's draft → canonical promotion.

    The real `scripts/activate-dept.sh` flips `department.status` to
    `live` while promoting. For Step 6 dry-run we only need the file
    to exist at the canonical name with valid YAML; status stays as it
    was when step 5 closed.
    """
    src = repo / "dept.yaml.draft"
    dst = repo / "dept.yaml"
    if not src.exists():
        raise FileNotFoundError(f"dept.yaml.draft missing at {src}")
    shutil.copyfile(src, dst)
    return dst


# ----- the integration sentinel -----


def test_qa_e2e_with_real_dry_run(tmp_path):
    """5-step walk → promote → real run_dry_run_full() → no setup errors."""
    _seed_state(tmp_path)
    _seed_draft(tmp_path)
    _drive_mandate(tmp_path)
    _drive_missions(tmp_path)
    _drive_layers(tmp_path)
    _drive_skills_tools(tmp_path)
    _drive_gates_kpis(tmp_path)
    _promote_draft_to_dept_yaml(tmp_path)

    # ----- call the real simulator -----
    from skill_lib.dry_run import run_dry_run_full, DryRunResult
    result = run_dry_run_full(dept_root=tmp_path, seed=1)

    # 1. simulator returned a DryRunResult (not an exception)
    assert isinstance(result, DryRunResult), (
        f"expected DryRunResult, got {type(result)!r}"
    )

    # 2. At least 1 layer with PASS or WARN
    statuses = {lr.status for lr in result.layer_results.values()}
    assert (
        "passed" in statuses or "warning" in statuses
    ), f"no layer passed or warned: {statuses!r}"

    # 3. No setup errors in any check (missing dept.yaml / no missions)
    setup_error_substrings = (
        "missing dept.yaml",
        "no missions found",
        "dept.yaml not found",
        "schema not found",
    )
    for check in result.checks:
        msg = (check.message or "").lower()
        for needle in setup_error_substrings:
            assert needle not in msg, (
                f"setup error in {check.step}: {check.message!r}"
            )

    # 4. The simulator must USE the real dept content (not fall back to
    #    generic). Catches drift between runner output shape and simulator
    #    expectations — the whole point of this integration test.
    dept_doc = yaml.safe_load(
        (tmp_path / "dept.yaml").read_text(encoding="utf-8"))
    real_slug = dept_doc["department"]["slug"]
    real_missions = dept_doc.get("recurring_missions") or []
    assert len(real_missions) >= 1, (
        "fixture mistake: missions runner produced no recurring_missions"
    )

    # 4a. The synthesized fake queue item must derive from the real mission's
    #     `creates[]` (not the fallback generic template). This is the
    #     contract `_synthesize_fake_queue_item` advertises.
    real_mission_creates = (real_missions[0] or {}).get("creates") or []
    qi_path = result.artifacts_dir / "1" / "synthesized-queue-item.yaml"
    assert qi_path.exists(), f"layer 1 queue-item not written: {qi_path}"
    qi_doc = yaml.safe_load(qi_path.read_text(encoding="utf-8"))
    if real_mission_creates:
        assert qi_doc.get("kind") == real_mission_creates[0], (
            f"queue item kind {qi_doc.get('kind')!r} does not match the real "
            f"mission's creates[0]={real_mission_creates[0]!r} — simulator "
            f"may have fallen back to the generic fixture instead of reading "
            f"recurring_missions[0]"
        )
    # 4b. Layer 4 management-export must reference the real dept slug
    #     (not "unknown-dept" fallback). Catches dept-block-reading drift.
    me_path = result.artifacts_dir / "4" / "management-export.yaml"
    assert me_path.exists(), f"layer 4 management-export not written: {me_path}"
    me_doc = yaml.safe_load(me_path.read_text(encoding="utf-8"))
    assert me_doc.get("dept") == real_slug, (
        f"management-export dept={me_doc.get('dept')!r} does not match the "
        f"real dept slug {real_slug!r} — simulator failed to read the "
        f"department block from the promoted dept.yaml"
    )

    # 5. The Step 6 runner accepts the real DryRunResult and produces
    #    a humanized FR Bureau-de-Cadre prose summary.
    from skill_lib.step_runners import get_runner
    runner = get_runner("dry_run")
    state_path = tmp_path / "STATE.yaml"
    draft_path = tmp_path / "dept.yaml.draft"
    runner.start(state_path, draft_path)  # un-mocked: calls the real simulator
    p = runner.next_prompt()
    assert p is not None and len(p) > 50, (
        f"humanized summary too short: {p!r}"
    )
    # French Bureau-de-Cadre prose: at least one moment-of-day token
    pl = p.lower()
    assert (
        "répétition" in pl or "répétition" in p
        or "matin" in pl or "passé" in pl or "passée" in pl
    ), (
        f"humanized summary not Bureau-de-Cadre French: {p!r}"
    )


def _drive_activation(
    state_path: Path, dept_yaml_draft_path: Path,
) -> dict:
    """Polish Fix 2 — drive the ActivationRunner end-to-end.

    Mocks `_run_activation_script` so we don't actually shell out.
    Verifies:
      - The runner accepts the humanized body (no legacy English).
      - on_answer("approuve") returns Action.DONE.
      - is_done() flips to True.
      - dept.yaml::department.status flips to "live".
      - STATE.yaml::status flips to "Live" (per Notion v5 lines 947-960).
      - The activation_pr tester returned PASS (verifies the humanized
        body, not the legacy English one).

    Returns the final STATE.yaml doc for further assertions by callers.
    """
    from skill_lib.step_runners import Action, get_runner
    runner = get_runner("activation")
    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=0,
    ) as mock_activate:
        runner.start(state_path, dept_yaml_draft_path)
        # 1. The runner must NOT block on a verification failure.
        assert runner._pr_body_ok, (
            f"activation PR body failed verification: "
            f"{runner._body_test_summary!r}"
        )
        # 2. The first prompt must surface the humanized body (Notion v5
        #    lines 977-995 + msg 2702/2708).
        prompt = runner.next_prompt()
        assert prompt is not None and len(prompt) > 100, (
            f"activation prompt too short: {prompt!r}"
        )
        assert (
            "Lettre d'arrivée" in prompt
            or "Cérémonie d'arrivée" in prompt
            or "Bienvenue à" in prompt
        ), (
            f"activation prompt missing humanized h1: {prompt[:200]!r}"
        )
        # Legacy English MUST never appear.
        assert "Activate Miranda department" not in prompt, (
            "legacy English PR body slipped through"
        )
        # 3. on_answer("approuve") returns DONE.
        action = runner.on_answer("approuve")
    assert action == Action.DONE, (
        f"approve did not return DONE; got {action!r}"
    )
    mock_activate.assert_called_once()
    # 4. is_done() flips True.
    assert runner.is_done() is True

    # 5. dept.yaml::department.status flipped to "live".
    dept_path = dept_yaml_draft_path.parent / "dept.yaml"
    if dept_path.exists():
        dept_doc = yaml.safe_load(dept_path.read_text(encoding="utf-8"))
        assert dept_doc["department"]["status"] == "live", (
            f"dept.yaml status not flipped to 'live': "
            f"{dept_doc['department'].get('status')!r}"
        )

    # 6. STATE.yaml::status flipped to "Live" (per Notion v5 lines 947-960).
    #    The activation script (mocked here) does this in prod via
    #    `scripts/lib/state_yaml.py::mark_activated()`; the runner must
    #    mirror it in test/dry-run mode so the in-memory state matches.
    final_state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    assert final_state.get("status") == "Live", (
        f"STATE.yaml::status not flipped to 'Live': "
        f"{final_state.get('status')!r} (the runner should mirror the "
        f"script's mark_activated() side-effect for test parity)"
    )

    return final_state


def test_qa_e2e_drive_activation(tmp_path):
    """5-step walk → promote → real dry-run → drive activation runner."""
    _seed_state(tmp_path)
    _seed_draft(tmp_path)
    _drive_mandate(tmp_path)
    _drive_missions(tmp_path)
    _drive_layers(tmp_path)
    _drive_skills_tools(tmp_path)
    _drive_gates_kpis(tmp_path)
    _promote_draft_to_dept_yaml(tmp_path)

    # Step 6 — drive the dry-run runner so STATE has step_progress.dry_run
    # marked validated (so STATE.yaml is in the shape Step 7 expects).
    from skill_lib.step_runners import get_runner
    dr_runner = get_runner("dry_run")
    dr_runner.start(tmp_path / "STATE.yaml", tmp_path / "dept.yaml.draft")
    dr_runner.on_answer("approuve")
    assert dr_runner.is_done() is True, (
        "dry-run runner did not complete on the bootstrapped fixture; "
        "step 7 cannot proceed"
    )

    # Step 7 — drive activation.
    state_doc = _drive_activation(
        tmp_path / "STATE.yaml", tmp_path / "dept.yaml.draft",
    )

    # The activation runner persisted step_progress.activation.current_status
    # == "validated".
    progress = (state_doc.get("step_progress") or {}).get("activation") or {}
    assert progress.get("current_status") == "validated", (
        f"activation step_progress not marked validated: {progress!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
