"""
Verify idempotency: running bootstrap twice with the same slug errors cleanly
(does not corrupt state). The second run is gated by the "already exists"
check (see test_bootstrap_dept_refuses_existing_repo); this test verifies the
local-clone-dir guard too.
"""
from __future__ import annotations

from pathlib import Path


def test_second_run_does_not_corrupt_first_clone(run_bootstrap, tmp_clone_dir: Path) -> None:
    run_bootstrap(slug="smoke-test", display_name="SmokeTest")
    clone = tmp_clone_dir / "bubble-ops-smoke-test"
    assert clone.exists()

    # Snapshot the dept.yaml.draft hash to ensure run 2 didn't mutate it.
    draft = (clone / "dept.yaml.draft").read_bytes()

    # Run 2 with the mock saying "repo exists already".
    res = run_bootstrap(
        slug="smoke-test",
        display_name="SmokeTest",
        extra_env={"FAKE_GH_REPO_EXISTS": "1"},
        expect_fail=True,
    )
    assert res.returncode != 0

    # The original clone must be untouched.
    after = (clone / "dept.yaml.draft").read_bytes()
    assert draft == after, "second run corrupted first clone"


def test_help_flag_works(scripts_dir) -> None:
    """Every script must support --help."""
    import subprocess

    for script in ("bootstrap-dept.sh", "validate-step.sh", "activate-dept.sh"):
        p = scripts_dir / script
        assert p.exists(), f"missing: {p}"
        res = subprocess.run(
            ["bash", str(p), "--help"], capture_output=True, text=True
        )
        assert res.returncode == 0, f"{script} --help exited {res.returncode}"
        combined = (res.stdout + res.stderr).lower()
        assert "usage" in combined or "synopsis" in combined, \
            f"{script} --help has no usage line"
