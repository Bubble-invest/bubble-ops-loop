"""Board #1550 — the relay's approved_by gate.

Covers:
  - the canonical DIRECTIVE_APPROVED_BY value is pinned (== "operator") and
    dispatch_directives.py references the SAME object, not a re-declared
    literal (board #1550 found the value hardcoded independently on both
    sides of this relay: bubble-ops-tony's directive_writer required a
    caller to pass "joris" while this relay only ever accepted "operator" —
    the two never matched, so nothing dispatched cleanly)
  - the improved SKIP log states both what the gate expected and what it
    actually saw (rule 4 of #1550: don't widen the gate, just make the
    refusal legible)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dispatch_directives as dd  # noqa: E402
from directive_constants import DIRECTIVE_APPROVED_BY  # noqa: E402


def test_directive_approved_by_pinned_value():
    assert DIRECTIVE_APPROVED_BY == "operator"


def test_dispatch_directives_uses_the_same_object():
    assert dd.DIRECTIVE_APPROVED_BY is DIRECTIVE_APPROVED_BY


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


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


def test_skip_log_states_expected_vs_seen(tmp_path, monkeypatch, capsys):
    root = tmp_path / "agents"
    root.mkdir()
    tony = _make_repo(root, "tony")
    _make_repo(root, "maya")
    (tony / dd._OUTBOUND_REL).mkdir(parents=True)
    monkeypatch.setenv("BUBBLE_BACKUP_LOCK_DIR", str(tmp_path / "lock"))

    import yaml
    bad = tony / dd._OUTBOUND_REL / "directive-dbad.yaml"
    bad.write_text(yaml.safe_dump({
        "directive_id": "dbad", "target_dept": "maya",
        "approved_by": "joris",  # board #1550's exact stuck-directive shape
        "status": "approved",
        "body": "x",
    }))

    rc = dd.dispatch(root, "tony", dry_run=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIP" in out
    assert f"expected approved_by={DIRECTIVE_APPROVED_BY!r}" in out
    assert "status='approved'" in out
    assert "saw approved_by='joris'" in out
    assert "status='approved'" in out
