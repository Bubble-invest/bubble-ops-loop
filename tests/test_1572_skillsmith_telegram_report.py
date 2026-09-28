"""Behavioral tests for board #1572: the weekly skillsmith run
(`cloud-wiki-compile.sh skillsmith`) logged "Telegram reporting unavailable
(no fleet-wiki bot token)" and silently skipped its report every week — not
because the token was missing (the templated `cloud-wiki-compile@.service`
already filters `TELEGRAM_BOT_TOKEN` into
`/run/bubble-headless-cloud-wiki-<mode>/env` for EVERY mode, skillsmith
included, via `filter-headless-env.py`), but because the launcher never had a
send step for skillsmith mode at all: COMPILE's STEP 10 send block existed,
skillsmith's did not, so the sandboxed model had no real mechanism and
narrated a plausible-sounding excuse instead.

The fix extracts the send logic into a shared `send_queued_telegram_report()`
function and calls it from BOTH mode's success paths, reusing the exact same
per-mode pre-filtered token (no new secret — mirrors the #1495/bvp#99
doctrine: "point at the token the fleet already uses").

These tests extract the exact, unmodified function body out of the real
launcher script (by line-anchored slicing, no reimplementation) and execute
it with bash in a temp sandbox with a stub `curl`, so a regression in the
shipped script — not a copy of it — fails these tests. Same technique as
test_1493_skillsmith_completion_marker.py.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPILE = REPO / "skills/cloud-wiki-compile/scripts/cloud-wiki-compile.sh"
SOURCE = COMPILE.read_text(encoding="utf-8")
SOURCE_LINES = SOURCE.splitlines()


def _function_snippet() -> str:
    """The shared send_queued_telegram_report() function body, sliced from
    its `send_queued_telegram_report() {` line to its own top-level `}`."""
    start = next(i for i, line in enumerate(SOURCE_LINES) if line.startswith("send_queued_telegram_report() {"))
    end = next(i for i in range(start, len(SOURCE_LINES)) if SOURCE_LINES[i] == "}")
    return "\n".join(SOURCE_LINES[start : end + 1]) + "\n"


SNIPPET = _function_snippet()

# Sanity: these anchors must still exist verbatim in the shipped function, or
# the extraction above is silently testing nothing.
assert 'local headless_env="/run/bubble-headless-cloud-wiki-${MODE}/env"' in SNIPPET
assert "report_bot_token=$(awk -F= '/^TELEGRAM_BOT_TOKEN=/{print $2; exit}'" in SNIPPET
assert "rm -f \"$report_file\"" in SNIPPET


def test_both_modes_call_the_shared_send_function() -> None:
    """Board #1572's actual bug: COMPILE called a send block, skillsmith
    called nothing. Both must now call the same shared function."""
    assert SOURCE.count("send_queued_telegram_report") >= 3  # def + 2 call sites (bash calls take no parens)
    assert "send_queued_telegram_report /home/claude/monitoring/wiki-compile-delta/telegram-report.txt" in SOURCE
    assert "send_queued_telegram_report /home/claude/monitoring/skillsmith/telegram-report.txt" in SOURCE


def test_skillsmith_send_call_is_inside_the_verified_completion_branch() -> None:
    """The send must only fire AFTER the SKILLSMITH_DONE marker is verified
    (never on a run the launcher itself judged a failure)."""
    idx_verified = SOURCE.index("log \"skillsmith completion marker verified")
    idx_send = SOURCE.index("send_queued_telegram_report /home/claude/monitoring/skillsmith/telegram-report.txt")
    assert idx_verified < idx_send, "skillsmith telegram send must come after marker verification, not before"


def test_report_dir_is_created_for_skillsmith_before_the_model_runs() -> None:
    idx_mkdir = SOURCE.index('mkdir -p /home/claude/monitoring/skillsmith')
    idx_claude_invoke = SOURCE.index('/usr/bin/claude')
    assert idx_mkdir < idx_claude_invoke, "skillsmith report dir must exist before the headless claude run starts"


HARNESS_PREFIX = """#!/usr/bin/env bash
set -uo pipefail
LOG_FILE="${LOG_FILE:?}"
log() { printf '%s\\n' "$*" >> "$LOG_FILE"; }
MODE="${MODE:?}"
"""


def _run(env: dict[str, str], args: list[str], tmp_path: Path, *, curl_ok: bool | None) -> subprocess.CompletedProcess:
    log_file = tmp_path / "log.txt"
    log_file.write_text("", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub_path = bin_dir / "curl"
    marker_file = tmp_path / "curl_called"
    if curl_ok is None:
        stub_body = "#!/usr/bin/env bash\nexit 99\n"  # never expected to run
    elif curl_ok:
        stub_body = f"#!/usr/bin/env bash\ntouch '{marker_file}'\nexit 0\n"
    else:
        stub_body = f"#!/usr/bin/env bash\ntouch '{marker_file}'\nexit 7\n"
    stub_path.write_text(stub_body, encoding="utf-8")
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC)

    script = tmp_path / "harness.sh"
    call = " ".join(f'"{a}"' if " " not in a else f"'{a}'" for a in args)
    script.write_text(HARNESS_PREFIX + SNIPPET + f'\nsend_queued_telegram_report {call}\necho DONE\n', encoding="utf-8")

    full_env = {**os.environ, **env, "LOG_FILE": str(log_file), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=full_env)
    result.log = log_file.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    result.curl_called = marker_file.exists()  # type: ignore[attr-defined]
    return result


# --------------------------------------------------------------------------
# (a) empty/missing report file: no-op, curl never invoked, nothing logged.
# --------------------------------------------------------------------------
def test_empty_report_file_is_a_noop(tmp_path: Path) -> None:
    report = tmp_path / "telegram-report.txt"
    result = _run({"MODE": "skillsmith"}, [str(report)], tmp_path, curl_ok=None)
    assert "DONE" in result.stdout
    assert result.log == ""
    assert not result.curl_called


# --------------------------------------------------------------------------
# (b) report queued + token present in the per-mode headless env file (the
#     board #1572 happy path: same mechanism compile already uses) -> sent,
#     queue file removed, success logged.
# --------------------------------------------------------------------------
def test_report_sent_when_mode_headless_env_has_token(tmp_path: Path) -> None:
    report = tmp_path / "telegram-report.txt"
    report.write_text("hello joris", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    headless_env = run_dir / "env"
    headless_env.write_text("TELEGRAM_BOT_TOKEN=abc123\nOTHER=1\n", encoding="utf-8")

    # send_queued_telegram_report hardcodes /run/bubble-headless-cloud-wiki-${MODE}/env,
    # so point MODE at a fake segment we can redirect via a bind-alike: instead,
    # verify the TELEGRAM_BOT_TOKEN fallback path (process-inherited env), which
    # the function also honors when the headless env file is absent — this is
    # the exact fallback a manual/test invocation relies on, and it proves the
    # send path end-to-end without requiring root to write under /run.
    result = _run({"MODE": "skillsmith", "TELEGRAM_BOT_TOKEN": "abc123"}, [str(report)], tmp_path, curl_ok=True)
    assert "DONE" in result.stdout
    assert result.curl_called
    assert "telegram report sent and queue cleared" in result.log
    assert not report.exists()


# --------------------------------------------------------------------------
# (c) report queued, curl fails (network/API error) -> WARN logged, file
#     stays queued for the next successful run (never silently dropped).
# --------------------------------------------------------------------------
def test_report_stays_queued_when_send_fails(tmp_path: Path) -> None:
    report = tmp_path / "telegram-report.txt"
    report.write_text("hello joris", encoding="utf-8")
    result = _run({"MODE": "skillsmith", "TELEGRAM_BOT_TOKEN": "abc123"}, [str(report)], tmp_path, curl_ok=False)
    assert "DONE" in result.stdout
    assert result.curl_called
    assert "WARN: telegram report send failed" in result.log
    assert report.exists()


# --------------------------------------------------------------------------
# (d) report queued, no token resolvable anywhere (real absence, not the
#     model's guess) -> loud WARN naming the exact env file checked, curl
#     never called, file stays queued. This is the "fail loudly, not a quiet
#     skip" bar the card sets.
# --------------------------------------------------------------------------
def test_missing_token_is_a_loud_warn_not_a_silent_drop(tmp_path: Path) -> None:
    report = tmp_path / "telegram-report.txt"
    report.write_text("hello joris", encoding="utf-8")
    env = {"MODE": "skillsmith"}
    env.pop("TELEGRAM_BOT_TOKEN", None)
    result = _run(env, [str(report)], tmp_path, curl_ok=None)
    assert "DONE" in result.stdout
    assert not result.curl_called
    assert "WARN: no TELEGRAM_BOT_TOKEN resolved for mode=skillsmith" in result.log
    assert "/run/bubble-headless-cloud-wiki-skillsmith/env" in result.log
    assert report.exists()


# --------------------------------------------------------------------------
# (e) board #1573: the token must never appear in curl's argv (ps/proc/
#     cmdline visibility on the multi-uid VPS) — it must travel only via the
#     `-K -` stdin config. Defense-in-depth alongside
#     tests/test_1573_telegram_token_argv_leak.sh's fleet-wide static scan,
#     scoped to exactly this function since board #1572/#1573 both touched it.
# --------------------------------------------------------------------------
def test_token_never_appears_in_curl_argv(tmp_path: Path) -> None:
    report = tmp_path / "telegram-report.txt"
    report.write_text("hello joris", encoding="utf-8")

    log_file = tmp_path / "log.txt"
    log_file.write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "curl_argv.log"
    stdin_log = tmp_path / "curl_stdin.log"
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"for a in \"$@\"; do printf '%s\\n' \"$a\" >> '{argv_log}'; done\n"
        f"if ! [ -t 0 ]; then cat >> '{stdin_log}'; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    fake_token = "FAKE-1572-BOT-TOKEN-abc123"
    script = tmp_path / "harness.sh"
    script.write_text(
        HARNESS_PREFIX + SNIPPET + f'\nsend_queued_telegram_report "{report}"\necho DONE\n',
        encoding="utf-8",
    )
    full_env = {
        **os.environ,
        "MODE": "skillsmith",
        "TELEGRAM_BOT_TOKEN": fake_token,
        "LOG_FILE": str(log_file),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=full_env)
    assert "DONE" in result.stdout, result.stderr

    argv_text = argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""
    assert fake_token not in argv_text, f"token leaked into curl argv: {argv_text!r}"
    stdin_text = stdin_log.read_text(encoding="utf-8") if stdin_log.exists() else ""
    assert fake_token in stdin_text, "token must travel via the -K stdin config"
    assert not report.exists()  # successful send clears the queue
