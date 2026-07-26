"""runtime_write_own to a SECOND repo the dept owns (#benvault).

Regression: when a dept's data/vault lives in a separate repo it owns (e.g.
ben -> bubble-ben-vault, split out 2026-06-03), runtime_write_own must permit a
push to that repo IF the policy declares an explicit write-rule for it. The
path allow-list still constrains WHAT may be written; this only governs WHICH
repos are eligible. Prior code hard-required repo == own_repo and silently
denied every vault push.

The security boundary is unchanged: a repo with NO write-rule is still denied.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def two_repo_policy_yaml(tmp_path):
    """A dept that owns its code repo AND a second data/vault repo."""
    import yaml

    data = {
        "github_access": {
            "actor": "ops-loop-ben",
            "own_repo": "bubble-ops-ben",
            "read": ["bubble-ops-ben", "bubble-ben-vault", "bubble-shared-wiki"],
            "write": [
                {
                    "repo": "bubble-ops-ben",
                    "allowed_paths": ["outputs/**", "queues/**"],
                    "mode": "direct_runtime_commit",
                },
                {
                    "repo": "bubble-ben-vault",
                    "allowed_paths": [
                        "value-chains/**",
                        "themes/**",
                        "investment-cases/**",
                        "clusters/**",
                        "positions/**",
                    ],
                    "mode": "direct_runtime_commit",
                },
            ],
            "pull_requests": {"can_open_to": []},
        }
    }
    p = tmp_path / "ben-policy.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_second_owned_repo_write_allowed(two_repo_policy_yaml):
    """A path within the second repo's declared rules is permitted."""
    from src.policy import Policy

    p = Policy.from_yaml(two_repo_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-ben",
        repo="bubble-ben-vault",
        action="runtime_write_own",
        paths=["value-chains/industrials.md"],
    )
    assert allowed, f"vault write should be allowed: {reasons}"


def test_own_repo_still_allowed(two_repo_policy_yaml):
    """The primary own_repo path still works (no regression)."""
    from src.policy import Policy

    p = Policy.from_yaml(two_repo_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-ben",
        repo="bubble-ops-ben",
        action="runtime_write_own",
        paths=["outputs/2026-07-27/1/summary.md"],
    )
    assert allowed, f"own_repo write should still be allowed: {reasons}"


def test_second_repo_path_outside_rules_denied(two_repo_policy_yaml):
    """A path NOT in the second repo's allowed_paths is still denied."""
    from src.policy import Policy

    p = Policy.from_yaml(two_repo_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-ben",
        repo="bubble-ben-vault",
        action="runtime_write_own",
        paths=["secrets/leak.txt"],
    )
    assert not allowed
    assert any("not in allowed_paths" in r for r in reasons), reasons


def test_unlisted_repo_still_denied(two_repo_policy_yaml):
    """SECURITY BOUNDARY: a repo with no write-rule at all is still denied —
    the fix only permits repos the policy explicitly grants."""
    from src.policy import Policy

    p = Policy.from_yaml(two_repo_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-ben",
        repo="bubble-ops-maya",  # a sibling dept's repo — must never be writable
        action="runtime_write_own",
        paths=["outputs/x.md"],
    )
    assert not allowed
    assert any("no write rules" in r or "not the actor's own_repo" in r for r in reasons), reasons


# --- console wildcard write-rule (#benvault side-effect, reviewer-flagged) -----
# Console's policy has own_repo=None + a wildcard write-rule repo: "bubble-ops-*"
# (a documented cross-dept power — see console-policy.template.yaml). Under the OLD
# code, `repo != own_repo` (None) was always true, so console's runtime_write_own
# was silently ALWAYS denied — a dead capability. This fix makes it work as the
# template always documented. These tests pin the now-reachable behavior + its
# bounds so the reactivation is intentional-and-covered, not silent. (No live
# console-policy.yaml is deployed in /opt today, so this is template-only until
# console is deployed.)

def test_console_wildcard_write_reachable_in_scope(console_policy_yaml):
    """Console CAN runtime_write_own a decision/gate path in any bubble-ops-* repo
    (its documented cross-dept power) — newly reachable, correctly scoped."""
    from src.policy import Policy

    p = Policy.from_yaml(console_policy_yaml)
    allowed, reasons = p.enforce(
        actor="bubble-ops-console",
        repo="bubble-ops-ben",
        action="runtime_write_own",
        paths=["inbox/decisions/dec-042.yaml"],
    )
    assert allowed, f"console decision-path write should be allowed: {reasons}"


def test_console_wildcard_write_out_of_scope_path_denied(console_policy_yaml):
    """Console's power is bounded to inbox/decisions + queues/gates — an
    out-of-scope path is still denied even in a matched repo."""
    from src.policy import Policy

    p = Policy.from_yaml(console_policy_yaml)
    allowed, reasons = p.enforce(
        actor="bubble-ops-console",
        repo="bubble-ops-ben",
        action="runtime_write_own",
        paths=["tools/evil.py"],
    )
    assert not allowed
    assert any("not in allowed_paths" in r or "structural" in r for r in reasons), reasons


def test_console_wildcard_does_not_match_non_bubble_ops_repo(console_policy_yaml):
    """The wildcard is literally bubble-ops-* — a repo outside that prefix
    (e.g. the vault repo) is NOT matched by console's rule and is denied."""
    from src.policy import Policy

    p = Policy.from_yaml(console_policy_yaml)
    allowed, reasons = p.enforce(
        actor="bubble-ops-console",
        repo="bubble-ben-vault",   # does NOT start with bubble-ops-
        action="runtime_write_own",
        paths=["inbox/decisions/x.yaml"],
    )
    assert not allowed, f"non-bubble-ops-* repo must not match the wildcard: {reasons}"
