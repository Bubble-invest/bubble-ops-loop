"""test_write_gate_decision_uid_isolation.py — #1451.

write_gate_decision's host=vps branch used to always write the operator's
decision to `repo_path(slug)/inbox/decisions/<gate_id>.yaml`. For a
uid-isolated dept (post-#1120, e.g. Maya) that path is the FROZEN legacy
mirror (e.g. /home/claude/agents/bubble-ops-maya) — the dept's live tree
moved to `runtime_repo_path(slug)` (/srv/agents/<slug>, owned by
agent-<slug>), which the console user cannot write into (EACCES) and which
never reads the legacy mirror again. Decisions written there were silently
lost.

The fix routes a uid-isolated vps dept through the SAME GitHub-commit path
`_write_gate_decision_github` that host=local depts already use — the dept's
own `safe_pull` then brings the decision into its live tree.

These tests only exercise the dispatch logic in `write_gate_decision`:
  (a) a uid-isolated vps dept (runtime_repo_path != repo_path) routes to
      `_write_gate_decision_github` and does NOT write to the disk mirror.
  (b) a non-isolated vps dept (runtime_repo_path == repo_path, or
      runtime_repo_path unresolved) still writes to disk, unchanged.
  (c) host=local behaviour is untouched by the new branch.
"""
from __future__ import annotations

import pytest

from console.services import github_reader, dept_registry


def _fake_dept(slug, host):
    return dept_registry.DeptSummary(
        slug=slug, display_name=slug.capitalize(), status="Live",
        validated_steps=[], host=host,
    )


def test_uid_isolated_vps_dept_routes_to_github(tmp_path, monkeypatch):
    """runtime_repo_path(slug) != repo_path(slug) (uid-isolated, e.g. Maya
    post-#1120) → must call _write_gate_decision_github and must NOT write
    the decision to the (orphaned) disk mirror."""
    legacy_mirror = tmp_path / "bubble-ops-maya"
    legacy_mirror.mkdir()
    live_tree = tmp_path / "srv-agents-maya"
    live_tree.mkdir()

    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("maya", "vps"))
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: legacy_mirror)
    monkeypatch.setattr(github_reader, "runtime_repo_path", lambda slug: live_tree)

    called = {}

    def fake_github_write(slug, gate_id, decision):
        called["args"] = (slug, gate_id, decision)
        return __import__("pathlib").Path(f"inbox/decisions/{gate_id}.yaml")

    monkeypatch.setattr(github_reader, "_write_gate_decision_github", fake_github_write)

    out = github_reader.write_gate_decision("maya", "gate-42", {"decision": "approve"})

    assert out is not None
    assert called["args"] == ("maya", "gate-42", {"decision": "approve"})
    # The legacy mirror must NOT receive the decision file.
    assert not (legacy_mirror / "inbox" / "decisions" / "gate-42.yaml").exists()
    # Nor must anything have been written into the live tree directly — the
    # console process has no write access there; delivery is via GitHub only.
    assert not (live_tree / "inbox" / "decisions" / "gate-42.yaml").exists()


def test_non_isolated_vps_dept_still_writes_disk(tmp_path, monkeypatch):
    """runtime_repo_path(slug) == repo_path(slug) (pre-isolation vps dept, or
    a dept never uid-isolated) → unchanged disk write; GitHub path NOT
    called."""
    repo = tmp_path / "bubble-ops-ben"
    repo.mkdir()

    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("ben", "vps"))
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    monkeypatch.setattr(github_reader, "runtime_repo_path", lambda slug: repo)

    called = {"github": False}
    monkeypatch.setattr(github_reader, "_write_gate_decision_github",
                        lambda *a, **k: called.__setitem__("github", True))

    out = github_reader.write_gate_decision("ben", "gate-1", {"decision": "approve"})

    assert out is not None
    assert (repo / "inbox" / "decisions" / "gate-1.yaml").is_file()
    assert called["github"] is False, "non-isolated vps dept must NOT route to GitHub"


def test_non_isolated_vps_dept_with_unresolved_runtime_path_still_writes_disk(tmp_path, monkeypatch):
    """runtime_repo_path(slug) resolves to None (e.g. dept_registry can't
    find it anywhere) → falls back to the disk write, same as today."""
    repo = tmp_path / "bubble-ops-tony"
    repo.mkdir()

    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("tony", "vps"))
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    monkeypatch.setattr(github_reader, "runtime_repo_path", lambda slug: None)

    called = {"github": False}
    monkeypatch.setattr(github_reader, "_write_gate_decision_github",
                        lambda *a, **k: called.__setitem__("github", True))

    out = github_reader.write_gate_decision("tony", "gate-2", {"decision": "reject"})

    assert out is not None
    assert (repo / "inbox" / "decisions" / "gate-2.yaml").is_file()
    assert called["github"] is False


def test_host_local_unaffected_by_isolation_branch(tmp_path, monkeypatch):
    """host=local must keep going through _write_gate_decision_github exactly
    as before, regardless of what repo_path/runtime_repo_path resolve to —
    the new uid-isolation branch lives strictly inside the host=vps path and
    must never be reached for host=local."""
    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("content", "local"))
    # Even if these two happened to differ (they shouldn't matter for local),
    # the host=="local" branch must return before either is consulted.
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: tmp_path / "a")
    monkeypatch.setattr(github_reader, "runtime_repo_path", lambda slug: tmp_path / "b")

    calls = {"github": 0}

    def fake_github_write(slug, gate_id, decision):
        calls["github"] += 1
        return __import__("pathlib").Path(f"inbox/decisions/{gate_id}.yaml")

    monkeypatch.setattr(github_reader, "_write_gate_decision_github", fake_github_write)
    # Local hide-marker writes best-effort into repo_path(slug); make it a
    # real (but harmless) dir so that inner logic doesn't need its own mocks.
    (tmp_path / "a").mkdir()

    out = github_reader.write_gate_decision("content", "gate-99", {"decision": "approve"})

    assert out is not None
    assert calls["github"] == 1
