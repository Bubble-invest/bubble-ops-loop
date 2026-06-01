"""
test_cancel_eclosion.py — Sprint Lifecycle Deliverable A.

Tests for the `cancel-eclosion` capability — used to abandon a department
that NEVER reached `Live`. Use case: testing eclosure repeatedly during
development, or Joris changes his mind mid-eclosure.

Flow (per the deliverable spec):
  1. Pre-flight: verify dept exists, status != "Live"
  2. Mock SSH to Morty: systemctl disable --now ops-loop-<slug>.service
     + remove the unit file
  3. Mock `gh repo archive vdk888/bubble-ops-<slug>`
  4. Update STATE.yaml: status -> "Cancelled" + cancelled_at
  5. Emit operator instructions for BotFather (cannot automate)

Mocking strategy: monkeypatch `subprocess.run` in the lib module so we
NEVER actually SSH / call gh / archive a repo.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

# Make scripts/lib importable.
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
SCRIPTS_LIB = PROJECT_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))


# ---------------------------------------------------------------------------
# Local helper to render a minimal dept repo with a given STATE.yaml status.
# ---------------------------------------------------------------------------

def _make_dept_repo(tmp_path: Path, slug: str, status: str,
                    display_name: str = "Smoke") -> Path:
    """Create the minimum tree cancel_eclosion needs: onboarding/STATE.yaml
    + dept.yaml.draft (or dept.yaml) so the pre-flight passes."""
    repo = tmp_path / f"bubble-ops-{slug}"
    (repo / "onboarding").mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "slug": slug,
        "display_name": display_name,
        "owner": "joris",
        "created_at": "2026-05-21T10:00:00Z",
        "status": status,
        "validated_steps": [],
        "last_updated_at": "2026-05-21T10:00:00Z",
        "commits": [],
    }
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(state, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    # Minimal dept.yaml.draft so dept_doc reads succeed.
    (repo / "dept.yaml.draft").write_text(
        yaml.safe_dump(
            {"department": {"slug": slug, "level": "ops",
                            "mandate": "Mandate de test pour cancel-eclosion."}},
            sort_keys=False, allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def patched_subprocess(monkeypatch):
    """Replace subprocess.run inside cancel_eclosion with a recording mock."""
    import cancel_eclosion  # type: ignore

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        # Record either argv list or stringly-passed command.
        if isinstance(cmd, (list, tuple)):
            calls.append(list(cmd))
        else:
            calls.append([str(cmd)])
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        mock.stderr = ""
        return mock

    monkeypatch.setattr(cancel_eclosion.subprocess, "run", _fake_run)
    return calls


# ---------------------------------------------------------------------------
# 8 tests.
# ---------------------------------------------------------------------------

def test_cancel_drafting_dept_succeeds(tmp_path, patched_subprocess):
    """A dept currently in Drafting status can be cancelled."""
    from cancel_eclosion import cancel_eclosion as cancel

    repo = _make_dept_repo(tmp_path, "smoke", status="Drafting")
    result = cancel(slug="smoke", repo_dir=repo)

    assert result["status"] == "cancelled", result
    assert result["reasons"] == [] or result["reasons"] is None or result["reasons"] == []


def test_cancel_ready_to_activate_dept_succeeds(tmp_path, patched_subprocess):
    """A Ready-to-activate dept (right before Live) can still be cancelled."""
    from cancel_eclosion import cancel_eclosion as cancel

    repo = _make_dept_repo(tmp_path, "smoke", status="Ready to activate")
    result = cancel(slug="smoke", repo_dir=repo)

    assert result["status"] == "cancelled", result


def test_cancel_live_dept_is_blocked(tmp_path, patched_subprocess):
    """A Live dept MUST NOT be cancellable — operator must use retire-dept."""
    from cancel_eclosion import cancel_eclosion as cancel

    repo = _make_dept_repo(tmp_path, "smoke", status="Live")
    result = cancel(slug="smoke", repo_dir=repo)

    assert result["status"] == "blocked", result
    joined = " ".join(result["reasons"])
    assert "retire-dept" in joined, (
        f"Expected error to point operator at retire-dept; got: {result['reasons']}"
    )


def test_cancel_nonexistent_dept_errors(tmp_path, patched_subprocess):
    """A repo that doesn't exist on disk yields a blocked + clear reason."""
    from cancel_eclosion import cancel_eclosion as cancel

    nonexistent = tmp_path / "bubble-ops-nothing"
    result = cancel(slug="nothing", repo_dir=nonexistent)

    assert result["status"] == "blocked", result
    joined = " ".join(result["reasons"]).lower()
    assert ("not found" in joined or "does not exist" in joined
            or "missing" in joined), result["reasons"]


def test_systemd_subprocess_called_with_disable_now(tmp_path, patched_subprocess):
    """The mocked SSH must invoke systemctl disable --now for the dept unit."""
    from cancel_eclosion import cancel_eclosion as cancel

    repo = _make_dept_repo(tmp_path, "smoke", status="Drafting")
    cancel(slug="smoke", repo_dir=repo)

    flat = [" ".join(c) for c in patched_subprocess]
    has_disable_now = any(
        "systemctl disable --now" in line and "ops-loop-smoke.service" in line
        for line in flat
    )
    assert has_disable_now, (
        f"Expected systemctl disable --now ops-loop-smoke.service call; "
        f"got calls: {flat}"
    )


def test_gh_archive_subprocess_called(tmp_path, patched_subprocess):
    """The mocked gh must be invoked with `repo archive vdk888/bubble-ops-<slug>`."""
    from cancel_eclosion import cancel_eclosion as cancel

    repo = _make_dept_repo(tmp_path, "smoke", status="Drafting")
    cancel(slug="smoke", repo_dir=repo)

    flat = [" ".join(c) for c in patched_subprocess]
    has_gh_archive = any(
        "gh" in line and "repo" in line and "archive" in line
        and "bubble-ops-smoke" in line
        for line in flat
    )
    assert has_gh_archive, (
        f"Expected gh repo archive vdk888/bubble-ops-smoke call; "
        f"got calls: {flat}"
    )


def test_state_yaml_status_becomes_cancelled(tmp_path, patched_subprocess):
    """After success, STATE.yaml::status must equal 'Cancelled' + cancelled_at present."""
    from cancel_eclosion import cancel_eclosion as cancel

    repo = _make_dept_repo(tmp_path, "smoke", status="Drafting")
    cancel(slug="smoke", repo_dir=repo)

    state = yaml.safe_load(
        (repo / "onboarding" / "STATE.yaml").read_text(encoding="utf-8")
    )
    assert state["status"] == "Cancelled", state
    assert "cancelled_at" in state, state
    assert state["cancelled_at"], state["cancelled_at"]


def test_operator_instructions_mention_botfather(tmp_path, patched_subprocess):
    """The operator instructions must spell out the BotFather flow (cannot automate)."""
    from cancel_eclosion import cancel_eclosion as cancel

    repo = _make_dept_repo(tmp_path, "smoke", status="Drafting")
    result = cancel(slug="smoke", repo_dir=repo)

    instructions = "\n".join(result["operator_instructions"])
    assert "BotFather" in instructions, instructions
    assert "/mybots" in instructions, instructions
    assert "Delete bot" in instructions, instructions


def test_dry_run_does_not_mutate_state(tmp_path, patched_subprocess):
    """--dry-run must compute the plan but NOT mutate STATE.yaml or call subprocess."""
    from cancel_eclosion import cancel_eclosion as cancel

    repo = _make_dept_repo(tmp_path, "smoke", status="Drafting")
    before = (repo / "onboarding" / "STATE.yaml").read_text(encoding="utf-8")

    result = cancel(slug="smoke", repo_dir=repo, dry_run=True)
    after = (repo / "onboarding" / "STATE.yaml").read_text(encoding="utf-8")

    assert result["status"] == "cancelled", result
    assert before == after, "dry-run mutated STATE.yaml"
    # And no subprocess calls in dry-run mode.
    assert patched_subprocess == [], patched_subprocess
