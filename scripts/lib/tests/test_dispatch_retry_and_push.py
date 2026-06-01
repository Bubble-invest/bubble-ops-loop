"""
test_dispatch_retry_and_push.py — TDD for auto-retry mechanism + forced
commit-and-push helpers.

{{OPERATOR}} msg 3134 (2026-05-24):
  > We need a mechanism for agent auto retry if he doesn't fetch the
  > correct input and outputs the correct expected format and data.
  >
  > Also a forced git commit and push all at each risk manager mission step.

What we add to dispatch_helpers.py:

1. `validate_layer_output(layer, output_dir, expected_artifacts)` →
   (ok: bool, missing: list[str], malformed: list[tuple[str, str]])
   Used at end of each layer subagent run to verify outputs match contract.

2. `should_retry(retry_count, max_retries=3)` → bool
   Simple gate: True if retry_count < max_retries. The prompt prose tells
   the agent to re-read inputs and rerun on validation failure.

3. `force_commit_and_push(repo_dir, message, ...)` → (ok, error_msg)
   Wraps `bubble-git-guard push --action runtime_write_own`. Idempotent
   (no-op if nothing to commit). Retries once on push conflict
   (pull --rebase + retry).

4. `escalate_validation_failure(slug, layer, retry_count, missing, malformed)`
   → sends an urgent Telegram alert via the dept's bot when we exhaust
   retries. Module-level constant `MAX_RETRIES_DEFAULT = 3`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent
for p in (str(SCRIPTS_LIB),):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_helpers as dh  # noqa: E402


# ── validate_layer_output ────────────────────────────────────────────────

def test_validate_returns_ok_when_all_artifacts_present(tmp_path):
    out = tmp_path / "outputs" / "2026-05-24" / "4"
    out.mkdir(parents=True)
    (out / "summary.md").write_text("ok", encoding="utf-8")
    (out / "logs.jsonl").write_text('{"event":"ok"}\n', encoding="utf-8")
    (out / ".last-run").write_text("2026-05-24T22:00:00+00:00", encoding="utf-8")
    ok, missing, malformed = dh.validate_layer_output(
        layer=4, output_dir=out,
        expected_artifacts=[
            {"name": "summary.md", "kind": "markdown"},
            {"name": "logs.jsonl", "kind": "jsonl"},
            {"name": ".last-run", "kind": "iso_timestamp"},
        ],
    )
    assert ok is True
    assert missing == []
    assert malformed == []


def test_validate_lists_missing_artifacts(tmp_path):
    out = tmp_path / "outputs" / "2026-05-24" / "4"
    out.mkdir(parents=True)
    (out / "summary.md").write_text("ok", encoding="utf-8")
    # Omit logs.jsonl and .last-run on purpose
    ok, missing, malformed = dh.validate_layer_output(
        layer=4, output_dir=out,
        expected_artifacts=[
            {"name": "summary.md", "kind": "markdown"},
            {"name": "logs.jsonl", "kind": "jsonl"},
            {"name": ".last-run", "kind": "iso_timestamp"},
        ],
    )
    assert ok is False
    assert set(missing) == {"logs.jsonl", ".last-run"}


def test_validate_flags_malformed_iso_timestamp(tmp_path):
    out = tmp_path / "outputs" / "2026-05-24" / "1"
    out.mkdir(parents=True)
    (out / ".last-run").write_text("not-a-timestamp", encoding="utf-8")
    ok, missing, malformed = dh.validate_layer_output(
        layer=1, output_dir=out,
        expected_artifacts=[{"name": ".last-run", "kind": "iso_timestamp"}],
    )
    assert ok is False
    assert missing == []
    assert len(malformed) == 1
    assert malformed[0][0] == ".last-run"
    assert "iso" in malformed[0][1].lower() or "timestamp" in malformed[0][1].lower()


def test_validate_flags_malformed_yaml(tmp_path):
    out = tmp_path / "outputs" / "2026-05-24" / "4"
    out.mkdir(parents=True)
    (out / "risk-kpis.yaml").write_text("not: : valid: yaml", encoding="utf-8")
    ok, missing, malformed = dh.validate_layer_output(
        layer=4, output_dir=out,
        expected_artifacts=[{"name": "risk-kpis.yaml", "kind": "yaml"}],
    )
    assert ok is False
    assert len(malformed) == 1
    assert malformed[0][0] == "risk-kpis.yaml"


def test_validate_flags_empty_jsonl(tmp_path):
    out = tmp_path / "outputs" / "2026-05-24" / "2"
    out.mkdir(parents=True)
    (out / "logs.jsonl").write_text("", encoding="utf-8")  # empty = malformed
    ok, missing, malformed = dh.validate_layer_output(
        layer=2, output_dir=out,
        expected_artifacts=[{"name": "logs.jsonl", "kind": "jsonl"}],
    )
    assert ok is False
    assert len(malformed) == 1
    assert malformed[0][0] == "logs.jsonl"


# ── should_retry ─────────────────────────────────────────────────────────

def test_should_retry_true_under_cap():
    assert dh.should_retry(retry_count=0) is True
    assert dh.should_retry(retry_count=1) is True
    assert dh.should_retry(retry_count=2) is True


def test_should_retry_false_at_cap():
    assert dh.should_retry(retry_count=3) is False
    assert dh.should_retry(retry_count=4) is False


def test_should_retry_respects_custom_cap():
    assert dh.should_retry(retry_count=4, max_retries=5) is True
    assert dh.should_retry(retry_count=5, max_retries=5) is False


def test_max_retries_default_is_3():
    """{{OPERATOR}} msg 3134: cap at 3. Locked in the module-level constant
    so PROMPT.md can reference it."""
    assert dh.MAX_RETRIES_DEFAULT == 3


# ── force_commit_and_push ────────────────────────────────────────────────

def test_force_commit_and_push_noop_when_nothing_staged(tmp_path, monkeypatch):
    """If `git status` shows nothing to commit, the helper exits OK with
    no error. Bubble-git-guard isn't even invoked."""
    import subprocess
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, err = dh.force_commit_and_push(repo_dir=tmp_path, message="test")
    assert ok is True
    assert err is None
    # We at least asked git status — but we did NOT call bubble-git-guard
    assert any("status" in c for c in calls), calls
    assert not any("bubble-git-guard" in " ".join(c) for c in calls), calls


def test_force_commit_and_push_invokes_bubble_git_guard_when_dirty(
    tmp_path, monkeypatch,
):
    """Happy path: dirty tree → add + commit + bubble-git-guard push."""
    import subprocess
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=" M outputs/2026-05-24/4/risk-brief.md\n",
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, err = dh.force_commit_and_push(
        repo_dir=tmp_path, message="L4: risk-brief written",
    )
    assert ok is True
    assert err is None
    # Verify the call sequence: status → add → commit → push
    cmd_strs = [" ".join(c) for c in calls]
    assert any("git" in c and "add" in c for c in cmd_strs), cmd_strs
    assert any("git" in c and "commit" in c for c in cmd_strs), cmd_strs
    assert any("bubble-git-guard" in c and "push" in c for c in cmd_strs), cmd_strs


def test_force_commit_and_push_returns_error_on_push_failure(
    tmp_path, monkeypatch,
):
    """If bubble-git-guard push returns non-zero, helper returns (False, err)."""
    import subprocess
    call_log = []

    def fake_run(cmd, **kw):
        call_log.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=" M something\n", stderr="",
            )
        if "bubble-git-guard" in cmd_str and "push" in cmd_str:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="push rejected: non-fast-forward",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, err = dh.force_commit_and_push(repo_dir=tmp_path, message="test")
    assert ok is False
    assert err is not None
    assert "push" in err.lower() or "non-fast-forward" in err.lower()
