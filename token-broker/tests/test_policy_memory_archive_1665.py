"""#1665: depts may push memory/archive/** (WORKING_MEMORY compaction archive), nothing broader."""
from pathlib import Path

import pytest

POL = Path(__file__).resolve().parents[1] / "deploy" / "policies"


@pytest.mark.parametrize("name,repo", [
    ("ops-leaf-policy.template.yaml", "bubble-ops-<DEPT_SLUG>"),
    ("management-policy.template.yaml", "bubble-ops-<DEPT_SLUG>"),
    ("fixture-policy.yaml", "bubble-ops-fixture"),
])
def test_memory_archive_allowed_other_memory_not(name, repo):
    from src.policy import Policy, _is_structural

    p = Policy.from_yaml(POL / name)
    assert not _is_structural("memory/archive/WORKING_MEMORY-2026-10.md")
    ok, why = p.enforce(p.actor, repo, "runtime_write_own",
                        ["memory/archive/WORKING_MEMORY-2026-10.md", "WORKING_MEMORY.md"])
    assert ok, why
    ok, _ = p.enforce(p.actor, repo, "runtime_write_own", ["memory/MEMORY.md"])
    assert not ok
