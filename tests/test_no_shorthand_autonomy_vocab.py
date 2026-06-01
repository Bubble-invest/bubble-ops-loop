"""
Surprise-2 guard — the shorthand autonomy vocabulary
(`shadow_autonomy`, `full_autonomy`) must NOT appear anywhere in the
project's production code, templates, docs, fixtures, or schemas.

Doctrine source: Notion v5 lines 256-260 enumerate the 5 OFFICIAL
autonomy modes used across the whole bubble-ops platform:
  - manual_required
  - manual_unless_policy_passed
  - auto_if_policy_passed
  - auto_with_veto_window
  - disabled

The shorthand `shadow_autonomy` / `full_autonomy` lost the doctrine's
nuance (each official mode encodes a specific human-AI control split)
and was used in early gate emissions (see /tmp/bubble-ops-fixture/
queues/gates/gate-notif-test-002.yaml). Mapping decided by Rick:
  - shadow_autonomy  -> auto_with_veto_window
                       (agent acts, human has a window to revert)
  - full_autonomy    -> auto_if_policy_passed
                       (no human gate, policy-gated only)

This test walks the entire project tree (excluding .git, .venv, test
files that legitimately discuss the shorthand for guard purposes, and
this file itself) and asserts ZERO matches.

Run:
    python3 -m pytest tests/test_no_shorthand_autonomy_vocab.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Files explicitly allowed to mention the shorthand (they exist precisely
# to forbid it). Paths are relative to PROJECT_ROOT.
ALLOWED_MENTION_FILES = {
    "tests/test_no_shorthand_autonomy_vocab.py",
    "tests/test_layer2_prompt_specifies_required_fields.py",
}

# Directories we skip entirely.
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    ".playwright-mcp",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "schemas-draft.bak-v1-20260520-1505",  # archived pre-v3 snapshot
}

# File extensions worth inspecting (text/code). We scan a broad set so
# new file types don't slip through; binary files are filtered by
# read errors and a heuristic.
SCAN_SUFFIXES = {
    ".py", ".md", ".yaml", ".yml", ".json", ".sh", ".toml", ".cfg",
    ".ini", ".txt", ".html", ".j2", ".template", ".jinja", ".jinja2",
}

SHORTHAND_PATTERN = re.compile(r"\b(shadow_autonomy|full_autonomy)\b")


def _iter_candidate_files() -> list[Path]:
    out: list[Path] = []
    for p in PROJECT_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in p.relative_to(PROJECT_ROOT).parts):
            continue
        if p.suffix not in SCAN_SUFFIXES:
            continue
        out.append(p)
    return out


def test_no_shorthand_autonomy_vocab_in_project() -> None:
    offenders: list[str] = []
    for path in _iter_candidate_files():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in ALLOWED_MENTION_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if SHORTHAND_PATTERN.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Found shorthand autonomy vocabulary (shadow_autonomy / "
        "full_autonomy) in the project. Replace with one of the 5 official "
        "modes from Notion v5 lines 256-260 (manual_required, "
        "manual_unless_policy_passed, auto_if_policy_passed, "
        "auto_with_veto_window, disabled).\n\nMapping per Rick's verdict:\n"
        "  shadow_autonomy -> auto_with_veto_window\n"
        "  full_autonomy   -> auto_if_policy_passed\n\nOffenders:\n  - "
        + "\n  - ".join(offenders)
    )
