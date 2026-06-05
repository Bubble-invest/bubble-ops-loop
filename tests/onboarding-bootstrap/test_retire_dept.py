"""
test_retire_dept.py — Sprint Lifecycle Deliverable B.

Tests for the `retire-dept` capability — used to decommission a Live
department. Distinct from `cancel-eclosion` (which is for pre-Live).

Flow:
  1. Pre-flight: dept exists, STATE.yaml::status == "Live"
  2. Compose + send a final FR Bureau-de-Cadre Telegram message
  3. Mock SSH to Morty: systemctl disable (NO --now — let the loop
     finish gracefully)
  4. Update dept.yaml::department.status = "retired" (mock git
     commit + push)
  5. Update STATE.yaml: status -> "Retired" + retired_at + retired_reason

We mock subprocess.run (covers Telegram curl, ssh, git, gh) and never
hit any real service.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
SCRIPTS_LIB = PROJECT_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))


# ---------------------------------------------------------------------------
# Helper to materialize a Live dept repo.
# ---------------------------------------------------------------------------

def _make_live_dept_repo(tmp_path: Path, slug: str, display_name: str,
                         status: str = "Live") -> Path:
    """A repo with dept.yaml (live status) + STATE.yaml (status)."""
    repo = tmp_path / f"bubble-ops-{slug}"
    (repo / "onboarding").mkdir(parents=True, exist_ok=True)

    state = {
        "schema_version": 1,
        "slug": slug,
        "display_name": display_name,
        "owner": "joris",
        "created_at": "2026-05-21T10:00:00Z",
        "status": status,
        "validated_steps": ["mandate", "missions", "layers",
                            "skills_tools", "gates_kpis", "dry_run"],
        "last_updated_at": "2026-05-21T10:00:00Z",
        "commits": [],
    }
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(state, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    dept_doc = {
        "department": {
            "slug": slug,
            "level": "ops",
            "status": "live" if status == "Live" else "onboarding",
            "mandate": f"Mandate de test pour {display_name}.",
        },
        "layers": {"subscribed": [1]},
        "recurring_missions": [],
        "skills": {},
        "tools": [],
        "gate_policies": {},
        "hierarchy": {
            "level": "ops",
            "parent": "tony",
            "children": [],
            "visibility": {
                "read_outputs": [],
                "read_risk_kpis": False,
                "read_risk_briefs": False,
                "read_raw_artifacts": False,
                "read_secrets": False,
            },
            "directive_policy": {
                "can_open_priority_prs": False,
                "target_queue": None,
                "requires_human_gate_for": [],
            },
        },
        "optional_domain_ledger": None,
    }
    (repo / "dept.yaml").write_text(
        yaml.safe_dump(dept_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def patched_subprocess(monkeypatch):
    """Replace subprocess.run inside retire_dept with a recording mock."""
    import retire_dept  # type: ignore

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)):
            calls.append(list(cmd))
        else:
            calls.append([str(cmd)])
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        mock.stderr = ""
        return mock

    monkeypatch.setattr(retire_dept.subprocess, "run", _fake_run)
    return calls


# ---------------------------------------------------------------------------
# 9 tests.
# ---------------------------------------------------------------------------

def test_retire_live_dept_succeeds(tmp_path, patched_subprocess):
    """Happy path: Live dept can be retired."""
    from retire_dept import retire_dept as retire

    repo = _make_live_dept_repo(tmp_path, "miranda", "Miranda")
    result = retire(slug="miranda", repo_dir=repo)

    assert result["status"] == "retired", result
    assert result["reasons"] == [] or not result["reasons"], result


def test_retire_non_live_dept_is_blocked(tmp_path, patched_subprocess):
    """A dept in Drafting (or any non-Live status) MUST NOT be retirable."""
    from retire_dept import retire_dept as retire

    repo = _make_live_dept_repo(tmp_path, "smoke", "Smoke", status="Drafting")
    result = retire(slug="smoke", repo_dir=repo)

    assert result["status"] == "blocked", result
    joined = " ".join(result["reasons"])
    assert "cancel-eclosion" in joined, (
        f"Expected error pointing operator at cancel-eclosion; "
        f"got: {result['reasons']}"
    )
    assert "Live" in joined, result["reasons"]


def test_retire_nonexistent_dept_errors(tmp_path, patched_subprocess):
    """Repo not on disk -> blocked + clear reason."""
    from retire_dept import retire_dept as retire

    nonexistent = tmp_path / "bubble-ops-nothing"
    result = retire(slug="nothing", repo_dir=nonexistent)

    assert result["status"] == "blocked", result
    joined = " ".join(result["reasons"]).lower()
    assert ("not found" in joined or "does not exist" in joined
            or "missing" in joined), result["reasons"]


def test_final_telegram_message_uses_bureau_de_cadre_voice(tmp_path,
                                                           patched_subprocess):
    """Final Telegram message is FR, dignified, includes the display name."""
    from retire_dept import retire_dept as retire

    repo = _make_live_dept_repo(tmp_path, "miranda", "Miranda")
    result = retire(slug="miranda", repo_dir=repo)

    msg = result["final_telegram_msg"]
    # Display name interpolated
    assert "Miranda" in msg, msg
    # FR Bureau-de-Cadre cue words
    assert "Merci" in msg, msg
    assert "retraite" in msg.lower(), msg
    # No English / robotic phrasing
    assert "Decommissioned" not in msg
    assert "shutting down" not in msg.lower()


def test_dept_yaml_status_flipped_to_retired(tmp_path, patched_subprocess):
    """After retire(), dept.yaml::department.status == 'retired'."""
    from retire_dept import retire_dept as retire

    repo = _make_live_dept_repo(tmp_path, "miranda", "Miranda")
    retire(slug="miranda", repo_dir=repo)

    dept_doc = yaml.safe_load(
        (repo / "dept.yaml").read_text(encoding="utf-8")
    )
    assert dept_doc["department"]["status"] == "retired", dept_doc


def test_state_yaml_becomes_retired_with_reason(tmp_path, patched_subprocess):
    """STATE.yaml status, retired_at, retired_reason all written."""
    from retire_dept import retire_dept as retire

    repo = _make_live_dept_repo(tmp_path, "miranda", "Miranda")
    retire(slug="miranda", repo_dir=repo,
           reason="Maya v2 supersedes this dept")

    state = yaml.safe_load(
        (repo / "onboarding" / "STATE.yaml").read_text(encoding="utf-8")
    )
    assert state["status"] == "Retired", state
    assert "retired_at" in state and state["retired_at"], state
    assert state["retired_reason"] == "Maya v2 supersedes this dept", state


def test_systemd_called_without_now_flag(tmp_path, patched_subprocess):
    """retire-dept MUST use systemctl disable WITHOUT --now (graceful)."""
    from retire_dept import retire_dept as retire

    repo = _make_live_dept_repo(tmp_path, "miranda", "Miranda")
    retire(slug="miranda", repo_dir=repo)

    flat = [" ".join(c) for c in patched_subprocess]
    # Find the systemctl call.
    systemd_lines = [line for line in flat if "systemctl" in line]
    assert systemd_lines, f"No systemctl call recorded; got: {flat}"
    for line in systemd_lines:
        if "disable" in line:
            assert "--now" not in line, (
                f"retire-dept must use systemctl disable WITHOUT --now; "
                f"got: {line}"
            )


def test_default_reason_is_decommissioned(tmp_path, patched_subprocess):
    """If --reason not provided, STATE.yaml::retired_reason defaults to
    'Decommissioned'."""
    from retire_dept import retire_dept as retire

    repo = _make_live_dept_repo(tmp_path, "miranda", "Miranda")
    retire(slug="miranda", repo_dir=repo)

    state = yaml.safe_load(
        (repo / "onboarding" / "STATE.yaml").read_text(encoding="utf-8")
    )
    assert state["retired_reason"] == "Decommissioned", state


def test_dry_run_does_not_mutate(tmp_path, patched_subprocess):
    """--dry-run composes the plan + Telegram message but mutates nothing."""
    from retire_dept import retire_dept as retire

    repo = _make_live_dept_repo(tmp_path, "miranda", "Miranda")
    before_state = (repo / "onboarding" / "STATE.yaml").read_text(encoding="utf-8")
    before_dept = (repo / "dept.yaml").read_text(encoding="utf-8")

    result = retire(slug="miranda", repo_dir=repo, dry_run=True)

    assert result["status"] == "retired", result
    assert result["final_telegram_msg"], result
    after_state = (repo / "onboarding" / "STATE.yaml").read_text(encoding="utf-8")
    after_dept = (repo / "dept.yaml").read_text(encoding="utf-8")
    assert before_state == after_state, "dry-run mutated STATE.yaml"
    assert before_dept == after_dept, "dry-run mutated dept.yaml"
    assert patched_subprocess == [], "dry-run made subprocess calls"


def test_secrets_quarantined_on_retire(tmp_path, patched_subprocess):
    """Side effect 2b (2026-06-05 security): retirement MUST invoke the secret
    quarantine helper, which locks the bot, archives the SOPS env, and wipes
    runtime secrets. A retired dept must lose live ACCESS (history stays)."""
    from retire_dept import retire_dept as retire

    repo = _make_live_dept_repo(tmp_path, "miranda", "Miranda")
    retire(slug="miranda", repo_dir=repo)

    flat = [" ".join(c) for c in patched_subprocess]
    quarantine_lines = [ln for ln in flat if "retire-secrets-quarantine.sh" in ln]
    assert quarantine_lines, (
        "retire-dept MUST invoke retire-secrets-quarantine.sh to revoke the "
        f"retired dept's live secret access; recorded calls: {flat}"
    )
    # the dept slug must be passed to the helper
    assert any("miranda" in ln for ln in quarantine_lines), (
        f"quarantine helper must receive the dept slug; got: {quarantine_lines}"
    )


def test_quarantine_failure_does_not_block_retirement(tmp_path, monkeypatch):
    """If the quarantine helper fails (non-zero), retirement still completes —
    the dept is already disabled, so the security window is bounded and a hard
    block would leave it in a worse half-retired state. The WARN is logged."""
    import retire_dept

    def _fake_run(cmd, *a, **k):
        m = MagicMock()
        # quarantine call fails; everything else succeeds
        m.returncode = 1 if "retire-secrets-quarantine.sh" in " ".join(cmd) else 0
        m.stdout = ""
        m.stderr = "boom" if "retire-secrets-quarantine.sh" in " ".join(cmd) else ""
        return m

    monkeypatch.setattr(retire_dept.subprocess, "run", _fake_run)
    repo = _make_live_dept_repo(tmp_path, "miranda", "Miranda")
    result = retire_dept.retire_dept(slug="miranda", repo_dir=repo)
    assert result["status"] == "retired"  # not blocked
