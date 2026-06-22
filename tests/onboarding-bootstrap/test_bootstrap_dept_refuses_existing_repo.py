"""
Verify the bootstrap refuses (with a clear error message) when the target
GitHub repo already exists.
"""
from __future__ import annotations


def test_refuses_when_repo_already_exists(run_bootstrap, mock_gh_bin) -> None:
    res = run_bootstrap(
        slug="smoke-test",
        display_name="SmokeTest",
        owner="operator",
        extra_env={"FAKE_GH_REPO_EXISTS": "1"},
        expect_fail=True,
    )
    assert res.returncode != 0, "expected non-zero exit when repo exists"
    combined = (res.stdout + res.stderr).lower()
    assert "already exists" in combined, f"missing 'already exists' message: {combined!r}"
    # Should mention --force-recreate as the escape hatch.
    assert "--force-recreate" in (res.stdout + res.stderr), \
        "spec requires hinting at --force-recreate"
