"""#961 regression: Policy.enforce() must be REPO-AWARE for the structural
check, not just the repo-agnostic STRUCTURAL_PATH_GLOBS.

Board incident: commit 5513824 pushed scripts/lib/budget.py directly to
bubble-ops-loop/main with no PR/review. FRAMEWORK_STRUCTURAL_PATH_GLOBS
(scripts/lib/**, .github/**, token-broker/**, git-guard/**, and — as of
#961 — scripts/** broadly) already existed (governance fix 2026-06-09,
#55/ce90bb2) via `is_structural_for_repo()`, but `Policy.enforce()`'s
`runtime_write_own` AND `settings_pr` branches only ever called the
repo-agnostic `_is_structural()`, so the framework-only list was never
actually consulted by the live guard/broker push path. This is the exact
gap #961 closes — wiring `is_structural_for_repo(path, repo)` into both
branches.

Uses a policy whose `own_repo` IS bubble-ops-loop (a shape that doesn't
exist in any deployed policy today — see the FRAMEWORK_STRUCTURAL_PATH_GLOBS
comment in src/policy.py — but proves the enforcement logic itself is now
correct regardless of what gets deployed).
"""

from __future__ import annotations

import pytest
import yaml


@pytest.fixture
def framework_repo_policy_yaml(tmp_path):
    """A hypothetical actor whose own_repo is the framework repo itself, with
    scripts/** in its allowed_paths — the worst case for this test: even if
    an operator misconfigured a dept's policy to point at bubble-ops-loop
    AND allow-listed scripts/**, the structural lock must still win."""
    data = {
        "github_access": {
            "actor": "ops-loop-framework-test",
            "own_repo": "bubble-ops-loop",
            "read": ["bubble-ops-loop"],
            "write": [
                {
                    "repo": "bubble-ops-loop",
                    "allowed_paths": ["outputs/**", "queues/**", "inbox/**", "scripts/**"],
                    "mode": "direct_runtime_commit",
                }
            ],
            "pull_requests": {"can_open_to": []},
        }
    }
    path = tmp_path / "framework-policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.parametrize(
    "path",
    [
        "scripts/lib/budget.py",          # the exact #961 incident file
        "scripts/lib/dispatch_helpers.py",
        "scripts/lib/scaffold.py",
        "scripts/vendor-dept-libs.sh",    # scripts/ broadly (not under lib/)
        "scripts/dispatch_directives.py",
        ".github/workflows/tests.yml",
        "token-broker/src/policy.py",
        "git-guard/src/guard.py",
    ],
)
def test_runtime_write_own_denies_framework_paths_on_framework_repo(
    framework_repo_policy_yaml, path
):
    from src.policy import Policy

    p = Policy.from_yaml(framework_repo_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-framework-test",
        repo="bubble-ops-loop",
        action="runtime_write_own",
        paths=[path],
    )
    assert not allowed, f"{path} must be DENIED for runtime_write_own on bubble-ops-loop: {reasons}"
    assert any("structural" in r for r in reasons), reasons


@pytest.mark.parametrize(
    "path",
    ["scripts/lib/dispatch_helpers.py", "scripts/vendor-dept-libs.sh"],
)
def test_runtime_write_own_allows_same_paths_on_dept_repo(ops_policy_yaml, path):
    """Sanity / no-loosening check: the SAME scripts/ paths on a DEPT repo
    (bubble-ops-fixture) are governed only by that dept's allowed_paths, not
    by the framework-only glob. ops_policy_yaml's allowed_paths does NOT
    include scripts/**, so this is expected to deny too — but for the
    ORDINARY "not in allowed_paths" reason, never "structural". Proves the
    framework glob does not leak into dept-repo enforcement."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        paths=[path],
    )
    assert not allowed  # not in this dept's allowed_paths at all
    assert all("structural" not in r for r in reasons), (
        f"{path} on a dept repo must NOT be denied as structural (framework-only "
        f"glob must not leak to dept repos): {reasons}"
    )


def test_settings_pr_eligible_for_scripts_lib_on_framework_repo(framework_repo_policy_yaml):
    """The PR route must stay OPEN for framework-lib changes — #961 denies
    the direct-push route (above) but must not close off settings_pr too,
    or a legitimate scripts/lib/ fix would have NO push path at all."""
    from src.policy import Policy

    p = Policy.from_yaml(framework_repo_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-framework-test",
        repo="bubble-ops-loop",
        action="settings_pr",
        paths=["scripts/lib/budget.py"],
    )
    assert allowed, f"settings_pr for scripts/lib/ on the framework repo must be allowed: {reasons}"


def test_existing_structural_paths_still_denied_unchanged(framework_repo_policy_yaml):
    """No loosening: dept.yaml, layers/**, tools/**, policies/**, missions/**
    stay denied for runtime_write_own on the framework repo too (they were
    already repo-agnostic structural, unaffected by #961's repo-aware wiring)."""
    from src.policy import Policy

    p = Policy.from_yaml(framework_repo_policy_yaml)
    for path in ["dept.yaml", "layers/1/PROMPT.md", "tools/t.py", "policies/p.yaml", "missions/m.yaml"]:
        allowed, reasons = p.enforce(
            actor="ops-loop-framework-test",
            repo="bubble-ops-loop",
            action="runtime_write_own",
            paths=[path],
        )
        assert not allowed, f"{path} must remain denied: {reasons}"
