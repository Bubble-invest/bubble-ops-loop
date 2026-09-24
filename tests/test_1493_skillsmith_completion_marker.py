"""Behavioral tests for board #1493: skillsmith previously exited
0/is_error=false/subtype=success while it had never actually loaded the
skill-authoring skill (it replied "I don't see a 'skill-authoring' skill"),
and that silent no-op went undetected for weeks because the launcher treated
any zero exit as success.

The launcher (skills/cloud-wiki-compile/scripts/cloud-wiki-compile.sh) now
requires the exact SKILLSMITH_DONE:<date> completion marker (defined in
skills/skill-authoring/SKILL.md's "Completion marker" section) to be present
in the JSON result envelope's `result` text before a skillsmith run is
accepted; otherwise a "successful" exit is turned into a launcher failure
(EXIT=1), mirroring COMPILE mode's WIKI_COMPILE_RECEIPT two-phase watermark.

These tests extract the exact, unmodified guard block out of the real
launcher script (by line-anchored slicing, no reimplementation) and execute
it with bash in a temp sandbox, so a regression in the shipped script — not
a copy of it — fails these tests. Same technique as
test_cloud_wiki_compile_intents_fallback.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPILE = REPO / "skills/cloud-wiki-compile/scripts/cloud-wiki-compile.sh"
SOURCE_LINES = COMPILE.read_text(encoding="utf-8").splitlines()


def _completion_marker_snippet() -> str:
    """The skillsmith completion-marker guard block: starts at the '# Board
    #1493:' comment, ends at its own top-level (unindented) `fi`."""
    start = next(
        i for i, line in enumerate(SOURCE_LINES) if line.startswith("# Board #1493: skillsmith")
    )
    guard_if = next(
        i
        for i in range(start, len(SOURCE_LINES))
        if SOURCE_LINES[i].startswith('if [ "$MODE" = "skillsmith" ]')
    )
    end = next(i for i in range(guard_if, len(SOURCE_LINES)) if SOURCE_LINES[i] == "fi")
    return "\n".join(SOURCE_LINES[guard_if : end + 1]) + "\n"


SNIPPET = _completion_marker_snippet()

# Sanity: these anchors must still exist verbatim in the shipped script, or
# the extraction above is silently testing nothing.
assert 'SKILLSMITH_MARKER="SKILLSMITH_DONE:${DATE_STAMP}"' in SNIPPET
assert "marker in str(value.get(\"result\", \"\"))" in SNIPPET
assert 'log "skillsmith completion marker verified' in SNIPPET


HARNESS_PREFIX = """#!/usr/bin/env bash
set -uo pipefail
LOG_FILE="${LOG_FILE:?}"
log() { printf '%s\\n' "$*" >> "$LOG_FILE"; }
MODE="${MODE:?}"
DATE_STAMP="${DATE_STAMP:?}"
EXIT="${EXIT:?}"
RUN_LOG="${RUN_LOG:?}"
"""

HARNESS_SUFFIX = '\necho "FINAL_EXIT=$EXIT"\n'


def _run(env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess:
    log_file = tmp_path / "log.txt"
    log_file.write_text("", encoding="utf-8")
    script = tmp_path / "harness.sh"
    script.write_text(HARNESS_PREFIX + SNIPPET + HARNESS_SUFFIX, encoding="utf-8")
    full_env = {**os.environ, **env, "LOG_FILE": str(log_file)}
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=full_env,
    )
    result.log = log_file.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    return result


def _write_result_envelope(run_log: Path, *, is_error: bool, result_text: str) -> None:
    # Real transcripts have multiple JSON lines; the guard must find the LAST
    # one with type == "result" — mirror that shape here.
    run_log.write_text(
        json.dumps({"type": "system", "subtype": "init"})
        + "\n"
        + json.dumps({"type": "result", "subtype": "success", "is_error": is_error, "result": result_text})
        + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# (a) EXIT != 0 to start with: guard must be a no-op (never overrides/masks
#     an already-failed exit, never touches RUN_LOG).
# --------------------------------------------------------------------------
def test_nonzero_exit_is_left_untouched(tmp_path: Path) -> None:
    result = _run({"MODE": "skillsmith", "DATE_STAMP": "2026-09-24", "EXIT": "1", "RUN_LOG": str(tmp_path / "missing.log")}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "FINAL_EXIT=1" in result.stdout
    assert result.log == ""


# --------------------------------------------------------------------------
# (b) mode != skillsmith: guard must be a no-op regardless of RUN_LOG content
#     (this is the skillsmith-only guard; COMPILE has its own separate one).
# --------------------------------------------------------------------------
def test_other_modes_are_never_touched_by_this_guard(tmp_path: Path) -> None:
    result = _run({"MODE": "compile", "DATE_STAMP": "2026-09-24", "EXIT": "0", "RUN_LOG": str(tmp_path / "missing.log")}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "FINAL_EXIT=0" in result.stdout
    assert result.log == ""


# --------------------------------------------------------------------------
# (c) the exact #1493 incident, reproduced: exit 0, is_error=false,
#     subtype=success, but the result text never mentions the marker (the
#     model never loaded the skill and just asked a clarifying question).
#     Must now be turned into a launcher failure.
# --------------------------------------------------------------------------
def test_success_without_marker_is_turned_into_failure(tmp_path: Path) -> None:
    run_log = tmp_path / "run.log"
    _write_result_envelope(
        run_log,
        is_error=False,
        result_text="I don't see a \"skill-authoring\" skill in the available skills list... Could you clarify?",
    )
    result = _run({"MODE": "skillsmith", "DATE_STAMP": "2026-09-24", "EXIT": "0", "RUN_LOG": str(run_log)}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "FINAL_EXIT=1" in result.stdout
    assert "FATAL" in result.log
    assert "SKILLSMITH_DONE:2026-09-24" in result.log


# --------------------------------------------------------------------------
# (d) genuine success: marker present, is_error=false -> EXIT stays 0.
# --------------------------------------------------------------------------
def test_success_with_marker_present_is_accepted(tmp_path: Path) -> None:
    run_log = tmp_path / "run.log"
    _write_result_envelope(
        run_log,
        is_error=False,
        result_text="Authored 0, pruned-flagged 1.\nSKILLSMITH_DONE:2026-09-24",
    )
    result = _run({"MODE": "skillsmith", "DATE_STAMP": "2026-09-24", "EXIT": "0", "RUN_LOG": str(run_log)}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "FINAL_EXIT=0" in result.stdout
    assert "FATAL" not in result.log
    assert "skillsmith completion marker verified" in result.log


# --------------------------------------------------------------------------
# (e) marker present but for the WRONG date (e.g. a stale cached response,
#     or a run that spans midnight) -> must still fail; the date must match
#     exactly, not just "some SKILLSMITH_DONE".
# --------------------------------------------------------------------------
def test_marker_for_the_wrong_date_is_rejected(tmp_path: Path) -> None:
    run_log = tmp_path / "run.log"
    _write_result_envelope(run_log, is_error=False, result_text="SKILLSMITH_DONE:2026-09-17")
    result = _run({"MODE": "skillsmith", "DATE_STAMP": "2026-09-24", "EXIT": "0", "RUN_LOG": str(run_log)}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "FINAL_EXIT=1" in result.stdout
    assert "FATAL" in result.log


# --------------------------------------------------------------------------
# (f) marker present but is_error=true -> must still fail (claude can emit a
#     trailing marker-looking string even on a genuinely errored turn; the
#     is_error check must not be short-circuited by substring presence).
# --------------------------------------------------------------------------
def test_marker_present_but_is_error_true_is_rejected(tmp_path: Path) -> None:
    run_log = tmp_path / "run.log"
    _write_result_envelope(run_log, is_error=True, result_text="SKILLSMITH_DONE:2026-09-24")
    result = _run({"MODE": "skillsmith", "DATE_STAMP": "2026-09-24", "EXIT": "0", "RUN_LOG": str(run_log)}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "FINAL_EXIT=1" in result.stdout
    assert "FATAL" in result.log


# --------------------------------------------------------------------------
# (g) empty RUN_LOG (budget/limit exhaustion before any envelope was
#     written) -> FATAL, never silently accepted.
# --------------------------------------------------------------------------
def test_empty_run_log_is_rejected(tmp_path: Path) -> None:
    run_log = tmp_path / "empty.log"
    run_log.write_text("", encoding="utf-8")
    result = _run({"MODE": "skillsmith", "DATE_STAMP": "2026-09-24", "EXIT": "0", "RUN_LOG": str(run_log)}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "FINAL_EXIT=1" in result.stdout
    assert "FATAL: skillsmith produced no result envelope" in result.log


# --------------------------------------------------------------------------
# (h) malformed / no valid JSON result envelope -> FATAL, never silently
#     accepted (mirrors COMPILE's equivalent malformed-output guard).
# --------------------------------------------------------------------------
def test_malformed_run_log_is_rejected(tmp_path: Path) -> None:
    run_log = tmp_path / "garbage.log"
    run_log.write_text("not json at all\n{also not json\n", encoding="utf-8")
    result = _run({"MODE": "skillsmith", "DATE_STAMP": "2026-09-24", "EXIT": "0", "RUN_LOG": str(run_log)}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "FINAL_EXIT=1" in result.stdout
    assert "FATAL" in result.log
