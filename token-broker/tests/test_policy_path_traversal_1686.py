"""#1686: enforce() rejects non-canonical paths before glob matching."""

from __future__ import annotations

import pytest

BAD = [
    "outputs/../MANDATE.md",
    "memory/archive/../../MANDATE.md",
    "outputs/./x.md",
    "./outputs/x.md",
    "../outputs/x.md",
    "/outputs/x.md",
    "/etc/passwd",
    "outputs//x.md",
    "outputs/",
    "outputs\\x.md",
    "outputs\\..\\MANDATE.md",
    "outputs/x\x00.md",
    "outputs/..",
    "",
]


def _enforce(policy_yaml, action, paths):
    from src.policy import Policy

    return Policy.from_yaml(policy_yaml).enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action=action,
        paths=paths,
    )


@pytest.mark.parametrize("path", BAD)
@pytest.mark.parametrize("action", ["runtime_write_own", "settings_pr"])
def test_malformed_paths_denied(ops_policy_yaml, action, path):
    allowed, reasons = _enforce(ops_policy_yaml, action, [path])
    assert not allowed, reasons


def test_traversal_denied_among_legit_paths(ops_policy_yaml):
    allowed, _ = _enforce(
        ops_policy_yaml,
        "runtime_write_own",
        ["outputs/ok.md", "outputs/../MANDATE.md"],
    )
    assert not allowed


def test_traversal_denied_for_priority_pr(ops_policy_yaml):
    allowed, _ = _enforce(
        ops_policy_yaml,
        "open_priority_pr",
        ["queues/management/../../MANDATE.md"],
    )
    assert not allowed


@pytest.mark.parametrize("path", ["OUTPUTS/x.md", "Outputs/x.md", "mandate.md", "MANDATE.MD"])
def test_case_variants_still_denied(ops_policy_yaml, path):
    allowed, _ = _enforce(ops_policy_yaml, "runtime_write_own", [path])
    assert not allowed


@pytest.mark.parametrize(
    "path",
    [
        "outputs/2026-05-20/1/summary.md",
        "outputs/2026-05-20/4/risk-kpis.yaml",
        "queues/research/task-001.yaml",
        "queues/gates/gate-007.yaml",
        "inbox/decisions/dec-001.yaml",
        "outputs/file.with.dots.md",
        "outputs/.hidden/x.md",
        "outputs/a..b.md",
    ],
)
def test_legit_paths_unchanged(ops_policy_yaml, path):
    allowed, reasons = _enforce(ops_policy_yaml, "runtime_write_own", [path])
    assert allowed, reasons
