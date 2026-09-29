"""Board #1552 regression: bare-name `bubble-token-broker` exec must not
surface a bare PermissionError when a PATH directory is untraversable, and
must fall back to the fleet-standard absolute install path when PATH
resolution genuinely fails.

Root cause recap (see board #1552 + RUNBOOK-1120 §2026-09-05 correction):
  - The Bash-tool PATH a dept's live Claude Code session actually uses comes
    from `~/.claude/settings.json`'s env.PATH, which historically (a) lacked
    `/opt/bubble-token-broker/bin` and (b) started with `/home/claude/.bun/bin`
    — untraversable by every `agent-<slug>` OS user since board #1120.
  - `subprocess.run(["bubble-token-broker", ...])` uses execvp-family PATH
    search. Per POSIX semantics, if ANY directory in PATH denies traversal
    (EACCES) and the binary is never found in any OTHER directory either, the
    terminal error reported is EACCES/PermissionError — NOT ENOENT/
    FileNotFoundError — even though the broker was never actually present in
    the denied directory. This reads exactly like "found it, but access
    denied", which is misleading and (pre-fix) uncaught by the guard.

This module tests the two guard-side mitigations:
  1. `resolve_broker_binary()` — a PATH search (via shutil.which, which
     treats an inaccessible directory as "not found there" rather than
     raising) with a fallback to the absolute fleet-standard install path.
  2. `Guard.push()` catching PermissionError (not just FileNotFoundError)
     from the broker-mint subprocess call, and printing a legible,
     PATH-problem-naming error instead of letting the exception propagate.
"""

from __future__ import annotations

from src import guard as guard_module
from src.guard import Guard, resolve_broker_binary
from src.policy_loader import load_policy
from tests.conftest import commit_staged, stage_files


# --------------------------------------------------------------------------
# resolve_broker_binary() unit tests
# --------------------------------------------------------------------------


def test_explicit_broker_is_never_second_guessed(monkeypatch, tmp_path):
    """An explicit --broker value is used verbatim, no PATH search at all."""
    monkeypatch.setenv("PATH", "/does/not/matter")
    assert resolve_broker_binary("/some/explicit/path") == "/some/explicit/path"


def test_falls_back_to_absolute_path_when_path_has_unreadable_dir_and_no_broker(
    monkeypatch, tmp_path
):
    """PATH = [unreadable dir, normal empty dir], broker in neither place.

    The unreadable dir simulates the untraversable `/home/claude/.bun/bin`
    ancestor (board #1120). shutil.which must skip it silently (no raise)
    rather than let an EACCES abort the whole search, and — since the broker
    is genuinely nowhere on PATH — resolve_broker_binary() must fall through
    to the fleet-standard absolute path.
    """
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    locked_dir.chmod(0o000)  # no read, no execute/traverse for anyone but owner-as-root
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    # The fallback absolute path DOES exist + is executable in this test.
    abs_broker_dir = tmp_path / "opt-broker-bin"
    abs_broker_dir.mkdir()
    abs_broker = abs_broker_dir / "bubble-token-broker"
    abs_broker.write_text("#!/usr/bin/env python3\n")
    abs_broker.chmod(0o755)
    monkeypatch.setattr(guard_module, "DEFAULT_BROKER_ABS_PATH", str(abs_broker))

    monkeypatch.setenv("PATH", f"{locked_dir}:{empty_dir}")

    try:
        result = resolve_broker_binary()
        assert result == str(abs_broker)
    finally:
        # Restore perms so tmp_path cleanup can remove the directory.
        locked_dir.chmod(0o755)


def test_returns_bare_name_when_absolute_path_also_missing(monkeypatch, tmp_path):
    """Neither PATH nor the absolute fallback has the broker: resolve_broker_binary()
    must return the bare name unchanged (never raise here — Guard.push() is
    what turns this into a legible error later, not this resolver)."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))
    monkeypatch.setattr(
        guard_module, "DEFAULT_BROKER_ABS_PATH", str(tmp_path / "nowhere" / "bubble-token-broker")
    )
    assert resolve_broker_binary() == guard_module.DEFAULT_BROKER_NAME


# --------------------------------------------------------------------------
# Guard.push() end-to-end: PermissionError must be a clear error, not a
# traceback, and must name the PATH problem.
# --------------------------------------------------------------------------


def test_guard_reports_clear_error_not_traceback_on_permission_denied_broker(
    fixture_policy_yaml, temp_git_repo, mock_git_push, tmp_path, capsys
):
    """A broker path whose PARENT directory is untraversable raises
    PermissionError from subprocess.run's execve — Guard.push() must catch
    it, print a legible PATH-naming error to stderr, audit `mint_failed`,
    and return 1 (never let the exception escape)."""
    stage_files(temp_git_repo, ["outputs/x.md"])
    commit_staged(temp_git_repo)
    locked_dir = tmp_path / "locked-broker-dir"
    locked_dir.mkdir()
    broker_path = locked_dir / "bubble-token-broker"
    broker_path.write_text("#!/usr/bin/env python3\n")
    broker_path.chmod(0o755)
    # Deny traversal on the PARENT dir — mirrors /home/claude being
    # drwxr-x--- to every agent-<slug> user (board #1120/#1552).
    locked_dir.chmod(0o000)

    audit = tmp_path / "audit.jsonl"
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy, broker_cmd=[str(broker_path)], audit_log_path=audit)

    try:
        rc = g.push(
            repo_dir=temp_git_repo,
            dept="fixture",
            action="runtime_write_own",
            repo="bubble-ops-fixture",
        )
    finally:
        locked_dir.chmod(0o755)

    assert rc != 0
    captured = capsys.readouterr()
    # A legible, PATH-naming message — not a bare Python traceback.
    assert "Traceback" not in captured.err
    assert "permission denied" in captured.err.lower()
    assert "PATH" in captured.err
    assert mock_git_push.calls == []  # never reached git push
    if audit.exists():
        import json
        lines = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
        assert any(ln.get("status") == "mint_failed" for ln in lines)


def test_cli_default_broker_resolves_via_resolve_broker_binary(
    monkeypatch, fixture_policy_yaml, temp_git_repo, tmp_path
):
    """cli.py's --broker default (None) must route through
    resolve_broker_binary() rather than hardcoding the bare name — this is
    what lets the PATH-search-then-absolute-fallback actually engage for a
    real invocation with no --broker flag given. Verified end-to-end via
    `cli_main` in --dry-run (no real broker/network needed) by capturing
    what broker_cmd Guard() actually receives."""
    from src import cli as cli_module

    stage_files(temp_git_repo, ["outputs/x.md"])
    commit_staged(temp_git_repo)

    calls = {"broker_arg": "unset"}

    def fake_resolve(broker=None):
        calls["broker_arg"] = broker
        return "/resolved/bubble-token-broker"

    monkeypatch.setattr(cli_module, "resolve_broker_binary", fake_resolve)

    captured = {}
    real_guard_init = cli_module.Guard.__init__

    def fake_init(self, *, broker_cmd=None, **kwargs):
        captured["broker_cmd"] = broker_cmd
        real_guard_init(self, broker_cmd=broker_cmd, **kwargs)

    monkeypatch.setattr(cli_module.Guard, "__init__", fake_init)

    rc = cli_module.main(
        [
            "push",
            "--dept", "fixture",
            "--action", "runtime_write_own",
            "--repo", "bubble-ops-fixture",
            "--policy", str(fixture_policy_yaml),
            "--repo-dir", str(temp_git_repo),
            "--audit-log", str(tmp_path / "audit.jsonl"),
            "--dry-run",
        ]
    )
    assert rc == 0
    # args.broker was never passed on the CLI, so it must have reached
    # resolve_broker_binary() as None (the "no --broker given" case), and
    # Guard must have received resolve_broker_binary()'s return value.
    assert calls["broker_arg"] is None
    assert captured["broker_cmd"] == ["/resolved/bubble-token-broker"]
