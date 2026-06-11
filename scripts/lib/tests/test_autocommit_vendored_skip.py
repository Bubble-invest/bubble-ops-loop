"""Regression: force_commit_and_push must NOT stage vendored scripts/lib/** .

Bug (Tony, 2026-06-11): the loop's auto-commit (force_commit_and_push) classified
re-vendored scripts/lib/tests/*.py as "runtime" (it is not in STRUCTURAL_PATH_GLOBS
— scripts/lib/** is structural only in the framework repo). It committed them, and
the guard then DENIED the dept's whole next push ("scripts/lib/... not in
allowed_paths"), stranding every runtime commit behind it. The fix skips vendored
canonical paths at the staging step.
"""

from __future__ import annotations

import subprocess

import scripts.lib.dispatch_helpers as dh


# ── _is_vendored_nonpushable classification ─────────────────────────────────

def test_vendored_globs_match_scripts_lib():
    assert dh._is_vendored_nonpushable("scripts/lib/dispatch_helpers.py") is True
    assert dh._is_vendored_nonpushable("scripts/lib/tests/test_build_dispatch_ctx.py") is True


def test_vendored_globs_do_not_match_runtime_paths():
    assert dh._is_vendored_nonpushable("outputs/2026-06-11/4/risk-kpis.yaml") is False
    assert dh._is_vendored_nonpushable("queues/research/x.yaml") is False
    assert dh._is_vendored_nonpushable("WORKING_MEMORY.md") is False
    assert dh._is_vendored_nonpushable("whiteboard.yaml") is False
    # a path that merely *starts* with the prefix but isn't under the dir
    assert dh._is_vendored_nonpushable("scripts/libfoo.py") is False


# ── force_commit_and_push excludes vendored paths from `git add` ────────────

def test_force_commit_and_push_skips_vendored_scripts_lib(tmp_path, monkeypatch):
    """With BOTH a runtime change and a vendored scripts/lib change dirty, only
    the runtime path is staged; scripts/lib/** is skipped (never reaches git add)."""
    added_paths: list[str] = []

    # Pretend the broker policy says scripts/lib is NOT structural in a dept repo
    # (the real gap) — so the ONLY thing keeping it out is the vendored skip.
    monkeypatch.setattr(dh, "_resolve_is_structural", lambda: (lambda p: False))
    monkeypatch.setattr(dh, "resolve_push_target", lambda rd: ("tony", "bubble-ops-tony"))

    def fake_run(cmd, *a, **k):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        # git status --porcelain → one runtime + one vendored change dirty
        if "status" in cmd and "--porcelain" in cmd:
            R.stdout = " M outputs/2026-06-11/4/risk-kpis.yaml\n M scripts/lib/tests/test_build_dispatch_ctx.py\n"
            return R
        if "remote" in cmd and "get-url" in cmd:
            R.stdout = "https://github.com/Bubble-invest/bubble-ops-tony.git"
            return R
        if cmd[3:5] == ["add", "--"] or (len(cmd) > 3 and cmd[3] == "add"):
            # capture everything after "add --"
            idx = cmd.index("add")
            added_paths.extend(cmd[idx + 2:])
            return R
        # commit / push / anything else → success
        return R

    monkeypatch.setattr(subprocess, "run", fake_run)
    # avoid the guarded-push branch resolving real binaries: force file://? No —
    # we let it run; push fake_run returns ok. Disable DRY_RUN so staging happens.
    monkeypatch.delenv("DRY_RUN", raising=False)

    ok, err = dh.force_commit_and_push(repo_dir=tmp_path, message="loop: auto-commit")

    assert "outputs/2026-06-11/4/risk-kpis.yaml" in added_paths, added_paths
    assert all("scripts/lib/" not in p for p in added_paths), (
        f"vendored scripts/lib path must NOT be staged: {added_paths}"
    )
