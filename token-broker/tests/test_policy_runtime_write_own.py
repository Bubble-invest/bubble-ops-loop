"""runtime_write_own policy class tests — write only to allowed_paths in own repo.

Notion v4 line 620: allowed_paths = outputs/**, queues/**, inbox/**.
"""

from __future__ import annotations


def test_runtime_write_own_paths_match_outputs_queues_inbox(ops_policy_yaml):
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    for path in [
        "outputs/2026-05-20/1/summary.md",
        "outputs/2026-05-20/4/risk-kpis.yaml",
        "queues/research/task-001.yaml",
        "queues/gates/gate-007.yaml",
        "inbox/decisions/dec-001.yaml",
    ]:
        allowed, reasons = p.enforce(
            actor="ops-loop-fixture",
            repo="bubble-ops-fixture",
            action="runtime_write_own",
            paths=[path],
        )
        assert allowed, f"{path} should be allowed: {reasons}"


def test_runtime_write_own_rejects_dept_yaml_path(ops_policy_yaml):
    """Per Notion v4 line 622: dept.yaml change requires settings_pr, NOT runtime_write_own."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        paths=["dept.yaml"],
    )
    assert not allowed
    # Either "not in allowed_paths" or "is structural; use action=settings_pr instead"
    # would be a correct deny reason — both express the same denial intent.
    assert any(
        "not in allowed_paths" in r or "structural" in r for r in reasons
    ), f"reasons did not include allowed_paths or structural denial: {reasons}"


def test_runtime_write_own_rejects_layer_prompt_path(ops_policy_yaml):
    """layers/*/PROMPT.md is structural -> settings_pr required."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, _ = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        paths=["layers/1/PROMPT.md"],
    )
    assert not allowed


def test_runtime_write_own_rejects_other_repo(ops_policy_yaml):
    """An ops dept cannot write into another dept's repo."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-tony",
        action="runtime_write_own",
        paths=["outputs/2026-05-20/1/summary.md"],
    )
    assert not allowed
    assert any("not the actor's own_repo" in r for r in reasons)


def test_runtime_write_own_rejects_mixed_paths(ops_policy_yaml):
    """If ANY path is outside allowed set, the whole batch is denied."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, _ = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        paths=["outputs/2026-05-20/1/summary.md", "skills/prompt-injector/SKILL.md"],
    )
    assert not allowed
