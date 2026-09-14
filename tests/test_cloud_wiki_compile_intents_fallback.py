"""Behavioral tests for board #1339: cloud-wiki-compile.sh's operator-intents
mirror resolution must fall back gracefully (WARN + skip) instead of hard
FATAL-ing the whole nightly compile when the root-owned mirror
(`/opt/bubble-operator-intents`, or `$BUBBLE_OPERATOR_INTENTS_MIRROR`) is not
deployed — the #430 regression. It must also never substitute the writable
shared-wiki checkout for the mirror (#1267's invariant, independently
enforced by wiki_intent_audit.py's mirror validation and its own tests).

These tests extract the exact, unmodified control-flow out of the real
launcher script (by line-anchored slicing, no reimplementation) and execute
it with bash in a temp sandbox, so a regression in the shipped script — not
a copy of it — fails these tests.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPILE = REPO / "skills/cloud-wiki-compile/scripts/cloud-wiki-compile.sh"
SOURCE_LINES = COMPILE.read_text(encoding="utf-8").splitlines()


def _resolution_snippet() -> str:
    """The INTENTS_ROOT fallback-chain block: no exit/FATAL, ends at the
    first top-level (unindented) `fi`."""
    start = SOURCE_LINES.index('INTENTS_ROOT=""')
    end = next(i for i in range(start, len(SOURCE_LINES)) if SOURCE_LINES[i] == "fi")
    return "\n".join(SOURCE_LINES[start : end + 1]) + "\n"


def _audit_invocation_snippet() -> str:
    """The compile-mode block that either runs wiki_intent_audit.py against a
    resolved mirror, or writes the {"skipped": true} marker. Includes the
    unclosed `if [ "$MODE" = "compile" ]; then` wrapper — callers must close
    it with a trailing `fi`."""
    start = next(
        i for i, line in enumerate(SOURCE_LINES) if line.startswith("INTENT_AUDIT_SCRIPT=")
    )
    stop = next(
        i
        for i, line in enumerate(SOURCE_LINES)
        if line.strip() == 'if [ ! -f "$DELTA_SCRIPT" ]; then'
    )
    return "\n".join(SOURCE_LINES[start:stop]) + "\n"


RESOLUTION_SNIPPET = _resolution_snippet()
AUDIT_SNIPPET = _audit_invocation_snippet()

# Sanity: these anchors must still exist verbatim in the shipped script, or
# the extraction above is silently testing nothing.
assert 'BUBBLE_OPERATOR_INTENTS_MIRROR' in RESOLUTION_SNIPPET
assert '/opt/bubble-operator-intents' in RESOLUTION_SNIPPET
assert 'shared-wiki' not in RESOLUTION_SNIPPET  # never a fallback candidate (#1267)
assert '--intents-root "$INTENTS_ROOT"' in AUDIT_SNIPPET
assert '"skipped":true' in AUDIT_SNIPPET


HARNESS_PREFIX = """#!/usr/bin/env bash
set -uo pipefail
LOG_FILE="${LOG_FILE:?}"
log() { printf '%s\\n' "$*" >> "$LOG_FILE"; }
MODE="${MODE:-compile}"
"""


def _run(snippet: str, env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess:
    log_file = tmp_path / "log.txt"
    log_file.write_text("", encoding="utf-8")
    script = tmp_path / "harness.sh"
    script.write_text(HARNESS_PREFIX + snippet, encoding="utf-8")
    full_env = {**os.environ, **env, "LOG_FILE": str(log_file)}
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=full_env,
    )
    result.log = log_file.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    return result


# --------------------------------------------------------------------------
# (a) mirror present (via env override) -> resolved and used
# --------------------------------------------------------------------------
def test_env_override_mirror_is_resolved_when_present(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    (mirror / "operator-intents").mkdir(parents=True)
    result = _run(
        RESOLUTION_SNIPPET + '\necho "INTENTS_ROOT=$INTENTS_ROOT"\n',
        {"BUBBLE_OPERATOR_INTENTS_MIRROR": str(mirror)},
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert f"INTENTS_ROOT={mirror}" in result.stdout
    assert "WARN" not in result.log


# --------------------------------------------------------------------------
# (b) mirror absent, but the wiki's OWN copy of operator-intents exists ->
#     the wiki copy must NEVER be picked up (#1267); this degrades exactly
#     like "neither present": WARN + empty INTENTS_ROOT, never FATAL.
# --------------------------------------------------------------------------
def test_wiki_own_intents_copy_is_never_used_as_a_fallback(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    (wiki / "shared" / "operator-intents").mkdir(parents=True)
    (wiki / "shared" / "operator-intents" / "system-convergence.md").write_text(
        "---\ntitle: x\ncore: true\n---\n", encoding="utf-8"
    )
    result = _run(
        RESOLUTION_SNIPPET + '\necho "INTENTS_ROOT=[$INTENTS_ROOT]"\n',
        {"BUBBLE_OPERATOR_INTENTS_MIRROR": "", "WIKI_DIR": str(wiki)},
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "INTENTS_ROOT=[]" in result.stdout
    assert "WARN: no read-only operator-intents mirror found" in result.log


# --------------------------------------------------------------------------
# (c) neither the mirror nor a wiki copy exists -> WARN + empty
#     INTENTS_ROOT, resolution step exits 0, never FATAL.
# --------------------------------------------------------------------------
def test_no_source_at_all_warns_and_never_fatals(tmp_path: Path) -> None:
    result = _run(RESOLUTION_SNIPPET, {"BUBBLE_OPERATOR_INTENTS_MIRROR": ""}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "WARN: no read-only operator-intents mirror found" in result.log
    assert "FATAL" not in result.log


def test_skillsmith_mode_never_warns_about_intents(tmp_path: Path) -> None:
    """skillsmith never touches the wiki/intents; the pre-existing MODE guard
    must still suppress the WARN for it (unrelated to #1339, just don't break it)."""
    result = _run(RESOLUTION_SNIPPET, {"BUBBLE_OPERATOR_INTENTS_MIRROR": "", "MODE": "skillsmith"}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.log == ""


# --------------------------------------------------------------------------
# Audit-invocation block: resolved mirror -> the audit script runs; no
# mirror -> the audit script is never invoked and a {"skipped": true}
# marker is written instead. Neither path exits non-zero.
# --------------------------------------------------------------------------
def _fake_audit_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_wiki_intent_audit.py"
    script.write_text(
        "import json, sys\n"
        "args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
        "with open(args['--output'], 'w') as fh:\n"
        "    json.dump({'ok': True, 'intents_root': args['--intents-root']}, fh)\n",
        encoding="utf-8",
    )
    return script


def test_resolved_mirror_invokes_the_audit_script(tmp_path: Path) -> None:
    fake_audit = _fake_audit_script(tmp_path)
    report = tmp_path / "latest.json"
    intents_root = tmp_path / "mirror"
    intents_root.mkdir()
    snippet = AUDIT_SNIPPET.replace(
        "INTENT_AUDIT_SCRIPT=/home/claude/scripts/wiki-intent-audit.py",
        f"INTENT_AUDIT_SCRIPT={fake_audit}",
    ).replace(
        "INTENT_AUDIT_REPORT=/home/claude/monitoring/wiki-intent-audit/latest.json",
        f"INTENT_AUDIT_REPORT={report}",
    )
    snippet += "\nfi\n"  # close the extracted `if [ "$MODE" = "compile" ]; then`
    result = _run(
        snippet,
        {"INTENTS_ROOT": str(intents_root), "WIKI_DIR": str(tmp_path / "wiki")},
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written == {"ok": True, "intents_root": str(intents_root)}
    assert "FATAL" not in result.log


def test_unresolved_mirror_skips_audit_script_and_writes_marker(tmp_path: Path) -> None:
    sentinel = tmp_path / "audit_was_called"
    fake_audit = tmp_path / "fake_wiki_intent_audit.py"
    fake_audit.write_text(
        f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('called')\n",
        encoding="utf-8",
    )
    report = tmp_path / "latest.json"
    snippet = AUDIT_SNIPPET.replace(
        "INTENT_AUDIT_SCRIPT=/home/claude/scripts/wiki-intent-audit.py",
        f"INTENT_AUDIT_SCRIPT={fake_audit}",
    ).replace(
        "INTENT_AUDIT_REPORT=/home/claude/monitoring/wiki-intent-audit/latest.json",
        f"INTENT_AUDIT_REPORT={report}",
    )
    snippet += "\nfi\n"
    result = _run(
        snippet,
        {"INTENTS_ROOT": "", "WIKI_DIR": str(tmp_path / "wiki"), "TS": "2026-09-15T00:00:00Z"},
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists(), "audit script must never run without a resolved mirror"
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["skipped"] is True
    assert "FATAL" not in result.log
    assert "skipping intent audit" in result.log
