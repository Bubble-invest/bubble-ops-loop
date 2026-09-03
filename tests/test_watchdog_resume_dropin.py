"""Tests for deploy/bin/bubble-watchdog-resume-dropin (board #1100).

This is the ROOT-OWNED helper the telegram-watchdog calls on a recovery to
install a TRANSIENT systemd drop-in that relaunches the dept with
`--continue --fork-session` so the session RESUMES instead of starting blank.
(ops-loop ships a byte-identical copy of the helper that bubble-vps-platform
manages; both install it to /usr/local/bin/bubble-watchdog-resume-dropin.)

Board #1100 fixed two bugs: the drop-in used to HARDCODE
    /usr/bin/script -qfc "/usr/bin/claude … --model \"opus[1m]\" …" /dev/null
which (1) relaunched the dept under `script` — silently reverting the #1097
dtach interactive-terminal durability on any watchdog recovery (dept came back
on a NON-attachable pty) — and (2) pinned a drifting short model id `opus[1m]`.

The fix DERIVES the drop-in ExecStart from the unit's OWN ExecStart (its
root-owned fragment file) and injects only the resume flags after the claude
binary, so the drop-in inherits the unit's dtach launcher, socket, --model and
--channels verbatim.

These tests EXECUTE the real helper in a bash subprocess against a fake
`systemctl` + a fixture unit fragment (via the helper's test-only env seams
BUBBLE_WATCHDOG_SYSTEMCTL / BUBBLE_WATCHDOG_DROPIN_ROOT — inert in production,
where sudo's env_reset strips them). No root or live systemd needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "deploy" / "bin" / "bubble-watchdog-resume-dropin"

_OPS_LOOP_FRAGMENT = (
    "[Service]\n"
    "ExecStart=\n"
    "ExecStart=/bin/sh -c 'exec /usr/bin/dtach -N /run/ops-loop-maya/dtach.sock "
    "/bin/sh -c \"/usr/bin/claude --model \\\"${CLAUDE_MODEL}\\\" "
    "--dangerously-skip-permissions --channels plugin:telegram@claude-plugins-official\"'\n"
)


def _fake_systemctl(tmp_path: Path, service: str, fragment: Path) -> Path:
    sc = tmp_path / "systemctl"
    sc.write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "$1" == "show" && "$2" == "{service}" && "$4" == "FragmentPath" ]]; '
        f'then echo "{fragment}"; exit 0; fi\n'
        'if [[ "$1" == "daemon-reload" ]]; then exit 0; fi\n'
        'echo "fake-systemctl: unhandled $*" >&2; exit 9\n',
        encoding="utf-8",
    )
    sc.chmod(0o755)
    return sc


def _install(tmp_path: Path, service: str, fragment_text: str) -> subprocess.CompletedProcess:
    frag = tmp_path / service
    frag.write_text(fragment_text, encoding="utf-8")
    systemctl = _fake_systemctl(tmp_path, service, frag)
    dropin_root = tmp_path / "sysd"
    dropin_root.mkdir()
    return subprocess.run(
        ["bash", str(HELPER), "install", service],
        capture_output=True, text=True, timeout=10,
        env={
            "PATH": "/usr/bin:/bin",
            "BUBBLE_WATCHDOG_SYSTEMCTL": str(systemctl),
            "BUBBLE_WATCHDOG_DROPIN_ROOT": str(dropin_root),
        },
    )


def _install_ok(tmp_path: Path, service: str, fragment_text: str) -> str:
    res = _install(tmp_path, service, fragment_text)
    assert res.returncode == 0, f"install failed: rc={res.returncode} stderr={res.stderr!r}"
    dropin = tmp_path / "sysd" / f"{service}.d" / "zz-watchdog-resume.conf"
    assert dropin.is_file(), "resume drop-in was not written."
    return dropin.read_text(encoding="utf-8")


def _exec_line(dropin: str) -> str:
    return [l for l in dropin.splitlines() if l.startswith("ExecStart=/bin")][0]


def test_helper_exists_and_is_executable():
    assert HELPER.is_file(), f"missing helper: {HELPER}"


def test_dropin_uses_dtach_not_script(tmp_path):
    """#1100: the drop-in must relaunch under dtach (inherited from the unit),
    NOT the old `script -qfc`."""
    dropin = _install_ok(tmp_path, "ops-loop-maya.service", _OPS_LOOP_FRAGMENT)
    assert "/usr/bin/dtach -N" in _exec_line(dropin)
    assert "script -qfc" not in dropin, (
        "drop-in must NOT use `script -qfc` — it reverts the #1097 dtach "
        "durability on a watchdog recovery."
    )


def test_dropin_socket_matches_unit(tmp_path):
    dropin = _install_ok(tmp_path, "ops-loop-maya.service", _OPS_LOOP_FRAGMENT)
    assert "/run/ops-loop-maya/dtach.sock" in _exec_line(dropin)


def test_dropin_injects_resume_flags_after_claude(tmp_path):
    dropin = _install_ok(tmp_path, "ops-loop-maya.service", _OPS_LOOP_FRAGMENT)
    assert "/usr/bin/claude --continue --fork-session" in _exec_line(dropin)
    assert "\nExecStart=\nExecStart=/bin/sh" in dropin


def test_dropin_no_hardcoded_model_inherits_unit_model(tmp_path):
    """#1100: no hardcoded model id — the unit's own ${CLAUDE_MODEL} is inherited
    verbatim, never the drifting `opus[1m]` literal."""
    dropin = _install_ok(tmp_path, "ops-loop-maya.service", _OPS_LOOP_FRAGMENT)
    assert "opus[1m]" not in dropin
    assert "${CLAUDE_MODEL}" in dropin


def test_dropin_allowlists_service_name(tmp_path):
    res = subprocess.run(
        ["bash", str(HELPER), "install", "evil.service"],
        capture_output=True, text=True, timeout=10,
        env={"PATH": "/usr/bin:/bin",
             "BUBBLE_WATCHDOG_DROPIN_ROOT": str(tmp_path / "sysd")},
    )
    assert res.returncode == 2, f"expected allowlist refusal (exit 2), got {res.returncode}"


def test_dropin_fails_closed_when_no_claude_binary(tmp_path):
    """Fail-closed (must not weaken recovery): no claude binary to inject into
    → refuse (non-zero), write nothing."""
    frag_text = (
        "[Service]\n"
        "ExecStart=/bin/sh -c 'exec /usr/bin/dtach -N /run/ops-loop-x/dtach.sock "
        "/bin/sh -c \"/usr/bin/not-claude --foo\"'\n"
    )
    res = _install(tmp_path, "ops-loop-x.service", frag_text)
    assert res.returncode != 0
    assert not (tmp_path / "sysd" / "ops-loop-x.service.d" / "zz-watchdog-resume.conf").exists()
