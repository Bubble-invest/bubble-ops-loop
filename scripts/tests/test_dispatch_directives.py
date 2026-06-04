"""Tests for dispatch_directives — gate, idempotency, isolation. No network."""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dispatch_directives as dd  # noqa: E402


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(root: Path, slug: str) -> Path:
    repo = root / f"bubble-ops-{slug}"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


@pytest.fixture()
def world(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    tony = _make_repo(root, "tony")
    maya = _make_repo(root, "maya")
    (tony / dd._OUTBOUND_REL).mkdir(parents=True)
    # Stub the push so no network/token is needed: just clear the worktree state.
    pushes = []

    def fake_push(repo_dir, repo_name, message, dry_run):
        pushes.append((repo_name, message))
        if dry_run:
            return True, "[dry-run]"
        _git(repo_dir, "add", "-A")
        r = dd._run(["git", "-C", str(repo_dir), "commit", "-m", message])
        return True, "pushed(stub)"

    monkeypatch.setattr(dd, "_push_repo", fake_push)
    return root, tony, maya, pushes


def _drop(tony, did, **fields):
    p = tony / dd._OUTBOUND_REL / f"directive-{did}.yaml"
    base = {"directive_id": did, "target_dept": "maya",
            "approved_by": "joris", "status": "approved",
            "body": "Prioritise the Charlie-Finance segment this week."}
    base.update(fields)
    p.write_text(yaml.safe_dump(base))
    return p


def test_approved_directive_is_delivered(world):
    root, tony, maya, pushes = world
    _drop(tony, "d1")
    rc = dd.dispatch(root, "tony", dry_run=False)
    assert rc == 0
    dest = maya / dd._INBOX_REL / "directive-d1.yaml"
    assert dest.exists(), "directive should land in maya inbox"
    payload = yaml.safe_load(dest.read_text())
    assert payload["body"].startswith("Prioritise")
    assert payload["from"] == "tony"
    assert "status" not in payload  # bookkeeping stripped from delivered copy
    # source marked dispatched
    src = yaml.safe_load((tony / dd._OUTBOUND_REL / "directive-d1.yaml").read_text())
    assert src["status"] == "dispatched"


def test_unapproved_is_NOT_delivered(world):
    root, tony, maya, pushes = world
    _drop(tony, "d2", approved_by="tony")          # not joris
    _drop(tony, "d3", status="draft")              # not approved
    rc = dd.dispatch(root, "tony", dry_run=False)
    assert rc == 0
    assert not (maya / dd._INBOX_REL / "directive-d2.yaml").exists()
    assert not (maya / dd._INBOX_REL / "directive-d3.yaml").exists()


def test_idempotent_already_dispatched(world):
    root, tony, maya, pushes = world
    _drop(tony, "d4", status="dispatched")
    dd.dispatch(root, "tony", dry_run=False)
    assert not (maya / dd._INBOX_REL / "directive-d4.yaml").exists()


def test_rerun_is_noop_after_delivery(world):
    root, tony, maya, pushes = world
    _drop(tony, "d5")
    dd.dispatch(root, "tony", dry_run=False)
    n_after_first = len(pushes)
    dd.dispatch(root, "tony", dry_run=False)   # second run
    # source is now 'dispatched' → gate short-circuits, no new child push
    assert len(pushes) == n_after_first, "re-run must not re-deliver"


def test_missing_target_repo_is_failed_not_fatal(world):
    root, tony, maya, pushes = world
    _drop(tony, "d6", target_dept="ghost")
    rc = dd.dispatch(root, "tony", dry_run=False)
    assert rc == 0  # non-fatal


def test_dry_run_writes_nothing(world):
    root, tony, maya, pushes = world
    _drop(tony, "d7")
    dd.dispatch(root, "tony", dry_run=True)
    assert not (maya / dd._INBOX_REL / "directive-d7.yaml").exists()
    src = yaml.safe_load((tony / dd._OUTBOUND_REL / "directive-d7.yaml").read_text())
    assert src["status"] == "approved"  # unchanged
