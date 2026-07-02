"""test_write_gate_decision_hybrid.py — Hybrid local/VPS gate-approval write-flow.

When an operator approves a gate in the cockpit, write_gate_decision must land the
decision where the dept's loop reads it (inbox/decisions/<id>.yaml):

  - host=vps (default): the dept's repo is on the cockpit's disk → write to disk
    (existing behaviour, unchanged).
  - host=local (e.g. Miranda on {{OPERATOR_2}}'s Mac): the dept's repo is NOT on the
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


# ---------------------------------------------------------------------------
# QA-requested extra coverage (B5): injection-safety, GITHUB_ORG resolution,
# re-approval of an existing path (422 → None).
# ---------------------------------------------------------------------------


def test_local_gate_id_special_chars_stays_a_discrete_argv_element(tmp_path, monkeypatch):
    """A gate_id with shell-significant / unicode characters must travel as a
    DISCRETE argv element to gh — never interpolated into a shell string — so it
    can't break out of the command (no shell=True, no injection). We assert the
    crafted gate_id appears verbatim inside exactly one argv element (the contents
    API path) and that no element is a shell metacharacter expansion."""
    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("content", "local"))
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        # The cockpit must NEVER hand gh a shell string; cmd must be a list (argv)
        # and the call must not request shell interpretation.
        assert isinstance(cmd, list), "command must be an argv list, not a shell string"
        assert k.get("shell", False) is False, "must not run with shell=True"
        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""
        return R()

    monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
    # Colon + spaces + a backtick + a unicode char + a would-be command separator.
    nasty = "evil:id `whoami`; rm -rf / é"
    out = github_reader.write_gate_decision("content", nasty, {"decision": "approve"})

    assert out is not None
    cmd = captured["cmd"]
    # The path argument is the gh contents path; the raw gate_id must live inside
    # exactly that ONE element, intact, and not be split across argv elements.
    path_args = [c for c in cmd if "contents/inbox/decisions/" in c]
    assert len(path_args) == 1, f"gate_id path must be a single argv element: {cmd}"
    assert nasty in path_args[0], "gate_id must be passed verbatim, not shell-mangled"
    # And no argv element is the dangerous fragment on its own (i.e. the shell
    # never had a chance to split it) — the whole nasty string is contained.
    assert "whoami" not in [c.strip() for c in cmd if c.strip() != path_args[0]]


def test_local_github_org_resolution_uses_settings_value(tmp_path, monkeypatch):
    """The gh api contents path must be built from settings.GITHUB_ORG, so a
    custom org (e.g. a client's Cabinet org) is honoured — not a hardcoded one."""
    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("content", "local"))
    # Patch GITHUB_ORG on the EXACT settings module object that the already-
    # imported github_reader references (github_reader.settings), not via a
    # dotted-string path. The `app`/`client` fixtures purge + re-import all
    # console.* modules between tests, so a string path can resolve to a fresh
    # module object that github_reader doesn't actually use at call time — this
    # binds the patch to the live object, making the test isolation-proof.
    monkeypatch.setattr(github_reader.settings, "GITHUB_ORG", "Bubble-invest-custom")
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""
        return R()

    monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
    out = github_reader.write_gate_decision("content", "gate-org", {"decision": "approve"})

    assert out is not None
    joined = " ".join(captured["cmd"])
    assert "repos/Bubble-invest-custom/bubble-ops-content/contents/" in joined, joined
    # the default org must NOT leak through when overridden
    assert "repos/vdk888/" not in joined


def test_local_reapproval_existing_path_422_returns_none(tmp_path, monkeypatch):
    """Re-approving a gate whose decision file already exists: a PUT without the
    blob sha returns HTTP 422 (gh exits non-zero). write_gate_decision must
    return None — neither fake a success nor crash."""
    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("content", "local"))

    def fake_run(cmd, *a, **k):
        class R:
            returncode = 1  # gh api surfaces a 422 as a non-zero exit
            stdout = ""
            stderr = ('{"message":"Invalid request.\\n\\n'
                      '\\"sha\\" wasn\'t supplied.","status":"422"}')
        return R()

    monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
    out = github_reader.write_gate_decision("content", "gate-existing", {"decision": "approve"})
    assert out is None, "a 422 (file exists, no sha) must return None, not a fake success"


# ─── Atomic writes (board #450) ────────────────────────────────────────
# write_gate_decision (host=vps, disk write) must never leave a reader able
# to observe a partially-written decision file — temp file in the same dir +
# os.replace, not a direct truncating write_text.

def test_vps_write_is_atomic_no_partial_file_visible(tmp_path, monkeypatch):
    """A reader polling the decisions dir mid-write must see EITHER nothing
    yet, or the complete final file — never a truncated/partial one."""
    repo = tmp_path / "bubble-ops-ben"
    repo.mkdir()
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("ben", "vps"))

    decisions_dir = repo / "inbox" / "decisions"
    out = github_reader.write_gate_decision("ben", "gate-atomic", {"decision": "approve", "comment": "ok"})

    assert out is not None
    # No leftover temp file from the write (mkstemp prefix used by _atomic_write_text).
    leftovers = list(decisions_dir.glob(".gate-atomic.yaml.*.tmp"))
    assert leftovers == [], f"atomic write left a temp file behind: {leftovers}"
    # The final file is complete/parseable.
    import yaml as _yaml
    doc = _yaml.safe_load(out.read_text(encoding="utf-8"))
    assert doc == {"decision": "approve", "comment": "ok"}


def test_vps_write_uses_os_replace_not_direct_write(tmp_path, monkeypatch):
    """Verify the atomic-write mechanism itself: os.replace is invoked (temp
    file swapped into place), rather than a direct Path.write_text on the
    target — the change this card requires."""
    repo = tmp_path / "bubble-ops-ben"
    repo.mkdir()
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("ben", "vps"))

    calls = []
    real_replace = github_reader.os.replace

    def spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(github_reader.os, "replace", spy_replace)
    out = github_reader.write_gate_decision("ben", "gate-spy", {"decision": "reject"})

    assert out is not None
    assert len(calls) == 1, f"expected exactly one os.replace call, got {calls}"
    src, dst = calls[0]
    assert str(dst) == str(out)
    assert src != str(out)  # temp file path must differ from the final path


# ─── _gh_contents_sha: bare except:pass → logged (board #450) ─────────────

def test_gh_contents_sha_logs_warning_on_exception(monkeypatch, caplog):
    """_gh_contents_sha used to bare `except: pass` on any failure — now logs
    at warning while still returning None (caller behavior unchanged)."""
    import logging

    def fake_run(*a, **k):
        raise OSError("gh binary not found")

    monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)

    with caplog.at_level(logging.WARNING, logger="console.github_reader"):
        result = github_reader._gh_contents_sha("repos/org/repo/contents/x.yaml", {})

    assert result is None
    assert any(rec.levelno == logging.WARNING for rec in caplog.records), (
        f"expected a WARNING log, got: {[r.message for r in caplog.records]}"
    )


def test_gh_contents_sha_logs_warning_on_nonzero_exit(monkeypatch, caplog):
    """A non-zero gh exit (not an exception) must also be logged."""
    import logging

    def fake_run(*a, **k):
        class R:
            returncode = 1
            stdout = ""
            stderr = "404 Not Found"
        return R()

    monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)

    with caplog.at_level(logging.WARNING):
        result = github_reader._gh_contents_sha("repos/org/repo/contents/x.yaml", {})

    assert result is None
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)
