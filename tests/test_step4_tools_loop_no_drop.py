"""
test_step4_tools_loop_no_drop.py — Sprint correctif Fix 3.

QA-E2E 2026-05-20 finding: when the operator approves N tools in a row,
only N-1 are committed because `_derive_phase()` is re-called from
`next_prompt()` after the last approval and flips `_phase` to
`tools_more` while the queue was momentarily empty.

This test drives 4 tools through the loop with `approuve` x 4 + `non`
and asserts ALL 4 TOOL.md files are written + ALL 4 names appear in
dept.yaml.draft::tools (root-level per Fix 1).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
sys.path.insert(0, str(SKILL_ROOT))

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.skills_tools import _TOOLS_CATALOGUE


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "joris",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Drafting",
        "validated_steps": ["mandate", "missions", "layers"],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    """Seed a draft that already has 1 skill per layer (so we're past skills phase)."""
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "level": "ops",
            "status": "onboarding",
            "owner": "joris",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "forbidden": ["publier sans validation"],
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
    }, sort_keys=False), encoding="utf-8")
    # Seed PROMPT.md for layers
    for n in (1, 2, 3, 4):
        d = tmp_path / "layers" / str(n)
        d.mkdir(parents=True, exist_ok=True)
        (d / "PROMPT.md").write_text(
            f"# Layer {n}\n\n## Focalisation\n- skill need A\n- skill need B\n",
            encoding="utf-8",
        )
    return draft


def _drive_through_skills(runner) -> None:
    """Approve 1 skill per subscribed layer, close each layer, get to tools phase."""
    safety = 0
    while safety < 50:
        safety += 1
        p = runner.next_prompt()
        if p is None:
            return
        lp = p.lower()
        if "tool" in lp and "besoin" in lp:
            return  # entered tools phase
        if "autres" in lp and "skill" in lp:
            runner.on_answer("non")  # close layer
        elif "skill" in lp and "besoin" in lp:
            runner.on_answer("ok")
        else:
            runner.on_answer("approuve")


def test_four_tools_all_committed_after_approve_x4_then_non(tmp_path):
    """Walk into tools phase, then approve all tools in queue, then `non`.

    QA-E2E found that the LAST tool in the queue is silently dropped.
    """
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("skills_tools")
    runner.start(state, draft)
    _drive_through_skills(runner)

    # Now we should be at tools_needs phase. Confirm.
    p = runner.next_prompt()
    assert p is not None
    assert "tool" in p.lower(), f"expected tools-phase prompt, got: {p[:120]}"

    # Operator says "ok" to confirm the tools list.
    runner.on_answer("ok")

    # Now we're in tools_card. Count the catalogue + the in-flight tool.
    # The catalogue is _TOOLS_CATALOGUE.
    catalogue_size = len(_TOOLS_CATALOGUE)
    assert catalogue_size >= 2

    # Approve each tool exactly once.
    approved = 0
    safety = 0
    while safety < catalogue_size * 3:
        safety += 1
        p = runner.next_prompt()
        if p is None:
            break
        lp = p.lower()
        if "autres" in lp and "tool" in lp:
            # We reached the closing question — say non.
            runner.on_answer("non")
            break
        runner.on_answer("approuve")
        approved += 1

    # Assert all tools in the catalogue got TOOL.md on disk.
    tool_dirs = sorted((tmp_path / "tools").iterdir()) if (tmp_path / "tools").exists() else []
    tool_md_count = sum(
        1 for d in tool_dirs if (d / "TOOL.md").exists()
    )
    assert tool_md_count == catalogue_size, (
        f"Expected {catalogue_size} TOOL.md files, got {tool_md_count}. "
        f"The last tool was dropped (Fix 3 regression)."
    )

    # Assert dept.yaml.draft::tools (root) contains all catalogue names.
    body = yaml.safe_load(draft.read_text(encoding="utf-8"))
    tools_root = body.get("tools") or []
    assert len(tools_root) == catalogue_size, (
        f"Expected {catalogue_size} entries in tools, got {len(tools_root)}: "
        f"{tools_root}"
    )
    # Names match catalogue.
    expected_names = {t["name"] for t in _TOOLS_CATALOGUE}
    assert set(tools_root) == expected_names

    # Runner is done.
    assert runner.is_done() is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
