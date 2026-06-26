"""
test_local_decision_delivery.py — host=local gate-decision delivery (#312).

The cockpit runs without ambient `gh auth`, so the host=local GitHub PUT must be
authenticated with a short-lived contents:write token (GH_TOKEN), or the decision
silently never reaches GitHub and the local agent (Miranda/content) never gets it.

Also: when the local dept's repo is ALSO mirrored on the cockpit disk (hybrid —
gates render from it), a successful GitHub commit must drop a local hide-marker so
the card disappears from the cockpit immediately instead of waiting for the agent
to round-trip `resolved:true`.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from console.services import github_reader, dept_registry


def _fake_dept(slug, host):
    return dept_registry.DeptSummary(
        slug=slug, display_name=slug.capitalize(), status="Live",
        validated_steps=[], host=host,
    )


# ── Token injection ─────────────────────────────────────────────────────────

class TestContentsTokenInjection:

    def test_put_runs_with_gh_token_from_file(self, tmp_path, monkeypatch):
        """The gh api PUT must run with GH_TOKEN set from the contents-token file."""
        monkeypatch.setattr("console.services.dept_registry.get_department",
                            lambda slug: _fake_dept("content", "local"))
        tokfile = tmp_path / "contents-token"
        tokfile.write_text("ghs_faketoken123")
        monkeypatch.setattr(github_reader, "_CONTENTS_TOKEN_FILE", str(tokfile))
        # No on-disk mirror in this test → marker no-ops.
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: None)

        captured = {}

        def fake_run(cmd, *a, **k):
            captured["env"] = k.get("env", {})
            class R:
                returncode = 0
                stdout = "{}"
                stderr = ""
            return R()

        monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
        out = github_reader.write_gate_decision("content", "g1", {"action": "approve"})
        assert out is not None
        assert captured["env"].get("GH_TOKEN") == "ghs_faketoken123", \
            "PUT must carry the contents token via GH_TOKEN"

    def test_env_token_fallback(self, tmp_path, monkeypatch):
        """When no token file exists, GH_TOKEN env is used (dev/CI)."""
        monkeypatch.setattr("console.services.dept_registry.get_department",
                            lambda slug: _fake_dept("content", "local"))
        monkeypatch.setattr(github_reader, "_CONTENTS_TOKEN_FILE", str(tmp_path / "nope"))
        monkeypatch.setenv("GH_TOKEN", "ghs_envtoken")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: None)

        captured = {}

        def fake_run(cmd, *a, **k):
            captured["env"] = k.get("env", {})
            class R:
                returncode = 0
                stdout = "{}"
                stderr = ""
            return R()

        monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
        github_reader.write_gate_decision("content", "g2", {"action": "approve"})
        assert captured["env"].get("GH_TOKEN") == "ghs_envtoken"


# ── Re-decision (422 → fetch sha → update) ──────────────────────────────────

def test_redecision_updates_existing_file_with_sha(tmp_path, monkeypatch):
    """A PUT that 422s because the file exists must fetch the blob sha and retry
    as an update (so changing your mind on a local dept actually lands)."""
    monkeypatch.setattr("console.services.dept_registry.get_department",
                        lambda slug: _fake_dept("content", "local"))
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: None)
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        class R: pass
        r = R()
        # 1st call: PUT without sha → 422
        if "-X" in cmd and "PUT" in cmd and not any("sha=" in c for c in cmd):
            r.returncode = 1
            r.stdout = ""
            r.stderr = '{"message":"Invalid request. \\"sha\\" wasn\'t supplied.","status":"422"}'
        # GET sha
        elif "--jq" in cmd:
            r.returncode = 0
            r.stdout = "abc123sha"
            r.stderr = ""
        # 2nd PUT with sha → success
        else:
            r.returncode = 0
            r.stdout = "{}"
            r.stderr = ""
        return r

    monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
    out = github_reader.write_gate_decision("content", "g-exists", {"action": "approve"})
    assert out is not None, "re-decision should succeed via sha-update"
    # the final PUT must have carried the fetched sha
    assert any("-X" in c and "PUT" in c and any("sha=abc123sha" in x for x in c)
               for c in calls), f"expected an update PUT with sha; calls={calls}"


# ── Local hide-marker ───────────────────────────────────────────────────────

class TestLocalHideMarker:

    def test_marker_written_when_repo_mirrored_on_disk(self, tmp_path, monkeypatch):
        """On a successful host=local commit, if the repo is mirrored on disk,
        a hide-marker decision file is written there so the card hides at once."""
        monkeypatch.setattr("console.services.dept_registry.get_department",
                            lambda slug: _fake_dept("content", "local"))
        repo = tmp_path / "bubble-ops-content"
        (repo / "queues" / "gates").mkdir(parents=True)
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)

        def fake_run(cmd, *a, **k):
            class R:
                returncode = 0
                stdout = "{}"
                stderr = ""
            return R()

        monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
        out = github_reader.write_gate_decision("content", "g-mark", {"action": "approve"})
        assert out is not None
        marker = repo / "inbox" / "decisions" / "g-mark.yaml"
        assert marker.is_file(), "hide-marker must be written to the on-disk mirror"
        assert yaml.safe_load(marker.read_text())["action"] == "approve"

    def test_no_marker_when_github_put_fails(self, tmp_path, monkeypatch):
        """If the GitHub commit fails, NO hide-marker is written (don't hide a
        card whose decision didn't actually get delivered)."""
        monkeypatch.setattr("console.services.dept_registry.get_department",
                            lambda slug: _fake_dept("content", "local"))
        repo = tmp_path / "bubble-ops-content"
        (repo / "queues" / "gates").mkdir(parents=True)
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)

        def fake_run(cmd, *a, **k):
            class R:
                returncode = 1
                stdout = ""
                stderr = "401 Unauthorized"
            return R()

        monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
        out = github_reader.write_gate_decision("content", "g-fail", {"action": "approve"})
        assert out is None
        assert not (repo / "inbox" / "decisions" / "g-fail.yaml").exists(), \
            "no hide-marker when the delivery failed"

    def test_marker_failure_does_not_break_success(self, tmp_path, monkeypatch):
        """A hide-marker write error must not turn a successful commit into a failure."""
        monkeypatch.setattr("console.services.dept_registry.get_department",
                            lambda slug: _fake_dept("content", "local"))
        # repo_path points at a path we then make unwritable by pointing at a file
        bad = tmp_path / "not-a-dir"
        bad.write_text("x")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: bad)

        def fake_run(cmd, *a, **k):
            class R:
                returncode = 0
                stdout = "{}"
                stderr = ""
            return R()

        monkeypatch.setattr("console.services.github_reader.subprocess.run", fake_run)
        out = github_reader.write_gate_decision("content", "g-robust", {"action": "approve"})
        assert out is not None, "commit success must survive a marker write error"
