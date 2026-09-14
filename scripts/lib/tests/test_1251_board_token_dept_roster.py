"""Regression tests for board #1251 r2 (manager review): the per-dept board
token copy list in deploy/bin/bubble-board-token-refresh.sh must be DERIVED
from the actual /etc/sudoers.d/bubble-board-token-agent-<dept> grant files,
not a hardcoded list — a hardcoded list is enforced only by a "keep in sync"
comment, and adding a dept there without the matching sudoers grant would
silently hand that uid a token it could never mint itself (a credential
widening by omission, with nothing to catch it).

This makes an isolated copy of the real script (same technique as
test_1253_token_refresh_retry.py: substitute the fixed MINTER/DEST_DIR
paths and strip root-only ownership flags) and exercises the REAL DEPT_LIST
derivation + authorization-gate logic against a fake sudoers.d directory.

No credential or live secret path is read; the "token" is a synthetic
placeholder string.

Run: python3 -m pytest scripts/lib/tests/test_1251_board_token_dept_roster.py -q
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE = REPO_ROOT / "deploy/bin/bubble-board-token-refresh.sh"
SYNTHETIC_TOKEN = "ghs_synthetic_roster_test_credential_must_not_be_logged"

INSTALL_PATTERN = re.compile(r'install -d -m (\d+) -o root -g claude "\$DEST_DIR"')
DEPT_CHOWN = 'chown "root:${_dept_group}" "${_dept_dest}.tmp"'


@pytest.fixture()
def runnable(tmp_path):
    """An isolated, root-free copy of bubble-board-token-refresh.sh."""
    minter = tmp_path / "fake-minter.sh"
    minter.write_text(
        f"#!/usr/bin/env bash\nprintf '%s' '{SYNTHETIC_TOKEN}'\n",
        encoding="utf-8",
    )
    minter.chmod(0o755)

    dest_dir = tmp_path / "destination"
    script_text = SOURCE.read_text(encoding="utf-8")

    assert "MINTER=/usr/local/bin/bubble-board-token.sh" in script_text
    assert "DEST_DIR=/run/bubble-board" in script_text
    assert INSTALL_PATTERN.search(script_text), (
        "expected the shared install -d -m <mode> -o root -g claude line — "
        "did the ownership-flag syntax change?"
    )
    assert DEPT_CHOWN in script_text, (
        "expected the per-dept chown line — did the per-dept copy loop change?"
    )

    script_text = script_text.replace(
        "MINTER=/usr/local/bin/bubble-board-token.sh",
        f"MINTER={minter}",
        1,
    ).replace(
        "DEST_DIR=/run/bubble-board",
        f"DEST_DIR={dest_dir}",
        1,
    )
    script_text = INSTALL_PATTERN.sub(r'install -d -m \1 "$DEST_DIR"', script_text, count=1)
    script_text = script_text.replace('chown root:claude "$DEST.tmp"', ":", 1)
    script_text = script_text.replace(DEPT_CHOWN, ":", 1)

    runnable_path = tmp_path / SOURCE.name
    runnable_path.write_text(script_text, encoding="utf-8")
    runnable_path.chmod(0o755)
    return runnable_path, dest_dir


@pytest.fixture()
def mock_bin(tmp_path):
    """A fake `getent` that always reports the group as existing — isolates
    the authorization-gate assertions below from needing REAL unix groups
    (agent-ben etc.) to exist on the test host, which would require root."""
    mock_dir = tmp_path / "mockbin"
    mock_dir.mkdir()
    getent = mock_dir / "getent"
    getent.write_text(
        '#!/usr/bin/env bash\n'
        'if [ "$1" = "group" ]; then exit 0; fi\n'
        'exit 1\n',
        encoding="utf-8",
    )
    getent.chmod(0o755)
    return mock_dir


def _sudoers_dir(tmp_path, depts):
    d = tmp_path / "fake-sudoers.d"
    d.mkdir()
    for dept in depts:
        (d / f"bubble-board-token-agent-{dept}").write_text(
            f"agent-{dept} ALL=(root) NOPASSWD: /usr/local/bin/bubble-board-token.sh\n",
            encoding="utf-8",
        )
    return d


def _run(runnable_path, mock_bin, env_extra):
    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}:{env['PATH']}"
    env.update(env_extra)
    return subprocess.run(
        [str(runnable_path)], env=env, capture_output=True, text=True, check=False
    )


def test_roster_derives_dept_list_and_writes_only_authorized_copies(
    runnable, mock_bin, tmp_path
):
    """Core structural property: a dept present in an OVERRIDE list but
    ABSENT from the sudoers roster gets NO token copy — the authorization
    check (sudoers grant file), not the hand-maintained list, is what
    gates a copy."""
    runnable_path, dest_dir = runnable
    sudoers_dir = _sudoers_dir(tmp_path, ["ben", "maya"])

    result = _run(
        runnable_path,
        mock_bin,
        {
            "BUBBLE_BOARD_TOKEN_SUDOERS_DIR": str(sudoers_dir),
            "BUBBLE_BOARD_TOKEN_DEPTS": "ben maya rogue",
        },
    )

    assert result.returncode == 0, result.stderr

    # Authorized depts (present in the sudoers roster) get a copy.
    assert (dest_dir / "token.ben").read_text(encoding="utf-8") == SYNTHETIC_TOKEN
    assert (dest_dir / "token.maya").read_text(encoding="utf-8") == SYNTHETIC_TOKEN

    # "rogue" is in the override list but has NO matching sudoers grant file
    # — it must get NO copy, and the skip must be logged as an
    # authorization failure specifically (not silently, and not conflated
    # with the separate "group not found" check).
    assert not (dest_dir / "token.rogue").exists(), (
        "an unauthorized override dept got a token copy — credential "
        "widening by omission is exactly the bug this test guards against"
    )
    assert "skip per-dept copy for 'rogue'" in result.stderr
    assert "unauthorized" in result.stderr
    # The rejection message must name the specific missing grant file, not
    # just "not in the list" — this is the file-based authorization check,
    # not a list-membership check.
    assert f"{sudoers_dir}/bubble-board-token-agent-rogue" in result.stderr

    # The two legitimately-authorized depts must NOT be flagged unauthorized.
    assert "skip per-dept copy for 'ben'" not in result.stderr
    assert "skip per-dept copy for 'maya'" not in result.stderr


def test_dept_present_only_in_sudoers_roster_needs_no_override_entry(
    runnable, mock_bin, tmp_path
):
    """A dept doesn't need to appear in BUBBLE_BOARD_TOKEN_DEPTS at all —
    having its own sudoers grant file is sufficient (the roster IS the
    list; nothing needs hand-syncing)."""
    runnable_path, dest_dir = runnable
    sudoers_dir = _sudoers_dir(tmp_path, ["tony"])

    result = _run(
        runnable_path,
        mock_bin,
        {"BUBBLE_BOARD_TOKEN_SUDOERS_DIR": str(sudoers_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert (dest_dir / "token.tony").read_text(encoding="utf-8") == SYNTHETIC_TOKEN


def test_dept_that_loses_its_grant_stops_getting_a_copy(runnable, mock_bin, tmp_path):
    """The correct direction to fail: removing a dept's sudoers grant file
    (e.g. the dept was retired) immediately stops its token copy on the
    very next refresh, with no code/list change needed."""
    runnable_path, dest_dir = runnable
    sudoers_dir = _sudoers_dir(tmp_path, ["ben"])

    # First refresh: ben is authorized and gets a copy.
    result1 = _run(
        runnable_path, mock_bin, {"BUBBLE_BOARD_TOKEN_SUDOERS_DIR": str(sudoers_dir)}
    )
    assert result1.returncode == 0, result1.stderr
    assert (dest_dir / "token.ben").exists()

    # The grant is revoked (dept retired / sudoers file removed).
    (sudoers_dir / "bubble-board-token-agent-ben").unlink()
    (dest_dir / "token.ben").unlink()  # simulate tmpfs having been cleared

    result2 = _run(
        runnable_path, mock_bin, {"BUBBLE_BOARD_TOKEN_SUDOERS_DIR": str(sudoers_dir)}
    )
    assert result2.returncode == 0, result2.stderr
    assert not (dest_dir / "token.ben").exists(), (
        "a dept with a revoked sudoers grant still received a token copy"
    )


def test_override_used_as_is_when_no_sudoers_roster_exists_at_all(
    runnable, mock_bin, tmp_path
):
    """BUBBLE_BOARD_TOKEN_DEPTS is kept as a fallback candidate source for a
    host with NO sudoers.d roster at all (e.g. this test host) — it must
    still work when there is nothing to authorize against."""
    runnable_path, dest_dir = runnable
    empty_sudoers_dir = tmp_path / "empty-sudoers.d"
    empty_sudoers_dir.mkdir()

    result = _run(
        runnable_path,
        mock_bin,
        {
            "BUBBLE_BOARD_TOKEN_SUDOERS_DIR": str(empty_sudoers_dir),
            "BUBBLE_BOARD_TOKEN_DEPTS": "testdept",
        },
    )

    assert result.returncode == 0, result.stderr
    assert (dest_dir / "token.testdept").read_text(encoding="utf-8") == SYNTHETIC_TOKEN
    assert "unauthorized" not in result.stderr


def test_no_roster_and_no_override_writes_no_per_dept_copies(runnable, mock_bin, tmp_path):
    """Baseline: nothing configured at all → the shared token is still
    written (never held hostage by the per-dept feature), but no per-dept
    copies are attempted."""
    runnable_path, dest_dir = runnable
    empty_sudoers_dir = tmp_path / "empty-sudoers.d"
    empty_sudoers_dir.mkdir()

    result = _run(
        runnable_path,
        mock_bin,
        {"BUBBLE_BOARD_TOKEN_SUDOERS_DIR": str(empty_sudoers_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert (dest_dir / "token").read_text(encoding="utf-8") == SYNTHETIC_TOKEN
    assert not list(dest_dir.glob("token.*"))
