"""Activation installs the operating SessionStart hook (BUG-HOOK fix).

Before this fix, activation only STRIPPED the onboarding hook — it never
installed an operating one. Maya ended activation with SessionStart pointing
at the deleted pre-rename path /home/claude/agents/<slug>/... → a dead no-op
hook, so she woke with no operating context and never re-observed
outputs/<today>/. This guards the install path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LIB = _HERE.parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import activate_runner  # noqa: E402


def _make_dept(tmp_path: Path, slug: str = "zoe") -> Path:
    repo = tmp_path / f"bubble-ops-{slug}"
    (repo / ".claude" / "hooks").mkdir(parents=True)
    # onboarding-style settings.json with the announce_current_step hook +
    # a stale pre-rename SessionStart command path.
    settings = {
        "hooks": {
            "SessionStart": [
                {"hooks": [
                    {"type": "command",
                     "command": f"/home/claude/agents/{slug}/.claude/hooks/announce_current_step.sh"},
                ]},
            ]
        }
    }
    (repo / ".claude" / "settings.json").write_text(
        json.dumps(settings, indent=2), encoding="utf-8")
    (repo / "CLAUDE.md").write_text("# onboarding\n", encoding="utf-8")
    return repo


def _dept_doc(slug: str = "zoe") -> dict:
    return {"department": {"slug": slug, "display_name": slug.capitalize(),
                           "level": "ops", "mandate": "test"}}


def test_flip_installs_operating_hook_at_canonical_path(tmp_path):
    slug = "zoe"
    repo = _make_dept(tmp_path, slug)
    activate_runner._flip_claude_md_to_operating(repo, _dept_doc(slug))

    # hook file exists + executable + operating-style content
    hook = repo / ".claude" / "hooks" / "session-start.sh"
    assert hook.exists(), "operating session-start.sh must be installed"
    assert hook.stat().st_mode & 0o111, "hook must be executable"
    body = hook.read_text(encoding="utf-8")
    assert "SessionStart" in body and "outputs/" in body
    assert f"bubble-ops-{slug}" in body, "hook must use the bubble-ops- path"

    # settings.json SessionStart points at the canonical bubble-ops path,
    # and the onboarding announce_current_step hook is gone.
    data = json.loads((repo / ".claude" / "settings.json").read_text())
    cmds = [
        h.get("command", "")
        for entry in data["hooks"]["SessionStart"]
        for h in entry.get("hooks", [])
    ]
    assert any(
        c == f"/home/claude/agents/bubble-ops-{slug}/.claude/hooks/session-start.sh"
        for c in cmds
    ), f"SessionStart must point at the canonical bubble-ops path; got {cmds}"
    assert not any("announce_current_step" in c for c in cmds), \
        "onboarding hook must be stripped"
    assert not any(f"/agents/{slug}/" in c for c in cmds), \
        "stale pre-rename path must be gone"


def test_flip_is_idempotent(tmp_path):
    slug = "zoe"
    repo = _make_dept(tmp_path, slug)
    activate_runner._flip_claude_md_to_operating(repo, _dept_doc(slug))
    first = (repo / ".claude" / "settings.json").read_text()
    activate_runner._flip_claude_md_to_operating(repo, _dept_doc(slug))
    second = (repo / ".claude" / "settings.json").read_text()
    assert first == second, "re-running flip must be a no-op"
