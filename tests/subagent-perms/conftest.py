"""
conftest.py — shared fixtures for the subagent-perms verification suite.

All paths absolute (per workspace convention).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# Make notion_v4_contract importable without packaging:
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from notion_v4_contract import NOTION_V4_SUBAGENT_CONTRACTS  # noqa: E402

# Pinned absolute path to the fixture repo (mirrors Step-5 convention).
FIXTURE_REPO = Path("/tmp/bubble-ops-fixture")
SUBAGENTS_DIR = FIXTURE_REPO / "subagents"

SUBAGENT_NAMES = ("data-curator", "task-orchestrator", "executor", "mandate-guardian")


def parse_tools_field(value: Any) -> set[str]:
    """Normalize a `tools:` / `disallowedTools:` YAML value to a set[str].

    Subagent frontmatter accepts either:
        tools: Read, Grep, Glob
    or
        tools:
          - Read
          - Grep

    Empty / None inputs yield an empty set.
    """
    if value is None or value == "":
        return set()
    if isinstance(value, str):
        return {t.strip() for t in value.split(",") if t.strip()}
    if isinstance(value, list):
        return {str(t).strip() for t in value if str(t).strip()}
    raise AssertionError(
        f"unrecognized tools-field shape: {type(value).__name__} {value!r}"
    )


def _load_subagent(name: str) -> dict[str, Any]:
    """Read /tmp/bubble-ops-fixture/subagents/<name>.md and split FM+body.

    Returns: {'frontmatter': dict, 'body': str, 'path': Path}
    """
    p = SUBAGENTS_DIR / f"{name}.md"
    assert p.is_file(), f"subagent file missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---"), (
        f"{p}: missing YAML frontmatter (must start with '---')"
    )
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"{p}: frontmatter missing closing '---'"
    fm = yaml.safe_load(parts[1])
    assert isinstance(fm, dict), f"{p}: frontmatter must parse to a dict, got {type(fm)}"
    body = parts[2]
    return {"frontmatter": fm, "body": body, "path": p}


@pytest.fixture
def subagent_file():
    """Factory fixture: subagent_file('executor') → {frontmatter, body, path}."""
    return _load_subagent


@pytest.fixture
def contract():
    """Factory fixture: contract('executor') → NOTION_V4_SUBAGENT_CONTRACTS['executor']."""

    def _get(name: str) -> dict[str, Any]:
        assert name in NOTION_V4_SUBAGENT_CONTRACTS, (
            f"no contract entry for {name!r}; "
            f"known: {sorted(NOTION_V4_SUBAGENT_CONTRACTS)}"
        )
        return NOTION_V4_SUBAGENT_CONTRACTS[name]

    return _get


@pytest.fixture
def all_subagents():
    """List of (name, loaded_dict) tuples for cross-subagent assertions."""
    return [(name, _load_subagent(name)) for name in SUBAGENT_NAMES]
