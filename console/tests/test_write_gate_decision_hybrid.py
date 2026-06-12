"""test_write_gate_decision_hybrid.py — Hybrid local/VPS gate-approval write-flow.

When an operator approves a gate in the cockpit, write_gate_decision must land the
decision where the dept's loop reads it (inbox/decisions/<id>.yaml):

  - host=vps (default): the dept's repo is on the cockpit's disk → write to disk
    (existing behaviour, unchanged).
  - host=local (e.g. Miranda on Jade's Mac): the dept's repo is NOT on the
    cockpit's disk — it lives on the Mac + GitHub. The cockpit must commit the
    decision to the dept's GitHub repo via `gh api`, so the Mac loop pulls it.

These tests mock the `gh` subprocess + the dept-host lookup — no real GitHub call.
"""
from __future__ import annotations

import base64
import json
import subprocess

import pytest

from console.services import github_reader, dept_registry


def _fake_dept(slug, host):
    return dept_registry.DeptSummary(
        slug=slug, display_name=slug.capitalize(), status="Live",
        validated_steps=[], host=host,
    )


def test_vps_dept_writes_decision_to_disk(tmp_path, monkeypatch):
    """host=vps → existing disk write (no gh api)."""
    repo = tmp_path / "bubble-ops-ben"
    repo.mkdir()
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    monkeypatch.setattr("console.services.dept_registry.get_department", lambda slug: _fake_dept("ben", "vps"))
    called = {"gh": False}
    monkeypatch.setattr("console.services.github_reader.subprocess.run",
                        lambda *a, **k: called.__setitem__("gh", True))
    out = github_reader.write_gate_decision("ben", "gate-1", {"decision": "approve"})
    assert out is not None
    assert (repo / "inbox" / "decisions" / "gate-1.yaml").is_file()
    assert called["gh"] is False, "vps dept must NOT call gh api"


def test_local_dept_commits_decision_via_gh_api(tmp_path, monkeypatch):
    """host=local → commit the decision to the dept's GitHub repo via gh api PUT
    (the Mac loop pulls it). No disk write needed."""
    monkeypatch.setattr("console.services.dept_registry.get_department", lambda slug: _fake_dept("content", "local"))
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""
        return R()

    monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
    out = github_reader.write_gate_decision("content", "gate-7", {"decision": "approve"})

    assert out is not None, "local write must report success"
    cmd = captured["cmd"]
    # must be a gh api PUT to the dept repo's contents path for the decision file
    assert cmd[:3] == ["gh", "api", "-X"], cmd
    assert "PUT" in cmd
    joined = " ".join(cmd)
    assert "contents/inbox/decisions/gate-7.yaml" in joined
    assert "content" in joined  # the bubble-ops-content repo


def test_local_dept_gh_api_failure_returns_none(tmp_path, monkeypatch):
    """If the gh api commit fails, return None (don't pretend success)."""
    monkeypatch.setattr("console.services.dept_registry.get_department", lambda slug: _fake_dept("content", "local"))

    def fake_run(cmd, *a, **k):
        class R:
            returncode = 1
            stdout = ""
            stderr = "gh: not found"
        return R()

    monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
    out = github_reader.write_gate_decision("content", "gate-9", {"decision": "approve"})
    assert out is None
