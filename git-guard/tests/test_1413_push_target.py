"""The checked changeset must match the source and destination passed to push."""

import subprocess
import threading
import time

import pytest

from src.guard import Guard
from src.policy_loader import load_policy
from src.staging import staged_paths_for_push
from tests.conftest import _git, stage_files

# Captured at import time, before the `mock_git_push` fixture (see conftest.py)
# ever gets a chance to monkeypatch the process-wide `subprocess.run` for
# `git push`-shaped commands. `mock_git_push` patches the `subprocess` MODULE
# object's `run` attribute directly, which is the same singleton everywhere
# it's imported (including this file's own `import subprocess` above) — so
# a TEST-SETUP push (getting a real destination branch onto the fixture's
# real bare remote, before the guard is even invoked) would otherwise be
# silently swallowed by that fixture instead of actually reaching the remote.
_REAL_SUBPROCESS_RUN = subprocess.run


def _real_push(repo, *args):
    """Push straight to the real remote, immune to `mock_git_push`."""
    return _REAL_SUBPROCESS_RUN(
        ["git", "push", *args], cwd=str(repo), check=True, capture_output=True, text=True
    )


def _write_runtime_policy(path):
    path.write_text(
        """github_access:
  actor: ops-loop-fixture
  own_repo: bubble-ops-fixture
  read: [bubble-ops-fixture]
  write:
    - repo: bubble-ops-fixture
      allowed_paths: ["outputs/**", "queues/**", "inbox/**", "tests/**", ".gitkeep"]
      mode: direct_runtime_commit
  pull_requests:
    can_open_to: []
"""
    )
    return path


def _write_broker(path, *, delay=0):
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        f"time.sleep({delay!r})\n"
        "sys.stdout.write('ghs_MOCK' + ('a' * 40))\n"
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def diverged_repo(temp_git_repo):
    repo = temp_git_repo
    _git(repo, "branch", "onboarding/ben")
    stage_files(repo, ["requirements.txt"], "weasyprint==66.0\n")
    _git(repo, "commit", "-m", "existing main dependency")
    _git(repo, "push", "origin", "main", "onboarding/ben")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/onboarding/ben")
    _git(repo, "branch", "--set-upstream-to=origin/onboarding/ben")
    stage_files(repo, ["outputs/heartbeat.log"])
    _git(repo, "commit", "-m", "runtime heartbeat")
    assert not _git(repo, "status", "--porcelain").stdout
    return repo


@pytest.mark.parametrize("ref", ["HEAD", "main", "refs/heads/main", "HEAD:main", "HEAD:refs/heads/main"])
def test_heartbeat_push_ignores_divergent_default_and_upstream(
    diverged_repo, fixture_policy_yaml, ref, monkeypatch, capsys
):
    # This override must not supersede the destination being authorized.
    monkeypatch.setenv("BUBBLE_GUARD_DIFF_BASE", "origin/HEAD")
    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(diverged_repo, "fixture", "runtime_write_own", "bubble-ops-fixture",
                      ref=ref, dry_run=True) == 0
    output = capsys.readouterr().err
    assert "outputs/heartbeat.log" in output
    assert "requirements.txt" not in output


def test_structural_commit_cannot_be_hidden_by_upstream_or_override(
    temp_git_repo, fixture_policy_yaml, mock_broker_binary, broker_call_log, mock_git_push, monkeypatch
):
    stage_files(temp_git_repo, ["MANDATE.md"])
    _git(temp_git_repo, "commit", "-m", "unreviewed structural change")
    _git(temp_git_repo, "update-ref", "refs/remotes/origin/onboarding/ben", "HEAD")
    _git(temp_git_repo, "branch", "--set-upstream-to=origin/onboarding/ben")
    monkeypatch.setenv("BUBBLE_GUARD_DIFF_BASE", "HEAD")
    stage_files(temp_git_repo, ["outputs/heartbeat.log"])
    guard = Guard(load_policy(fixture_policy_yaml), broker_cmd=[str(mock_broker_binary)])
    assert guard.push(temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture") == 1
    assert not broker_call_log.exists()
    assert not mock_git_push.calls


def test_explicit_source_is_checked_instead_of_checkout(temp_git_repo):
    _git(temp_git_repo, "checkout", "-b", "other")
    stage_files(temp_git_repo, ["MANDATE.md"])
    _git(temp_git_repo, "commit", "-m", "structural source")
    _git(temp_git_repo, "checkout", "main")
    assert staged_paths_for_push(temp_git_repo, ref="other:main") == ["MANDATE.md"]


def test_selected_remote_and_destination_are_checked(temp_git_repo, tmp_path):
    """`--remote backup` must be resolved against the REAL `backup` remote,
    not conflated with `origin` (whose local tracking ref is deliberately
    forged here to a value that would produce the WRONG answer if it were
    ever consulted for the `backup` push).

    (#543 hardening: `backup` used to be added with a fake, unfetchable URL
    and the test faked `refs/remotes/backup/release` locally — that only
    ever exercised the OLD, vulnerable local-ref-trusting code path. `backup`
    is now a real bare repo so `ls-remote`/`fetch` against it actually work.
    """
    backup_remote = tmp_path / "backup-remote.git"
    subprocess.run(["git", "init", "--bare", str(backup_remote)], check=True, capture_output=True)
    _git(temp_git_repo, "remote", "add", "backup", str(backup_remote))
    _git(temp_git_repo, "push", "backup", "HEAD:release")
    # Forge origin's local tracking ref for the SAME destination name to a
    # value that would hide the upcoming change if the wrong remote's local
    # ref were ever read.
    _git(temp_git_repo, "update-ref", "refs/remotes/origin/release", "HEAD")

    stage_files(temp_git_repo, ["outputs/heartbeat.log"])
    _git(temp_git_repo, "commit", "-m", "runtime")
    assert staged_paths_for_push(temp_git_repo, remote="backup", ref="HEAD:release") == ["outputs/heartbeat.log"]


def test_missing_destination_checks_complete_source_tree(diverged_repo):
    paths = staged_paths_for_push(diverged_repo, ref="HEAD:new-branch")
    assert {".gitkeep", "requirements.txt", "outputs/heartbeat.log"} <= set(paths)


def test_head_swap_during_broker_mint_cannot_change_the_pushed_commit(
    temp_git_repo, tmp_path
):
    """The exact TOCTOU PoC from security review round 3.

    The path check must bind the operation to one immutable source SHA. Moving
    symbolic HEAD while the broker is minting may change the checkout, but it
    must not change the object sent by the already-authorized push.
    """
    policy_path = _write_runtime_policy(tmp_path / "policy.yaml")
    broker_path = _write_broker(tmp_path / "slow-broker", delay=1.0)

    stage_files(temp_git_repo, ["outputs/safe.txt"], "legitimate\n")
    _git(temp_git_repo, "commit", "-m", "runtime heartbeat")
    checked_sha = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()
    errors = []

    def swap_head():
        try:
            time.sleep(0.25)
            stage_files(temp_git_repo, ["MANDATE.md"], "attacker controlled\n")
            _git(temp_git_repo, "commit", "-m", "swap HEAD during mint")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    attacker = threading.Thread(target=swap_head)
    attacker.start()
    guard = Guard(load_policy(policy_path), broker_cmd=[str(broker_path)])
    rc = guard.push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture"
    )
    attacker.join()
    assert not errors
    assert rc == 0
    assert _git(temp_git_repo, "rev-parse", "refs/remotes/origin/main").stdout.strip() == checked_sha
    remote_paths = _git(
        temp_git_repo, "ls-tree", "-r", "--name-only", "refs/remotes/origin/main"
    ).stdout.splitlines()
    assert "outputs/safe.txt" in remote_paths
    assert "MANDATE.md" not in remote_paths


def test_new_branch_merge_only_structural_path_is_denied(
    temp_git_repo, tmp_path
):
    """The exact merge-hidden-path PoC from security review round 3."""
    policy_path = _write_runtime_policy(tmp_path / "policy.yaml")
    broker_path = _write_broker(tmp_path / "broker")

    _git(temp_git_repo, "checkout", "-b", "tony/directive/new-thing")
    stage_files(temp_git_repo, ["outputs/safe.txt"], "legitimate\n")
    _git(temp_git_repo, "commit", "-m", "legit new-branch change")

    _git(temp_git_repo, "checkout", "-b", "side", "main")
    stage_files(temp_git_repo, ["outputs/side.txt"], "safe too\n")
    _git(temp_git_repo, "commit", "-m", "safe side change")

    _git(temp_git_repo, "checkout", "tony/directive/new-thing")
    _git(temp_git_repo, "merge", "--no-ff", "--no-commit", "side")
    stage_files(temp_git_repo, ["MANDATE.md"], "merge-only structural change\n")
    _git(temp_git_repo, "commit", "-m", "merge with resolution-only path")

    guard = Guard(load_policy(policy_path), broker_cmd=[str(broker_path)])
    rc = guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
        ref="HEAD:tony/directive/new-thing",
    )
    assert rc == 1
    remote_ref = _git(
        temp_git_repo,
        "ls-remote",
        "--heads",
        "origin",
        "refs/heads/tony/directive/new-thing",
    ).stdout
    assert remote_ref == ""


def test_existing_branch_tree_diff_sees_merge_only_structural_path(
    temp_git_repo, fixture_policy_yaml
):
    """An existing destination is checked by final-tree diff, including merges."""
    _git(temp_git_repo, "checkout", "-b", "candidate")
    stage_files(temp_git_repo, ["outputs/safe.txt"])
    _git(temp_git_repo, "commit", "-m", "safe candidate change")

    _git(temp_git_repo, "checkout", "-b", "side", "main")
    stage_files(temp_git_repo, ["outputs/side.txt"])
    _git(temp_git_repo, "commit", "-m", "safe side change")

    _git(temp_git_repo, "checkout", "candidate")
    _git(temp_git_repo, "merge", "--no-ff", "--no-commit", "side")
    stage_files(temp_git_repo, ["MANDATE.md"], "merge-only structural change\n")
    _git(temp_git_repo, "commit", "-m", "merge with resolution-only path")

    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
        ref="HEAD:main",
        dry_run=True,
    ) == 1


def test_rename_of_structural_path_into_allowed_path_is_denied(
    temp_git_repo, fixture_policy_yaml
):
    """Rename detection must never collapse away the deleted structural path."""
    stage_files(temp_git_repo, ["MANDATE.md"], "reviewed governance\n")
    _git(temp_git_repo, "commit", "-m", "seed reviewed mandate")
    _real_push(temp_git_repo, "origin", "main")

    (temp_git_repo / "outputs").mkdir(exist_ok=True)
    _git(temp_git_repo, "mv", "MANDATE.md", "outputs/archived.md")
    _git(temp_git_repo, "commit", "-m", "rename mandate into runtime output")

    paths = staged_paths_for_push(temp_git_repo)
    assert "MANDATE.md" in paths
    assert "outputs/archived.md" in paths

    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
        dry_run=True,
    ) == 1


def test_replace_object_cannot_substitute_clean_tree_for_checked_commit(
    temp_git_repo, fixture_policy_yaml
):
    """The guard must inspect the real object that an ordinary push transfers."""
    stage_files(temp_git_repo, ["MANDATE.md"], "attacker controlled\n")
    stage_files(temp_git_repo, ["outputs/safe.txt"], "legitimate\n")
    _git(temp_git_repo, "commit", "-m", "real commit includes mandate")
    real_sha = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()

    _git(temp_git_repo, "rm", "--cached", "MANDATE.md")
    (temp_git_repo / "MANDATE.md").unlink()
    clean_tree = _git(temp_git_repo, "write-tree").stdout.strip()
    parent = _git(temp_git_repo, "rev-parse", "HEAD^").stdout.strip()
    clean_commit = _git(
        temp_git_repo,
        "commit-tree",
        clean_tree,
        "-p",
        parent,
        "-m",
        "clean replacement",
    ).stdout.strip()
    _git(temp_git_repo, "replace", real_sha, clean_commit)
    _git(temp_git_repo, "reset", "--hard", real_sha)

    paths = staged_paths_for_push(temp_git_repo)
    assert "MANDATE.md" in paths
    assert "outputs/safe.txt" in paths

    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
        dry_run=True,
    ) == 1


def test_legacy_grafts_file_fails_closed(temp_git_repo, fixture_policy_yaml):
    """GIT_NO_REPLACE_OBJECTS does not disable info/grafts, so reject it."""
    stage_files(temp_git_repo, ["outputs/safe.txt"])
    _git(temp_git_repo, "commit", "-m", "allowed runtime change")
    head = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()
    (temp_git_repo / ".git" / "info" / "grafts").write_text(f"{head}\n")

    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
        dry_run=True,
    ) == 1


def test_push_argv_binds_checked_sha_and_expected_remote_tip(
    temp_git_repo,
    fixture_policy_yaml,
    mock_broker_binary,
    mock_git_push,
):
    """The final push carries neither a symbolic source nor an unleased ref."""
    expected_remote_sha = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()
    stage_files(temp_git_repo, ["outputs/safe.txt"])
    _git(temp_git_repo, "commit", "-m", "allowed runtime change")
    checked_sha = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()

    guard = Guard(load_policy(fixture_policy_yaml), broker_cmd=[str(mock_broker_binary)])
    assert guard.push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture"
    ) == 0

    cmd, _env = mock_git_push.calls[0]
    assert "HEAD" not in cmd
    assert f"--force-with-lease=refs/heads/main:{expected_remote_sha}" in cmd
    assert cmd[-2:] == ["origin", f"{checked_sha}:refs/heads/main"]


def test_new_branch_push_lease_requires_destination_to_remain_absent(
    temp_git_repo,
    tmp_path,
    mock_git_push,
):
    policy_path = _write_runtime_policy(tmp_path / "policy.yaml")
    broker_path = _write_broker(tmp_path / "broker")
    _git(temp_git_repo, "checkout", "-b", "new-branch")
    stage_files(temp_git_repo, ["outputs/safe.txt"])
    _git(temp_git_repo, "commit", "-m", "allowed new branch")
    checked_sha = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()

    guard = Guard(load_policy(policy_path), broker_cmd=[str(broker_path)])
    assert guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
        ref="HEAD:new-branch",
    ) == 0

    cmd, _env = mock_git_push.calls[0]
    assert "--force-with-lease=refs/heads/new-branch:" in cmd
    assert cmd[-2:] == ["origin", f"{checked_sha}:refs/heads/new-branch"]


def test_allowed_new_branch_push_succeeds_with_absence_lease(
    temp_git_repo, tmp_path
):
    """Exercise the empty lease expectation against a real bare remote."""
    policy_path = _write_runtime_policy(tmp_path / "policy.yaml")
    broker_path = _write_broker(tmp_path / "broker")
    _git(temp_git_repo, "checkout", "-b", "allowed-new-branch")
    stage_files(temp_git_repo, ["outputs/safe.txt"])
    _git(temp_git_repo, "commit", "-m", "allowed new branch")
    checked_sha = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()

    guard = Guard(
        load_policy(policy_path),
        broker_cmd=[str(broker_path)],
        audit_log_path=tmp_path / "audit.jsonl",
    )
    assert guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
        ref="HEAD:allowed-new-branch",
    ) == 0
    remote_line = _git(
        temp_git_repo,
        "ls-remote",
        "--heads",
        "origin",
        "refs/heads/allowed-new-branch",
    ).stdout
    assert remote_line.split()[0] == checked_sha


def test_staged_structural_change_still_denied(diverged_repo, fixture_policy_yaml):
    stage_files(diverged_repo, ["MANDATE.md"])
    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(diverged_repo, "fixture", "runtime_write_own", "bubble-ops-fixture",
                      dry_run=True) == 1


@pytest.mark.parametrize("ref", [":main", "HEAD:", "HEAD:refs/tags/v1", "refs/heads/*:refs/heads/*", "missing:main", "--all", "HEAD:HEAD"])
def test_unsupported_ref_fails_before_mint(
    temp_git_repo, fixture_policy_yaml, mock_broker_binary, broker_call_log, mock_git_push, ref
):
    stage_files(temp_git_repo, ["outputs/heartbeat.log"])
    guard = Guard(load_policy(fixture_policy_yaml), broker_cmd=[str(mock_broker_binary)])
    assert guard.push(temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture", ref=ref) == 1
    assert not broker_call_log.exists()
    assert not mock_git_push.calls


# --------------------------------------------------------------------------
# #543 SECURITY FIX: the diff base is derived from the REAL remote (ls-remote
# + fetch into refs/git-guard/base), never from a local refs/remotes/* ref.
# --------------------------------------------------------------------------


def test_forged_refs_remotes_cannot_hide_a_structural_path(
    temp_git_repo, fixture_policy_yaml, mock_broker_binary, broker_call_log, mock_git_push
):
    """The EXACT bypass reported by the independent reviewer on PR #543.

    Reproduction from the review: the guarded actor controls its own
    checkout, so `git commit-tree <HEAD tree>` + `git update-ref
    refs/remotes/<remote>/<destination> <fake>` — no push, no network, no
    broker call — used to be enough to make `staged_paths_for_push()` see an
    empty (or truncated) diff against the forged base, hiding a genuinely
    committed, policy-denied structural path (MANDATE.md) behind an
    unrelated allowed one (outputs/heartbeat.log) that WAS visible in that
    forged diff. `git push` would then carry MANDATE.md along, unreviewed.

    Before this fix: `refs/remotes/origin/main` was read directly, so the
    forged ref (built from the tree BEFORE the heartbeat commit) made the
    unpushed-commit diff report only `outputs/heartbeat.log` — a clean,
    allowed-looking push that a broker mint + real `git push` would have
    gone on to carry MANDATE.md's forbidden path through, un-checked.
    """
    stage_files(temp_git_repo, ["MANDATE.md"])
    _git(temp_git_repo, "commit", "-m", "unreviewed structural change")
    forged_tree = _git(temp_git_repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    forged_base = _git(temp_git_repo, "commit-tree", forged_tree, "-m", "forged base (attacker-controlled)").stdout.strip()
    _git(temp_git_repo, "update-ref", "refs/remotes/origin/main", forged_base)

    # An allowed file on top of the structural one: this is what the old
    # code would have reported as the ENTIRE unpushed-commit diff against
    # the forged base (MANDATE.md is present in both the forged base's tree
    # and HEAD's tree, so it wouldn't show as "changed" at all).
    stage_files(temp_git_repo, ["outputs/heartbeat.log"])
    _git(temp_git_repo, "commit", "-m", "runtime heartbeat")

    guard = Guard(load_policy(fixture_policy_yaml), broker_cmd=[str(mock_broker_binary)])
    assert guard.push(temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture") == 1
    # Fail-closed BEFORE minting or pushing anything.
    assert not broker_call_log.exists()
    assert not mock_git_push.calls


def test_normal_push_to_non_default_branch_is_allowed(
    temp_git_repo, fixture_policy_yaml, mock_broker_binary, broker_call_log, mock_git_push
):
    """The fix must not break the ordinary, legitimate case: a genuinely new
    commit with only allowed paths, pushed to a non-default branch that
    already exists (and was fetched-from-real-remote) — full pipeline,
    not dry-run, proving mint + push both actually happen."""
    _git(temp_git_repo, "checkout", "-b", "feature/x")
    _real_push(temp_git_repo, "-u", "origin", "feature/x")
    stage_files(temp_git_repo, ["outputs/feature-note.md"])
    _git(temp_git_repo, "commit", "-m", "feature work")

    guard = Guard(load_policy(fixture_policy_yaml), broker_cmd=[str(mock_broker_binary)])
    assert guard.push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture", ref="HEAD:feature/x"
    ) == 0
    assert broker_call_log.exists()
    assert mock_git_push.calls


def test_deleted_remote_ref_falls_back_to_conservative_sweep(temp_git_repo):
    """A branch that WAS on the remote and is now deleted must be detected
    as absent via `ls-remote` (not "no local tracking ref happens to
    exist," which a stale or forged local ref could fake either way). The
    fallback then conservatively reports EVERY path in the pushed commit's
    full tree — nothing is silently hidden just because the destination is
    gone."""
    _git(temp_git_repo, "checkout", "-b", "short-lived")
    stage_files(temp_git_repo, ["outputs/a.md"])
    _git(temp_git_repo, "commit", "-m", "on short-lived")
    _git(temp_git_repo, "push", "origin", "short-lived")
    _git(temp_git_repo, "push", "origin", "--delete", "short-lived")

    stage_files(temp_git_repo, ["MANDATE.md"])
    _git(temp_git_repo, "commit", "-m", "after remote deletion")

    paths = staged_paths_for_push(temp_git_repo, ref="HEAD:short-lived")
    assert {".gitkeep", "outputs/a.md", "MANDATE.md"} <= set(paths)


def test_force_pushed_rewritten_history_is_still_diffed_correctly(temp_git_repo, fixture_policy_yaml):
    """Force-push case: local history no longer descends from the remote tip
    at all (e.g. after a hard reset to an unrelated root commit). `git diff
    A..B` compares TREES, not ancestry, so the structural path must still
    be detected even though there's no common ancestor to walk."""
    _git(temp_git_repo, "checkout", "--orphan", "rewritten")
    (temp_git_repo / "MANDATE.md").write_text("rewritten governance\n")
    _git(temp_git_repo, "add", "MANDATE.md")
    _git(temp_git_repo, "commit", "-m", "unrelated rewritten history (force-push shape)")
    _git(temp_git_repo, "branch", "-f", "main", "HEAD")
    _git(temp_git_repo, "checkout", "main")

    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture", dry_run=True
    ) == 1


def test_unreachable_remote_fails_closed(temp_git_repo, fixture_policy_yaml):
    """If `ls-remote`/`fetch` against the real remote errors (bad URL,
    network down, auth failure, ...), the guard must fail CLOSED — never
    silently fall back to treating the push as if nothing changed."""
    _git(temp_git_repo, "remote", "set-url", "origin", str(temp_git_repo / "does-not-exist.git"))
    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture", dry_run=True
    ) == 1


def test_ls_remote_and_fetch_never_read_local_remotes_ref(temp_git_repo):
    """Direct unit check on the helpers: a local refs/remotes/origin/main
    forged to an unrelated, unreachable-from-history object must have ZERO
    effect on the sha `_ls_remote_sha` reports or the object `staging`
    diffs against — both are re-derived from the real remote every call."""
    from src.staging import _ls_remote_sha

    real_sha = _git(temp_git_repo, "rev-parse", "main").stdout.strip()
    # A real, but locally-fabricated (attacker-controlled), object --
    # update-ref requires the new value to actually exist as an object, so
    # reuse HEAD's own tree to build a standalone forged commit for it.
    existing_tree = _git(temp_git_repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    bogus = _git(temp_git_repo, "commit-tree", existing_tree, "-m", "bogus forged base").stdout.strip()
    _git(temp_git_repo, "update-ref", "refs/remotes/origin/main", bogus)

    assert _ls_remote_sha(temp_git_repo, "origin", "main") == real_sha
