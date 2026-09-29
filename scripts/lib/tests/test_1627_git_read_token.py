"""Regression coverage for board #1627's sandbox-safe git read tokens.

All credentials are synthetic.  The production refresher is exercised with a
temporary sudoers roster, agents root, tmpfs directory, and /run destination;
root-only and network tools are small PATH stubs.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
REFRESHER = REPO_ROOT / "deploy/bin/bubble-git-read-token-refresh.sh"
WRAPPER = REPO_ROOT / "deploy/bin/git-credential-bubble-gh"
TOKEN = "ghs_synthetic_1627_read_token"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture()
def refresh_env(tmp_path: Path):
    bins = tmp_path / "bin"
    bins.mkdir()
    sudoers = tmp_path / "sudoers"
    sudoers.mkdir()
    agents = tmp_path / "agents"
    agents.mkdir()
    run_dir = tmp_path / "run" / "bubble-git"
    key_dir = tmp_path / "run" / "lock"
    key_dir.mkdir(parents=True)
    remotes = tmp_path / "remotes"
    remotes.mkdir()

    logs = {
        "runuser": tmp_path / "runuser.log",
        "curl": tmp_path / "curl.log",
        "chown": tmp_path / "chown.log",
        "mv": tmp_path / "mv.log",
        "install": tmp_path / "install.log",
    }

    _write_executable(
        bins / "install",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$INSTALL_LOG"
mode=0755
while (($#)); do
  case "$1" in
    -m) mode=$2; shift 2 ;;
    -o|-g) shift 2 ;;
    -d) shift ;;
    *) dest=$1; shift ;;
  esac
done
mkdir -p "$dest"
chmod "$mode" "$dest"
""",
    )
    _write_executable(
        bins / "sops",
        """#!/usr/bin/env bash
set -eu
out=
while (($#)); do
  if [[ "$1" == --output ]]; then out=$2; shift 2; else shift; fi
done
[[ -n "$out" ]]
printf '%s' 'synthetic-private-key' > "$out"
""",
    )
    _write_executable(
        bins / "openssl",
        """#!/usr/bin/env bash
while (($#)); do shift; done
cat
""",
    )
    _write_executable(
        bins / "runuser",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$RUNUSER_LOG"
[[ "$1" == -u && "$3" == -- ]]
shift 3
exec "$@"
""",
    )
    _write_executable(
        bins / "git",
        """#!/usr/bin/env bash
set -eu
[[ "$1" == -C && "$3" == remote && "$4" == get-url && "$5" == origin ]]
slug=${2##*/}
cat "$REMOTE_DIR/$slug"
""",
    )
    _write_executable(
        bins / "curl",
        f"""#!/usr/bin/env bash
set -eu
data=
while (($#)); do
  if [[ "$1" == -d ]]; then data=$2; shift 2; else shift; fi
done
printf '%s\n' "$data" >> "$CURL_LOG"
if [[ -n "${{CURL_INVALID_REPO:-}}" && "$data" == *\"${{CURL_INVALID_REPO}}\"* ]]; then
  printf '%s' '{{"token":""}}'
  exit 0
fi
printf '%s' '{{"token":"{TOKEN}"}}'
""",
    )
    _write_executable(
        bins / "chown",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CHOWN_LOG"
""",
    )
    _write_executable(
        bins / "mv",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MV_LOG"
exec /bin/mv "$@"
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bins}:{env['PATH']}",
            "BUBBLE_GIT_READ_TOKEN_SUDOERS_DIR": str(sudoers),
            "BUBBLE_GIT_READ_TOKEN_AGENTS_ROOT": str(agents),
            "BUBBLE_GIT_READ_TOKEN_RUN_DIR": str(run_dir),
            "BUBBLE_GIT_READ_TOKEN_TMPFS_DIR": str(key_dir),
            "REMOTE_DIR": str(remotes),
            "RUNUSER_LOG": str(logs["runuser"]),
            "CURL_LOG": str(logs["curl"]),
            "CHOWN_LOG": str(logs["chown"]),
            "MV_LOG": str(logs["mv"]),
            "INSTALL_LOG": str(logs["install"]),
        }
    )
    return env, sudoers, remotes, run_dir, key_dir, logs


def _grant(sudoers: Path, *depts: str) -> None:
    for dept in depts:
        (sudoers / f"bubble-board-token-agent-{dept}").write_text(
            f"agent-{dept} ALL=(root) NOPASSWD: /usr/local/bin/bubble-board-token.sh\n",
            encoding="utf-8",
        )


def _remote(remotes: Path, dept: str, url: str) -> None:
    (remotes / dept).write_text(url, encoding="utf-8")


def _run_refresh(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(REFRESHER)], env=env, capture_output=True, text=True, check=False
    )


def test_sudoers_roster_is_authoritative_and_deduplicated(refresh_env):
    env, sudoers, remotes, run_dir, _, logs = refresh_env
    _grant(sudoers, "alpha", "beta")
    _remote(remotes, "alpha", "https://github.com/Bubble-invest/bubble-ops-alpha.git")
    _remote(remotes, "beta", "https://github.com/Bubble-invest/bubble-ops-beta")
    env["BUBBLE_GIT_READ_TOKEN_DEPTS"] = "alpha rogue"

    result = _run_refresh(env)

    assert result.returncode == 0, result.stderr
    assert (run_dir / "token.alpha").read_text() == TOKEN
    assert (run_dir / "token.beta").read_text() == TOKEN
    assert not (run_dir / "token.rogue").exists()
    calls = logs["runuser"].read_text()
    assert calls.count("agent-alpha") == 1
    assert calls.count("agent-beta") == 1
    assert "agent-rogue" not in calls
    assert "no matching sudoers grant" in result.stderr
    assert TOKEN not in result.stdout + result.stderr


def test_override_supplies_roster_when_sudoers_directory_is_empty(refresh_env):
    env, _, remotes, run_dir, _, _ = refresh_env
    env["BUBBLE_GIT_READ_TOKEN_DEPTS"] = "testdept"
    _remote(remotes, "testdept", "https://github.com/Bubble-invest/bubble-ops-testdept")

    result = _run_refresh(env)

    assert result.returncode == 0, result.stderr
    assert (run_dir / "token.testdept").read_text() == TOKEN


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Bubble-invest/bubble-ops-alpha",
        "https://github.com/Bubble-invest/bubble-ops-alpha.git",
    ],
)
def test_accepts_only_expected_https_form_and_scopes_payload(
    refresh_env, url: str
):
    env, _, remotes, _, _, logs = refresh_env
    env["BUBBLE_GIT_READ_TOKEN_DEPTS"] = "alpha"
    _remote(remotes, "alpha", url)

    result = _run_refresh(env)

    assert result.returncode == 0, result.stderr
    payload = logs["curl"].read_text().strip()
    assert payload == (
        '{"permissions":{"contents":"read","metadata":"read"},'
        '"repositories":["bubble-ops-alpha"]}'
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Bubble-invest/owner/bubble-ops-alpha",
        "https://github.com/Bubble-invest/bubble-ops-alpha.git?ref=x",
        "https://github.com/Bubble-invest/",
    ],
)
def test_rejects_malformed_bubble_invest_remote_urls(refresh_env, url: str):
    env, _, remotes, run_dir, _, logs = refresh_env
    env["BUBBLE_GIT_READ_TOKEN_DEPTS"] = "alpha"
    _remote(remotes, "alpha", url)

    result = _run_refresh(env)

    assert result.returncode == 1
    assert "origin is not an accepted" in result.stderr
    assert not (run_dir / "token.alpha").exists()
    assert not logs["curl"].exists()


def test_file_mode_atomic_move_owner_command_and_key_cleanup(refresh_env):
    env, _, remotes, run_dir, key_dir, logs = refresh_env
    env["BUBBLE_GIT_READ_TOKEN_DEPTS"] = "alpha"
    _remote(remotes, "alpha", "https://github.com/Bubble-invest/bubble-ops-alpha")
    run_dir.mkdir(parents=True)
    (run_dir / "token.alpha").write_text("old-token", encoding="utf-8")

    result = _run_refresh(env)

    token_file = run_dir / "token.alpha"
    assert result.returncode == 0, result.stderr
    assert token_file.read_text() == TOKEN
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o640
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o711
    assert not (run_dir / "token.alpha.tmp").exists()
    assert "root:agent-alpha" in logs["chown"].read_text()
    assert "token.alpha.tmp" in logs["mv"].read_text()
    assert not list(key_dir.glob("bubble-git-read-key.*"))
    assert TOKEN not in result.stdout + result.stderr


def test_cross_dept_origin_is_failure_and_writes_no_token(refresh_env):
    env, _, remotes, run_dir, _, logs = refresh_env
    env["BUBBLE_GIT_READ_TOKEN_DEPTS"] = "alpha"
    _remote(remotes, "alpha", "https://github.com/Bubble-invest/bubble-ops-beta")

    result = _run_refresh(env)

    assert result.returncode == 1
    assert not (run_dir / "token.alpha").exists()
    assert "alpha: origin is not an accepted Bubble-invest HTTPS repository" in result.stderr
    assert "completed with 1 department failure(s)" in result.stderr
    assert not logs["curl"].exists()


def test_non_bubble_https_origin_is_skipped_without_blocking_valid_dept(refresh_env):
    env, sudoers, remotes, run_dir, _, _ = refresh_env
    _grant(sudoers, "alpha", "beta")
    _remote(remotes, "alpha", "git@github.com:Bubble-invest/nope.git")
    _remote(remotes, "beta", "https://github.com/Bubble-invest/bubble-ops-beta")

    result = _run_refresh(env)

    assert result.returncode == 0, result.stderr
    assert not (run_dir / "token.alpha").exists()
    assert (run_dir / "token.beta").read_text() == TOKEN
    assert "alpha: origin not Bubble-invest HTTPS, skipped" in result.stderr
    assert "completed with" not in result.stderr


def test_one_dept_mint_failure_does_not_block_later_depts(refresh_env):
    env, sudoers, remotes, run_dir, _, _ = refresh_env
    _grant(sudoers, "alpha", "beta")
    _remote(remotes, "alpha", "https://github.com/Bubble-invest/bubble-ops-alpha")
    _remote(remotes, "beta", "https://github.com/Bubble-invest/bubble-ops-beta")
    env["CURL_INVALID_REPO"] = "bubble-ops-alpha"

    result = _run_refresh(env)

    assert result.returncode == 1
    assert not (run_dir / "token.alpha").exists()
    assert (run_dir / "token.beta").read_text() == TOKEN
    assert "alpha: GitHub returned no valid token" in result.stderr
    assert TOKEN not in result.stdout + result.stderr


@pytest.fixture()
def wrapper_env(tmp_path: Path):
    bins = tmp_path / "bin"
    bins.mkdir()
    run_dir = tmp_path / "bubble-git"
    run_dir.mkdir()
    stdin_log = tmp_path / "sudo.stdin"
    helper_output = tmp_path / "helper.output"

    _write_executable(
        bins / "sudo",
        """#!/usr/bin/env bash
cat > "$SUDO_STDIN_LOG"
if [[ "$SUDO_MODE" == success ]]; then
  cat "$HELPER_OUTPUT"
  exit 0
fi
printf 'username=discard-me\n'
exit 1
""",
    )
    _write_executable(
        bins / "id",
        """#!/usr/bin/env bash
[[ "$1" == -un ]]
printf '%s\n' "$CALLER_USER"
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bins}:{env['PATH']}",
            "BUBBLE_GIT_READ_TOKEN_RUN_DIR": str(run_dir),
            "SUDO_STDIN_LOG": str(stdin_log),
            "HELPER_OUTPUT": str(helper_output),
            "SUDO_MODE": "failure",
            "CALLER_USER": "agent-alpha",
        }
    )
    return env, run_dir, stdin_log, helper_output


def _run_wrapper(env: dict[str, str], action: str = "get"):
    protocol = "protocol=https\nhost=github.com\npath=Bubble-invest/repo.git\n\n"
    result = subprocess.run(
        [str(WRAPPER), action],
        env=env,
        input=protocol,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, protocol


def test_wrapper_falls_back_to_callers_token_and_forwards_stdin(wrapper_env):
    env, run_dir, stdin_log, _ = wrapper_env
    (run_dir / "token.alpha").write_text(TOKEN, encoding="utf-8")

    result, protocol = _run_wrapper(env)

    assert result.returncode == 0
    assert result.stdout == f"username=x-access-token\npassword={TOKEN}\n"
    assert result.stderr == ""
    assert stdin_log.read_text() == protocol


def test_wrapper_primary_password_response_is_passed_through_exactly(wrapper_env):
    env, run_dir, _, helper_output = wrapper_env
    env["SUDO_MODE"] = "success"
    primary = "username=primary\npassword=ghs_primary\nextra=unchanged\n\n"
    helper_output.write_text(primary, encoding="utf-8")
    (run_dir / "token.alpha").write_text(TOKEN, encoding="utf-8")

    result, _ = _run_wrapper(env)

    assert result.returncode == 0
    assert result.stdout == primary


def test_wrapper_emits_nothing_when_no_fallback_file(wrapper_env):
    env, _, _, _ = wrapper_env

    result, _ = _run_wrapper(env)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize("action", ["store", "erase"])
def test_wrapper_store_and_erase_are_noops(wrapper_env, action: str):
    env, _, stdin_log, _ = wrapper_env

    result, _ = _run_wrapper(env, action)

    assert result.returncode == 0
    assert result.stdout == ""
    assert not stdin_log.exists()


def test_scripts_have_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(REFRESHER), str(WRAPPER)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_systemd_templates_pin_refresh_schedule_and_failure_alert():
    service = (REPO_ROOT / "deploy/templates/bubble-git-read-token-refresh.service").read_text()
    timer = (REPO_ROOT / "deploy/templates/bubble-git-read-token-refresh.timer").read_text()
    dropin = (
        REPO_ROOT
        / "deploy/templates/onfailure-dropins/bubble-git-read-token-refresh.service.d/override.conf"
    ).read_text()

    assert "ExecStart=/usr/local/bin/bubble-git-read-token-refresh.sh" in service
    assert "OnBootSec=30s" in timer
    assert "OnUnitActiveSec=45min" in timer
    assert "OnFailure=cron-failure-alert@%n.service" in dropin
