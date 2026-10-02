"""The guard must be fast-forward-only for existing destination refs.

Incident 2026-10-02: a force-with-lease bound to the remote's CURRENT sha
always matches, so a non-fast-forward push silently overwrote merged PRs
(bubble-ops-tony#82/#83). The lease only closes the check/push race.
"""

import json
import subprocess
import threading
import time

from src.guard import Guard
from src.policy_loader import load_policy
from tests.conftest import _git, stage_files
from tests.test_1413_push_target import _write_runtime_policy


def _broker(path, marker, delay=0):
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        f"open({str(marker)!r}, 'a').write('minted\\n')\n"
        f"time.sleep({delay!r})\n"
        "sys.stdout.write('ghs_MOCK' + ('a' * 40))\n"
    )
    path.chmod(0o755)
    return path


def _remote_main(repo):
    return _git(repo, "ls-remote", "origin", "refs/heads/main").stdout.split()[0]


def _advance_remote(tmp_path, name="other.txt"):
    """Land a commit on the remote's main from a second clone."""
    other = tmp_path / f"other-{name}"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(other)], check=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "o")
    (other / "outputs").mkdir(exist_ok=True)
    (other / "outputs" / name).write_text("merged PR\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "merged PR on remote")
    _git(other, "push", "origin", "main")
    return _git(other, "rev-parse", "HEAD").stdout.strip()


def _guard(tmp_path, marker, delay=0):
    policy = _write_runtime_policy(tmp_path / "policy.yaml")
    broker = _broker(tmp_path / "broker", marker, delay)
    return Guard(
        load_policy(policy), broker_cmd=[str(broker)],
        audit_log_path=tmp_path / "audit.jsonl",
    )


def _audit(tmp_path):
    return [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]


def test_remote_has_commit_source_lacks_is_denied(temp_git_repo, tmp_path):
    marker = tmp_path / "minted"
    remote_tip = _advance_remote(tmp_path)
    stage_files(temp_git_repo, ["outputs/mine.txt"])
    _git(temp_git_repo, "commit", "-m", "local divergent work")
    local = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()

    rc = _guard(tmp_path, marker).push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture"
    )
    assert rc == 1
    assert _remote_main(temp_git_repo) == remote_tip  # remote unchanged
    assert not marker.exists()  # no broker call / no token mint
    last = _audit(tmp_path)[-1]
    assert last["status"] == "denied"
    assert any("non_fast_forward" in r for r in last["reasons"])
    # never-lose-work: local commit intact
    assert _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip() == local


def test_denied_message_tells_dept_to_pull(temp_git_repo, tmp_path, capsys):
    _advance_remote(tmp_path)
    stage_files(temp_git_repo, ["outputs/mine.txt"])
    _git(temp_git_repo, "commit", "-m", "local")
    _guard(tmp_path, tmp_path / "m").push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture"
    )
    assert "safe_pull" in capsys.readouterr().err


def test_plain_fast_forward_is_pushed(temp_git_repo, tmp_path):
    marker = tmp_path / "minted"
    stage_files(temp_git_repo, ["outputs/mine.txt"])
    _git(temp_git_repo, "commit", "-m", "ff work")
    local = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()
    rc = _guard(tmp_path, marker).push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture"
    )
    assert rc == 0
    assert _remote_main(temp_git_repo) == local
    assert _audit(tmp_path)[-1]["status"] == "pushed"


def test_concurrent_remote_update_after_planning_is_rejected_by_lease(
    temp_git_repo, tmp_path
):
    marker = tmp_path / "minted"
    stage_files(temp_git_repo, ["outputs/mine.txt"])
    _git(temp_git_repo, "commit", "-m", "ff work")
    result = {}

    def bump():
        time.sleep(0.5)  # while the (slow) broker is minting
        result["tip"] = _advance_remote(tmp_path, "race.txt")

    t = threading.Thread(target=bump)
    t.start()
    rc = _guard(tmp_path, marker, delay=1.5).push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture"
    )
    t.join()
    assert rc == 1
    assert _remote_main(temp_git_repo) == result["tip"]  # race winner kept
    assert _audit(tmp_path)[-1]["status"] == "push_failed"


def test_new_branch_creation_still_works(temp_git_repo, tmp_path):
    _git(temp_git_repo, "checkout", "-b", "brand-new")
    stage_files(temp_git_repo, ["outputs/new.txt"])
    _git(temp_git_repo, "commit", "-m", "new branch")
    sha = _git(temp_git_repo, "rev-parse", "HEAD").stdout.strip()
    rc = _guard(tmp_path, tmp_path / "m").push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture",
        ref="HEAD:brand-new",
    )
    assert rc == 0
    out = _git(temp_git_repo, "ls-remote", "--heads", "origin", "brand-new").stdout
    assert out.split()[0] == sha
