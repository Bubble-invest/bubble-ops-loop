"""runtime_write_own MUST allow outputs/** for the fixture dept.

Per Notion v4 line 620:
  runtime_write_own → allowed_paths: outputs/**, queues/**, inbox/**
"""

from __future__ import annotations

import pytest

from src.guard import Guard
from src.policy_loader import load_policy


@pytest.mark.parametrize(
    "path",
    [
        "outputs/2026-05-20/1/summary.md",
        "outputs/2026-05-20/4/risk-kpis.yaml",
        "outputs/2026-05-20/4/risk-brief.md",
        "outputs/2026-05-20/management-export.yaml",
        "outputs/2026-05-20/1/artifacts/data.json",
        "outputs/2026-05-20/3/logs.jsonl",
    ],
)
def test_runtime_write_own_allows_outputs_subdir(fixture_policy_yaml, path):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, ok_paths, denied = g.check_paths(
        [path], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert allowed, f"expected ALLOW for {path}, got denied={denied}"
    assert path in ok_paths
    assert denied == []
