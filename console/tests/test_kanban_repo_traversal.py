r"""
test_kanban_repo_traversal.py — regression guard for PR #89 review finding.

`repo_path_for_org_repo(org_repo)` resolves an "org/repo" string to a local
checkout under disk_root(). The original regex `^[\w.-]+/[\w.-]+$` accepted
`foo/..` (because `[\w.-]+` matches `..`), which let the `repo` query param of
the /kanban/attachment route escape one level above disk_root. This test pins
the fix: `..` / `.` components are rejected, and any resolved candidate must
stay contained within disk_root().
"""
from __future__ import annotations

import pytest

from console.services.github_reader import repo_path_for_org_repo


@pytest.mark.parametrize("bad", [
    "foo/..",
    "../etc",
    "..",
    "../..",
    "foo/.",
    "./foo",
    "Bubble-invest/..",
    "Bubble-invest/../../etc",
    # malformed (no exactly-one-slash) — also rejected by the regex
    "foo",
    "foo/bar/baz",
    "/etc/passwd",
    "",
])
def test_repo_path_rejects_traversal_and_malformed(bad):
    assert repo_path_for_org_repo(bad) is None, (
        f"repo_path_for_org_repo({bad!r}) must return None (traversal/malformed)"
    )
