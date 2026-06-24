"""
conftest.py — fixtures for the UX-2 onboarding bootstrap test suite.

Provides:
  - PROJECT_ROOT / SCRIPTS_DIR / SCHEMAS_DIR / SKILL_ROOT path constants
  - mock_gh_bin()        — drop a fake `gh` binary into PATH that records every
                           invocation to a JSON file. NEVER touches GitHub.
  - mock_git_remote_dir()— provides a bare git repo URL the bootstrap can push
                           to, so we can exercise the full clone/commit/push
                           cycle in pure local fs (no network).
  - tmp_clone_dir()      — a tmp dir suitable to host the local clone the
                           bootstrap creates.
  - stub_skill_lib()     — ensures the UX-1 skill_lib is importable (puts the
                           skill root on sys.path).
  - sample_state_yaml()  — a valid STATE.yaml dict for schema-validation tests.

CRITICAL: no test in this suite is allowed to perform a real `gh` API call or
push to a real remote. The mock_gh_bin fixture is mandatory for any test that
invokes the bash scripts; CI must fail if a real `gh` ever runs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent  # .../projects/bubble-ops-loop
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SCRIPTS_LIB_DIR = SCRIPTS_DIR / "lib"
SCHEMAS_DIR = PROJECT_ROOT / "schemas-draft"
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"

# Ensure the skill is importable for any test that needs render_template etc.
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))
# Ensure scripts/lib is importable for state_yaml and pr_body helpers.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def scripts_dir() -> Path:
    return SCRIPTS_DIR


@pytest.fixture
def schemas_dir() -> Path:
    return SCHEMAS_DIR


@pytest.fixture
def skill_root() -> Path:
    return SKILL_ROOT


def _git(cwd: Path, *args: str) -> str:
    """Run git, return stdout, raise on failure."""
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


@pytest.fixture
def mock_git_remote_dir(tmp_path: Path) -> Path:
    """
    Create a bare git repo to serve as 'origin' for bootstrap. The bootstrap
    script uses `gh` to create the repo on GitHub, but we mock that step and
    point the local clone at this bare repo instead so git push actually
    works locally.
    """
    bare = tmp_path / "remote" / "bubble-ops-smoke-test.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    return bare


@pytest.fixture
def mock_gh_bin(tmp_path: Path, mock_git_remote_dir: Path) -> dict:
    """
    Materialize a fake `gh` script at <tmp_path>/bin/gh that records each
    invocation to <tmp_path>/gh_calls.jsonl AND emits realistic stdout / exit
    codes so the bootstrap can proceed without touching GitHub.

    Returns:
        {
          "bin_dir": Path,                # prepend to PATH
          "calls_file": Path,             # JSONL of every invocation
          "remote_dir": Path,             # bare repo to push to
          "env_extras": dict[str, str],   # vars to inject into subprocess env
        }
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls_file = tmp_path / "gh_calls.jsonl"

    # The fake gh writes one JSON line per invocation, then handles the
    # specific subcommands the bootstrap uses: `repo view`, `repo create`,
    # `pr create`. Everything else exits 0 with no output.
    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        # Record this invocation.
        python3 - "$@" <<PYEOF
        import json, os, sys
        from pathlib import Path
        Path("{calls_file}").parent.mkdir(parents=True, exist_ok=True)
        entry = {{"argv": sys.argv[1:], "cwd": os.getcwd()}}
        with open("{calls_file}", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\\n")
        PYEOF

        sub="${{1:-}}"
        case "$sub" in
          repo)
            sub2="${{2:-}}"
            case "$sub2" in
              view)
                # Mock 'gh repo view' — controlled by FAKE_GH_REPO_EXISTS env var.
                if [[ "${{FAKE_GH_REPO_EXISTS:-0}}" == "1" ]]; then
                  echo "vdk888/bubble-ops-smoke-test"
                  exit 0
                else
                  echo "GraphQL: Could not resolve to a Repository (404)" >&2
                  exit 1
                fi
                ;;
              create)
                # Mock 'gh repo create' — succeed silently.
                echo "https://github.com/${{3:-vdk888/unknown}}"
                exit 0
                ;;
              *)
                exit 0
                ;;
            esac
            ;;
          pr)
            sub2="${{2:-}}"
            if [[ "$sub2" == "create" ]]; then
              # Emit a fake PR URL.
              echo "https://github.com/vdk888/bubble-ops-smoke-test/pull/1"
              exit 0
            fi
            exit 0
            ;;
          auth)
            # `gh auth status` — pretend authenticated.
            echo "Logged in as vdk888"
            exit 0
            ;;
          *)
            exit 0
            ;;
        esac
        """
    )
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(script, encoding="utf-8")
    fake_gh.chmod(0o755)

    return {
        "bin_dir": bin_dir,
        "calls_file": calls_file,
        "remote_dir": mock_git_remote_dir,
        "env_extras": {
            # Bootstrap reads this to know which URL to use for the local clone
            # (in real life it's https://github.com/vdk888/bubble-ops-<slug>.git).
            "FAKE_GH_REPO_URL": str(mock_git_remote_dir),
            "FAKE_GH_REPO_EXISTS": "0",
        },
    }


@pytest.fixture
def tmp_clone_dir(tmp_path: Path) -> Path:
    """A tmp dir suitable for `bootstrap-dept.sh --clone-dir=...`."""
    d = tmp_path / "clones"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def run_bootstrap(mock_gh_bin: dict, tmp_clone_dir: Path, tmp_path: Path) -> callable:
    """
    Callable that runs bootstrap-dept.sh with `gh` mocked + the local-clone
    target overridden. Returns the CompletedProcess.

    Each invocation with a distinct slug gets its own fresh bare git remote so
    that a second run_bootstrap(slug="other") called after bootstrapped_repo has
    already populated the shared mock remote doesn't encounter a pre-existing
    STATE.yaml in the clone.
    """

    def _run(slug: str, display_name: str, owner: str = "operator",
             extra_env: dict | None = None, expect_fail: bool = False):
        # Create a per-slug bare repo so different slugs never share state.
        per_slug_remote = tmp_path / f"remote-{slug}" / f"bubble-ops-{slug}.git"
        per_slug_remote.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", str(per_slug_remote)],
            check=True, capture_output=True,
        )

        env = os.environ.copy()
        env["PATH"] = f"{mock_gh_bin['bin_dir']}:{env['PATH']}"
        env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(tmp_clone_dir)
        env["FAKE_GH_REPO_URL"] = str(per_slug_remote)
        env["FAKE_GH_REPO_EXISTS"] = mock_gh_bin["env_extras"]["FAKE_GH_REPO_EXISTS"]
        if extra_env:
            env.update(extra_env)
        script = SCRIPTS_DIR / "bootstrap-dept.sh"
        res = subprocess.run(
            [
                "bash", str(script),
                f"--slug={slug}",
                f"--display-name={display_name}",
                f"--owner={owner}",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        if not expect_fail and res.returncode != 0:
            raise AssertionError(
                f"bootstrap-dept.sh failed (exit={res.returncode}):\n"
                f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
            )
        return res

    return _run


@pytest.fixture
def sample_state_yaml() -> dict:
    """A valid STATE.yaml dict for schema-validation tests."""
    return {
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-20T19:30:00Z",
        "status": "Configuring",
        "validated_steps": ["mandate"],
        "last_updated_at": "2026-05-20T19:35:00Z",
        "last_validated_by": "operator",
        "commits": [
            {
                "step": "mandate",
                "commit_sha": "abc1234",
                "validated_at": "2026-05-20T19:33:00Z",
            }
        ],
    }


@pytest.fixture
def bootstrapped_repo(run_bootstrap, tmp_clone_dir: Path):
    """Run bootstrap once, return the path to the local clone."""
    run_bootstrap(slug="smoke-test", display_name="SmokeTest", owner="operator")
    return tmp_clone_dir / "bubble-ops-smoke-test"
