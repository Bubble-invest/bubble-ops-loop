"""Regression tests for #1253's token-refresh retry boundary.

The production wrappers use fixed absolute helper and destination paths.  The
tests make an isolated copy and replace only those path assignments (plus the
root-only chown/install ownership flags), then exercise the real shell logic.
No credential or live secret path is read.

Run: python3 -m pytest scripts/lib/tests/test_1253_token_refresh_retry.py -q
"""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPERS = (
    (
        REPO_ROOT / "deploy/bin/bubble-board-token-refresh.sh",
        "MINTER=/usr/local/bin/bubble-board-token.sh",
        "DEST_DIR=/run/bubble-board",
        "bubble-board-token-refresh",
    ),
    (
        REPO_ROOT
        / "console/deploy/contents-token/bubble-ops-contents-token-refresh.sh",
        "MINTER=/usr/local/bin/bubble-ops-contents-token.sh",
        "DEST_DIR=/run/bubble-ops-contents",
        "bubble-ops-contents-token-refresh",
    ),
)

SYNTHETIC_TOKEN = "ghs_synthetic_test_credential_must_not_be_logged"
OLD_SYNTHETIC_TOKEN = "ghs_existing_synthetic_token"


@pytest.fixture(params=WRAPPERS, ids=("board", "contents"))
def wrapper(request, tmp_path):
    source, minter_assignment, dest_assignment, log_name = request.param
    minter = tmp_path / "fake-minter.sh"
    minter.write_text(
        """#!/usr/bin/env bash
set -eu
count=0
[ ! -f "$STATE_FILE" ] || count=$(cat "$STATE_FILE")
count=$((count + 1))
printf '%s' "$count" > "$STATE_FILE"
if [ "$count" -le "$FAILS_BEFORE_SUCCESS" ]; then
  case "$FAIL_MODE" in
    exit)
      printf '%s' "$TEST_TOKEN"  # even failed stdout must stay out of logs
      echo "helper-sensitive-diagnostic-$count" >&2
      exit 75
      ;;
    invalid)
      printf 'definitely-not-a-token'
      exit 0
      ;;
    empty)
      exit 0
      ;;
  esac
fi
printf '%s' "$TEST_TOKEN"
""",
        encoding="utf-8",
    )
    minter.chmod(0o755)

    dest_dir = tmp_path / "destination"
    script_text = source.read_text(encoding="utf-8")
    assert minter_assignment in script_text
    assert dest_assignment in script_text
    script_text = script_text.replace(
        minter_assignment, f"MINTER={shlex.quote(str(minter))}", 1
    ).replace(dest_assignment, f"DEST_DIR={shlex.quote(str(dest_dir))}", 1)
    # The retry/validation/atomic-write behavior does not require root. Remove
    # only ownership arguments from the isolated test copy.
    script_text = script_text.replace(
        'install -d -m 0750 -o root -g claude "$DEST_DIR"',
        'install -d -m 0750 "$DEST_DIR"',
        1,
    ).replace('chown root:claude "$DEST.tmp"', ":", 1)
    runnable = tmp_path / source.name
    runnable.write_text(script_text, encoding="utf-8")
    runnable.chmod(0o755)

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    fake_sleep = mock_bin / "sleep"
    fake_sleep.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$1\" >> \"$SLEEP_LOG\"\n",
        encoding="utf-8",
    )
    fake_sleep.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{mock_bin}:{env['PATH']}",
            "STATE_FILE": str(tmp_path / "attempt-count"),
            "SLEEP_LOG": str(tmp_path / "sleep-log"),
            "TEST_TOKEN": SYNTHETIC_TOKEN,
        }
    )
    return runnable, dest_dir, log_name, env


def _run(wrapper, *, failures, mode):
    runnable, _, _, env = wrapper
    env["FAILS_BEFORE_SUCCESS"] = str(failures)
    env["FAIL_MODE"] = mode
    return subprocess.run(
        [str(runnable)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_first_attempt_success_needs_no_sleep_or_diagnostic(wrapper):
    _, dest_dir, _, env = wrapper

    result = _run(wrapper, failures=0, mode="exit")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert (dest_dir / "token").read_text(encoding="utf-8") == SYNTHETIC_TOKEN
    assert Path(env["STATE_FILE"]).read_text(encoding="utf-8") == "1"
    assert not Path(env["SLEEP_LOG"]).exists()


def test_transient_helper_failure_retries_then_atomically_installs(wrapper):
    _, dest_dir, log_name, env = wrapper

    result = _run(wrapper, failures=2, mode="exit")

    assert result.returncode == 0
    assert result.stdout == ""
    assert (dest_dir / "token").read_text(encoding="utf-8") == SYNTHETIC_TOKEN
    assert not (dest_dir / "token.tmp").exists()
    assert stat.S_IMODE((dest_dir / "token").stat().st_mode) == 0o640
    assert Path(env["STATE_FILE"]).read_text(encoding="utf-8") == "3"
    assert Path(env["SLEEP_LOG"]).read_text(encoding="utf-8").splitlines() == [
        "5",
        "10",
    ]
    assert (
        f"{log_name}: mint attempt 1/4 failed (helper exit 75; output withheld)"
        in result.stderr
    )
    assert f"{log_name}: mint succeeded on attempt 3/4" in result.stderr
    assert SYNTHETIC_TOKEN not in result.stderr
    assert "helper-sensitive-diagnostic" not in result.stderr


def test_exhausted_invalid_output_keeps_previous_token_and_is_sanitized(wrapper):
    _, dest_dir, log_name, env = wrapper
    dest_dir.mkdir()
    token_path = dest_dir / "token"
    token_path.write_text(OLD_SYNTHETIC_TOKEN, encoding="utf-8")

    result = _run(wrapper, failures=4, mode="invalid")

    assert result.returncode == 1
    assert result.stdout == ""
    assert token_path.read_text(encoding="utf-8") == OLD_SYNTHETIC_TOKEN
    assert Path(env["STATE_FILE"]).read_text(encoding="utf-8") == "4"
    assert Path(env["SLEEP_LOG"]).read_text(encoding="utf-8").splitlines() == [
        "5",
        "10",
        "20",
    ]
    assert result.stderr.count("invalid helper output withheld") == 4
    assert (
        f"{log_name}: mint failed after 4 attempts; token file left untouched"
        in result.stderr
    )
    assert "definitely-not-a-token" not in result.stderr
    assert OLD_SYNTHETIC_TOKEN not in result.stderr


def test_exhausted_empty_output_keeps_previous_token_and_is_sanitized(wrapper):
    _, dest_dir, log_name, env = wrapper
    dest_dir.mkdir()
    token_path = dest_dir / "token"
    token_path.write_text(OLD_SYNTHETIC_TOKEN, encoding="utf-8")

    result = _run(wrapper, failures=4, mode="empty")

    assert result.returncode == 1
    assert result.stdout == ""
    assert token_path.read_text(encoding="utf-8") == OLD_SYNTHETIC_TOKEN
    assert Path(env["STATE_FILE"]).read_text(encoding="utf-8") == "4"
    assert Path(env["SLEEP_LOG"]).read_text(encoding="utf-8").splitlines() == [
        "5",
        "10",
        "20",
    ]
    assert result.stderr.count("empty helper output") == 4
    assert (
        f"{log_name}: mint failed after 4 attempts; token file left untouched"
        in result.stderr
    )
    assert SYNTHETIC_TOKEN not in result.stderr
    assert OLD_SYNTHETIC_TOKEN not in result.stderr


@pytest.mark.parametrize("source,_,__,___", WRAPPERS, ids=("board", "contents"))
def test_production_wrapper_has_valid_bash_syntax(source, _, __, ___):
    result = subprocess.run(
        ["bash", "-n", str(source)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
