"""Pin the STRUCTURAL_PATH_GLOBS classification for mission-definition files.

Governance fix 2026-06-01: the structural list must lock the dept's top-level
mission entry-points (CLAUDE.md, MANDATE.md, skills_manifest.yaml, config.yaml,
gate_policy.yaml) in addition to the dir-based globs, so an agent cannot edit
its own mission. WORKING_MEMORY.md and whiteboard.yaml must stay WRITABLE.

If someone later edits policy.py and drops one of these, this test fails loud.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

POLICY_PY = Path(__file__).resolve().parent.parent / "src" / "policy.py"


def _load():
    spec = importlib.util.spec_from_file_location("bubble_broker_policy", str(POLICY_PY))
    m = importlib.util.module_from_spec(spec)
    sys.modules["bubble_broker_policy"] = m
    spec.loader.exec_module(m)
    return m


M = _load()


import pytest


@pytest.mark.parametrize(
    "path",
    [
        # top-level mission entry-points (the gap that let Tony self-edit)
        "CLAUDE.md",
        "MANDATE.md",
        "skills_manifest.yaml",
        "config.yaml",
        "gate_policy.yaml",
        "dept.yaml",
        # dir-based mission globs (must remain locked)
        "layers/1/PROMPT.md",
        "missions/morning_brief.yaml",
        "skills/foo/SKILL.md",
        "tools/bar.py",
        "subagents/x.md",
        "policies/kpis.yaml",
        "templates/t.yaml",
        ".claude/CLAUDE.md",
        ".claude/settings.json",
        ".claude/agents/a.md",
    ],
)
def test_mission_files_are_structural(path):
    assert M._is_structural(path) is True, f"{path} must be locked (structural)"


@pytest.mark.parametrize(
    "path",
    [
        # writable runtime / working memory — MUST NOT be locked
        "WORKING_MEMORY.md",
        "whiteboard.yaml",
        "outputs/2026-06-01/1/summary.md",
        "outputs/2026-06-01/heartbeat.log",
        "queues/management/directive-1.yaml",
        "inbox/decisions/d.yaml",
        "README.md",
        "kanban_queue.jsonl",
    ],
)
def test_runtime_and_working_memory_are_writable(path):
    assert M._is_structural(path) is False, f"{path} must stay writable (non-structural)"
