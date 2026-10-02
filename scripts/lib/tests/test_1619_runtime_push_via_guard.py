"""#1619 step 2: dept runtime pushes go through the guard + broker.

force_commit_and_push must (a) hand the push to `bubble-git-guard push` WITHOUT
any `--broker` override (the guard's own default resolver then selects the sudo
mint shim for `agent-<slug>` users) and WITHOUT touching the credential
helper, and (b) leave the documented fallback (no guard / no policy, e.g.
agent-morty) on the credential helper.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import scripts.lib.dispatch_helpers as dh


def _arm(monkeypatch, slug="tony", policy=True):
    monkeypatch.setattr(dh, "resolve_push_target",
                        lambda repo_dir: (slug, f"bubble-ops-{slug}"))
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/local/bin/bubble-git-guard" if policy else None)
    real_exists = Path.exists

    def fake_exists(self):
        if str(self).endswith(f"{slug}-policy.yaml"):
            return policy
        if str(self) == "/usr/local/bin/bubble-git-guard":
            return policy
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)


def _recorder(monkeypatch, push_rc=0, helper_token="ghs_readonly"):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        if cmd[:2] == ["git", "-C"] and "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(cmd, 0, " M outputs/2026-10-02/x.md\n", "")
        if cmd[:2] == ["git", "-C"] and "remote" in cmd and "get-url" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/Bubble-invest/bubble-ops-tony.git\n", "")
        if "bubble-gh-credential-helper.sh" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, f"username=x\npassword={helper_token}\n", "")
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, push_rc, "", "403" if push_rc else "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_guarded_push_has_no_broker_override_and_skips_the_helper(tmp_path, monkeypatch):
    _arm(monkeypatch)
    calls = _recorder(monkeypatch)
    ok, err = dh.force_commit_and_push(repo_dir=tmp_path, message="L4")
    assert ok is True and err is None
    guard_calls = [c for c, _ in calls if c and c[0].endswith("bubble-git-guard")]
    assert len(guard_calls) == 1
    g = guard_calls[0]
    assert g[1] == "push" and "runtime_write_own" in g
    assert "--broker" not in g, g          # the guard's default (shim for agents) must apply
    flat = [" ".join(c) for c, _ in calls]
    assert not any("bubble-gh-credential-helper" in f for f in flat), flat


def test_guard_denial_is_not_retried_through_the_helper(tmp_path, monkeypatch):
    """A guard failure must surface as a failure; it must NOT fall back to a
    helper-minted token push (that is the bypass step 2 closes)."""
    _arm(monkeypatch)
    calls = _recorder(monkeypatch, push_rc=1)
    ok, err = dh.force_commit_and_push(repo_dir=tmp_path, message="L4")
    assert ok is False and "git push failed" in (err or "")
    flat = [" ".join(c) for c, _ in calls]
    assert not any("bubble-gh-credential-helper" in f for f in flat), flat
    assert sum(1 for c, _ in calls if c and "push" in c and not c[0].endswith("bubble-git-guard")) == 0


def test_no_guard_no_policy_keeps_the_helper_fallback(tmp_path, monkeypatch):
    """Documented exception path (agent-morty / hosts without a broker policy):
    unchanged, still the credential-helper push."""
    _arm(monkeypatch, slug="morty", policy=False)
    monkeypatch.setattr(dh, "_is_sudo_available", lambda: True)
    calls = _recorder(monkeypatch, helper_token="ghs_morty")
    ok, err = dh.force_commit_and_push(repo_dir=tmp_path, message="L4")
    assert ok is True, err
    flat = [" ".join(c) for c, _ in calls]
    assert any("bubble-gh-credential-helper" in f for f in flat), flat
