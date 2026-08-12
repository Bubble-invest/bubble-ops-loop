"""tests/** policy-widening tests (#913, 2026-08-12, Joris-approved).

Prior to #913, landing a test file was blocked on BOTH gates at once:
  1. runtime_write_own denied it: tests/ was absent from every actor's
     allowed_paths (`path {p!r} not in allowed_paths {allowed_paths}`).
  2. settings_pr (propose-settings-pr) also denied it: tests/ correctly
     failed `_is_structural()` (`path {p!r} is not structural; use
     runtime_write_own instead`).

See #773 (original report), #891/#888 (blocked landings), #913 (the
Joris-approved fix). The fix:
  - `deploy/policies/*.template.yaml` (ops-leaf + management) and
    `fixture-policy.yaml` now include `tests/**` in `write.allowed_paths`.
  - `src/policy.py` adds `SETTINGS_PR_EXTRA_GLOBS = ("tests/**",)` and
    `_is_settings_pr_eligible()`, consulted by the settings_pr branch of
    `Policy.enforce()` instead of the narrower `_is_structural()`.

These tests pin BOTH routes now ALLOWING tests/**, while proving the
widening is scoped to tests/ exactly — a path that is neither in
allowed_paths nor settings-PR-eligible (e.g. a secret/config file outside
the allow-list) is still REFUSED on both routes.
"""

from __future__ import annotations

import pytest


TEST_PATHS = [
    "tests/test_new_feature.py",
    "tests/unit/test_widget.py",
    "tests/fixtures/data.json",
]

# A path that is neither structural, nor in allowed_paths, nor in
# SETTINGS_PR_EXTRA_GLOBS — must stay refused on BOTH routes. Deliberately
# secret/config-shaped (outside outputs/queues/inbox/tests) so the widening
# is proven NOT to have blinded the guard to arbitrary paths.
STILL_FORBIDDEN_PATH = "secrets/prod-broker.sops.env"


@pytest.mark.parametrize("path", TEST_PATHS)
def test_runtime_write_own_now_allows_tests_path(path, ops_policy_yaml):
    """Gate 1 (runtime_write_own / the push-time guard): tests/** is now in
    allowed_paths, so a direct commit of a test file is ALLOWED."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        paths=[path],
    )
    assert allowed, f"{path} should now be writable via runtime_write_own: {reasons}"


@pytest.mark.parametrize("path", TEST_PATHS)
def test_settings_pr_now_allows_tests_path(path, ops_policy_yaml):
    """Gate 2 (propose-settings-pr / the settings_pr action): tests/** is now
    settings-PR-eligible (SETTINGS_PR_EXTRA_GLOBS), so an agent MAY also
    propose a test file via a reviewed PR (e.g. before a dept's policy has
    been redeployed with the allowed_paths widening, or when human review is
    wanted)."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="settings_pr",
        paths=[path],
    )
    assert allowed, f"{path} should now be settings-PR-eligible: {reasons}"


def test_tests_path_is_not_reclassified_as_structural(ops_policy_yaml):
    """Scope check: tests/** must NOT have been folded into
    STRUCTURAL_PATH_GLOBS. If it had, runtime_write_own would deny it
    OUTRIGHT (the `_is_structural` early-continue in enforce()) regardless of
    allowed_paths, silently closing the very route #913 opened."""
    from src.policy import _is_structural

    assert _is_structural("tests/test_new_feature.py") is False


def test_runtime_write_own_still_rejects_forbidden_path(ops_policy_yaml):
    """The widening is scoped to tests/ exactly — a path outside every
    allowed_paths entry (and not structural) is STILL refused by
    runtime_write_own. Proves the guard was not blinded."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        paths=[STILL_FORBIDDEN_PATH],
    )
    assert not allowed, f"{STILL_FORBIDDEN_PATH} must stay refused: {reasons}"
    assert any("not in allowed_paths" in r for r in reasons), reasons


def test_settings_pr_still_rejects_forbidden_path(ops_policy_yaml):
    """Same forbidden path is ALSO still refused via settings_pr — it is
    neither structural nor in SETTINGS_PR_EXTRA_GLOBS."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="settings_pr",
        paths=[STILL_FORBIDDEN_PATH],
    )
    assert not allowed, f"{STILL_FORBIDDEN_PATH} must stay refused: {reasons}"
    assert any("not settings-PR-eligible" in r for r in reasons), reasons


def test_settings_pr_still_rejects_ordinary_runtime_path(ops_policy_yaml):
    """A plain runtime path that is neither structural nor tests/ (e.g.
    outputs/foo.md) must still be REFUSED via settings_pr — unchanged
    behavior, pinned so #913 didn't accidentally widen settings_pr beyond
    tests/."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="settings_pr",
        paths=["outputs/2026-08-12/1/summary.md"],
    )
    assert not allowed, f"non-structural, non-tests path must stay refused: {reasons}"
