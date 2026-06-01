"""
Verify validate-step.sh refuses to commit when the step artifact is invalid
(e.g. dept.yaml.draft with missing required field).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def test_rejects_invalid_dept_yaml_draft(scripts_dir: Path, bootstrapped_repo: Path) -> None:
    # Corrupt the draft: remove `mandate` (required field on department block).
    draft = bootstrapped_repo / "dept.yaml.draft"
    doc = yaml.safe_load(draft.read_text(encoding="utf-8"))
    del doc["department"]["mandate"]
    draft.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    env = os.environ.copy()
    res = subprocess.run(
        [
            "bash", str(scripts_dir / "validate-step.sh"),
            "--slug=smoke-test", "--step=mandate", f"--repo-dir={bootstrapped_repo}",
        ],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode != 0, (
        f"expected non-zero exit; got 0. stdout={res.stdout!r} stderr={res.stderr!r}"
    )
    combined = (res.stdout + res.stderr).lower()
    assert "validation" in combined or "invalid" in combined or "mandate" in combined, \
        f"expected a validation error message, got: {res.stdout + res.stderr!r}"

    # STATE.yaml should NOT have recorded mandate.
    state = yaml.safe_load((bootstrapped_repo / "onboarding" / "STATE.yaml").read_text())
    assert "mandate" not in state.get("validated_steps", []), \
        "STATE.yaml was updated despite invalid artifact"
