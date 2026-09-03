"""#961: scripts/lib/ (and scripts/ broadly) MUST be DENIED for a direct
push to the framework repo (bubble-ops-loop) via runtime_write_own.

Board incident: commit 5513824 pushed scripts/lib/budget.py straight to
bubble-ops-loop/main, bypassing review. STRUCTURAL_PATH_GLOBS already
protected dept.yaml/missions/tools/policies/layers everywhere; this file
pins the closed gap at the git-guard level (Guard.check_paths, the same
call the CLI's `bubble-git-guard push` makes) — mirrors
git-guard/tests/test_denied_paths_structural.py's shape/style exactly.
"""

from __future__ import annotations

import pytest
import yaml

from src.guard import Guard
from src.policy_loader import load_policy


@pytest.fixture
def framework_repo_policy_yaml(tmp_path):
    """Same shape as fixture_policy_yaml (conftest.py) but own_repo is the
    framework repo, with scripts/** allow-listed — the worst case: even a
    dept policy that (mis)declares scripts/** writable must still lose to
    the framework-only structural lock when repo == bubble-ops-loop."""
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
        "scripts/lib/budget.py",        # the exact #961 incident
        "scripts/lib/dispatch_helpers.py",
        "scripts/vendor-dept-libs.sh",  # scripts/ broadly, not just lib/
        "scripts/dispatch_directives.py",
    ],
)
def test_runtime_write_own_denies_scripts_on_framework_repo(framework_repo_policy_yaml, path):
    policy = load_policy(framework_repo_policy_yaml)
    g = Guard(policy=policy)
    allowed, ok_paths, denied = g.check_paths(
        [path], action="runtime_write_own", repo="bubble-ops-loop"
    )
    assert not allowed, f"expected DENY for {path} on bubble-ops-loop, got allowed=True"
    assert path not in ok_paths
    assert any(path in d for d in denied), f"denied reasons should mention path: {denied}"


def test_existing_structural_denials_unchanged(fixture_policy_yaml):
    """No regression: the pre-#961 structural set (dept.yaml, layers/, etc.)
    stays denied on the fixture (dept) repo exactly as before."""
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    for path in ["dept.yaml", "layers/1/PROMPT.md", "tools/t.py", "policies/p.yaml"]:
        allowed, _, denied = g.check_paths(
            [path], action="runtime_write_own", repo="bubble-ops-fixture"
        )
        assert not allowed, f"{path} must remain denied: {denied}"


def test_scripts_lib_on_dept_repo_not_flagged_structural(fixture_policy_yaml):
    """No loosening AND no over-reach: scripts/lib/ pushed to a dept repo is
    denied only because it's outside that dept's allowed_paths — never
    tagged 'structural' (the framework-only glob must not leak to dept
    repos, preserving the documented vendored-lib-sync workflow)."""
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, _, denied = g.check_paths(
        ["scripts/lib/dispatch_helpers.py"], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert not allowed
    assert all("structural" not in d for d in denied), denied
