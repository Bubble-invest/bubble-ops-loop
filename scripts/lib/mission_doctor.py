#!/usr/bin/env python3
"""
mission_doctor.py — build-time / CI lint for the loop-mission ⇄ skill contract.

Board #1051 (L1 `gather_internal_work` blocked): a loop mission runs in a
**stateless subagent** (see `layer_templates._COMMON_HEADER` — every L1..L4
prompt is spawned as a fresh subagent with no session context). When such a
subagent tries to use a Skill whose `SKILL.md` frontmatter declares
`disable-model-invocation: true`, the Skill tool REFUSES the call (that flag
means "only a human/programmatic caller may invoke this, never the model") —
and hand-rolling the skill's behaviour is forbidden by the skill's own
guardrail. Live result (content dept, 2026-08-28): `gather_internal_work`
called `research-internal-work` (disable-model-invocation), the subagent
could not invoke it, and the mission wrote an EMPTY `internal.yaml`
(`signal_status: skill_blocked`) every single L1 tick — a recurring silent
failure that no marker/dispatch fix can cure, because the mission simply
cannot do its job as configured.

The real fixes are dept-side (re-scope the mission to a caller where the
skill is invocable, OR drop `disable-model-invocation` from the skill's
frontmatter). This module is the FRAMEWORK's half: a cheap, generic
detector that surfaces the whole class at build/onboarding/CI time instead
of once-per-tick at runtime. `dept-spawner` / the onboarding guide (or a
`tools/detect_*` health cron, mirroring `tools/detect_auth_dead_agents.py`)
can call `find_uninvokable_skill_missions(repo_dir)` and fail/warn when a
dept ships a mission that references a model-un-invokable skill.

Deliberately generic (no hardcoded mission/skill names): it derives the
mission→skill edges from the same conventions the cockpit resolver
(`console/services/mission_pieces.py`) already uses — `input_sources`
skill keys, the own-skill naming convention, and skills named in the
mission's free-text description — so it stays in lockstep with what the
fleet actually treats as "a mission's skills".

Never raises on a malformed dept.yaml / SKILL.md: an unreadable file simply
contributes no edge (mirroring the resolver's degrade-gracefully doctrine).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

# Loop layers all run as stateless subagents (layer_templates._COMMON_HEADER),
# so a disable-model-invocation skill is un-invokable on ANY of them. Kept as a
# set so a future non-subagent execution path (if one is ever added) can be
# excluded here without touching the detection logic.
_SUBAGENT_LAYERS = frozenset({1, 2, 3, 4})

# The frontmatter keys that mean "the model may not invoke this skill". Both
# the hyphenated form (the canonical SKILL.md frontmatter key) and the
# underscored form are accepted, since YAML authors use either.
_DISABLE_KEYS = ("disable-model-invocation", "disable_model_invocation")


def _dehyphenate(mission_id: str) -> str:
    """`gather_internal_work` -> `gather-internal-work` (the own-skill dir
    convention, mirroring mission_scaffold._dehyphenate /
    mission_pieces._mission_skill_piece)."""
    return mission_id.replace("_", "-")


def _skill_names(repo_dir: Path) -> "set[str]":
    """Every skill dir name that actually has a SKILL.md on disk."""
    skills_dir = repo_dir / "skills"
    if not skills_dir.is_dir():
        return set()
    return {
        d.name
        for d in skills_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    }


def _referenced_skill_names(
    mission: Dict[str, Any], available: "set[str]"
) -> "set[str]":
    """The skill dir names THIS mission references, restricted to skills that
    exist on disk (`available`). Union of three resolver conventions:

      1. `input_sources` entries whose (hyphen-normalised) value names a skill
         dir (mission_pieces._skill_piece).
      2. the own-skill naming convention `skills/<dashed-mission-id>/`
         (mission_pieces._mission_skill_piece).
      3. skill dir names mentioned whole-word in the mission's free-text
         `description` (mission_pieces._description_mentioned_skills).
    """
    refs: "set[str]" = set()

    # 1. input_sources skill keys (accept both dashed and underscored forms).
    for key in mission.get("input_sources") or []:
        if not isinstance(key, str):
            continue
        for cand in (key, key.replace("_", "-")):
            if cand in available:
                refs.add(cand)

    # 2. own-skill by naming convention.
    own = _dehyphenate(str(mission.get("id") or ""))
    if own in available:
        refs.add(own)

    # 3. skills named in the description (whole-word match, same guard as the
    #    cockpit resolver so ordinary prose can't accidentally match).
    description = str(mission.get("description") or "")
    if description:
        for name in available:
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", description):
                refs.add(name)

    return refs


def _skill_disables_invocation(skill_md: Path) -> bool:
    """True iff `skill_md`'s YAML frontmatter sets a disable-model-invocation
    flag truthy. A file with no frontmatter / unreadable → False (no edge)."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return False
    fm = _parse_frontmatter(text)
    for k in _DISABLE_KEYS:
        v = fm.get(k)
        if v is True or (isinstance(v, str) and v.strip().lower() == "true"):
            return True
    return False


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    """Parse a leading `---\\n...\\n---` YAML frontmatter block. Returns {} when
    there is none or it is unparseable / not a mapping."""
    if not text.startswith("---"):
        return {}
    # Split on the closing fence: lines[0] is the opening '---'.
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    block = "\n".join(lines[1:end])
    try:
        data = yaml.safe_load(block) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def find_uninvokable_skill_missions(repo_dir: "Path | str") -> List[Dict[str, str]]:
    """Return every (mission, skill) edge where a stateless-subagent loop
    mission references a Skill whose SKILL.md declares disable-model-invocation
    — i.e. the mission cannot invoke that skill at runtime (board #1051).

    Each hit: ``{"mission_id", "layer", "skill", "skill_path"}`` (layer as a
    str for stable serialisation). Sorted by (mission_id, skill) for
    deterministic output. Empty list when the dept is clean or has no
    dept.yaml / skills.
    """
    repo = Path(repo_dir)
    dept_yaml = repo / "dept.yaml"
    if not dept_yaml.is_file():
        return []
    try:
        dept = yaml.safe_load(dept_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(dept, dict):
        return []

    available = _skill_names(repo)
    if not available:
        return []

    # Cache the per-skill invocability verdict so a skill referenced by many
    # missions is read once.
    disabled: Dict[str, bool] = {}

    hits: List[Dict[str, str]] = []
    for m in dept.get("recurring_missions") or []:
        if not isinstance(m, dict):
            continue
        try:
            layer = int(m.get("layer", 0))
        except (TypeError, ValueError):
            continue
        if layer not in _SUBAGENT_LAYERS:
            continue
        mid = str(m.get("id") or "")
        if not mid:
            continue
        for skill in sorted(_referenced_skill_names(m, available)):
            if skill not in disabled:
                disabled[skill] = _skill_disables_invocation(
                    repo / "skills" / skill / "SKILL.md"
                )
            if disabled[skill]:
                hits.append({
                    "mission_id": mid,
                    "layer": str(layer),
                    "skill": skill,
                    "skill_path": f"skills/{skill}/SKILL.md",
                })

    hits.sort(key=lambda h: (h["mission_id"], h["skill"]))
    return hits


def _main(argv: "list[str]") -> int:
    """CLI: `python3 -m scripts.lib.mission_doctor [repo_dir]` — prints the
    offending mission→skill edges and exits non-zero if any are found (so a
    CI / onboarding step can gate on it)."""
    repo = argv[1] if len(argv) > 1 else "."
    hits = find_uninvokable_skill_missions(repo)
    if not hits:
        print(f"[mission-doctor] OK — no un-invokable skill dependencies in {repo!r}")
        return 0
    print(f"[mission-doctor] {len(hits)} un-invokable skill dependency(ies) in {repo!r}:")
    for h in hits:
        print(
            f"  - mission {h['mission_id']!r} (L{h['layer']}) references "
            f"{h['skill_path']} which sets disable-model-invocation — a "
            f"stateless L{h['layer']} subagent cannot invoke it (#1051)."
        )
    return 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
