#!/usr/bin/env python3
"""
mission_scaffold.py - card #688 (DEPT FACTORY emits #642's frozen
conventions natively).

console/services/mission_pieces.py (PR#272, shipped 2026-07-16) is the
CONSUMER that the cockpit's layered clickable-piece view reads. Its
conventions, verified against the real fleet + its own docstrings:

  1. mission core   -> missions/<id>/PROMPT.md (preferred) or
                        missions/<id>.yaml (flat legacy layout).
  2. own skill       -> skills/<dashed-id>/SKILL.md
  3. own config      -> config/<mission-id-glossary-token>.yaml, resolved
                        by mission-id convention (verb-stripped tail /
                        individual underscore token, +"_sources"/"_topics"
                        suffix for gather_* missions) -- see
                        mission_pieces._mission_config_piece.
  4. own memory      -> memory/<id>.md (when input_sources declares
                        "<id>_memory" or the literal "working_memory").
  5. own voice       -> <channel>/VOICE.md when input_sources declares
                        "<channel>_voice".
  6. own output tile -> mission_pieces._output_piece reads output_queue +
                        creates[] directly off the mission dict -- no
                        file to scaffold, just requires both fields to be
                        present (already enforced by
                        recurring-mission.schema.yaml).
  7. pool/gate bands -> mission_pieces.resolve_layer_band /
                        resolve_gate_band read output_queue off L1/L2
                        missions + (optionally) docs/CONTEXT_POOL_SCHEMA.md.

Before PR#688, BOTH factory entry points that create a mission
(scripts/lib/scaffold.py's bootstrap skeleton and the Step-2
MissionsRunner's conversational "+ add mission" flow) wrote only a bare
`missions/<id>.yaml` -- none of the richer conventions above. A newly
hatched dept therefore rendered the piece view's leanest possible
degradation (reference chips only) until a human manually retrofitted
missions/<id>/PROMPT.md + config/ + memory/ + skills/ by hand, exactly
the "retrofit, not native" gap #688 exists to close.

This module is the SINGLE shared emitter both factory entry points call.
It is deliberately generic (mirrors the resolver's own doctrine: no
hardcoded per-dept file map) -- it derives every path from the mission
dict + the dept-glossary conventions the resolver itself already
encodes, so widening this emitter widens both factory entry points at
once, by construction.

Never raises on a best-effort piece: a piece with nothing meaningful to
write (e.g. a mission with no input_sources at all) is simply skipped,
mirroring mission_pieces.py's own "degrade to a non-clickable piece"
doctrine on the read side.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# Kept in exact lockstep with mission_pieces._MISSION_VERB_PREFIXES /
# _mission_config_piece's candidate-generation order (console/services/
# mission_pieces.py) -- the WRITE side must offer the file at the same
# candidate the READ side tries first, or the emitted config file
# silently never resolves. See that module's docstring for the full
# per-shape verification (draft_x -> config/x.yaml, gather_x_timeline ->
# config/x_sources.yaml, etc).
_MISSION_VERB_PREFIXES = ("gather_", "draft_", "synthesizing_", "publish_")


def _dehyphenate(key: str) -> str:
    return key.replace("_", "-")


def _mission_config_glossary_name(mission_id: str) -> str:
    """Return the config/<name>.yaml candidate this emitter WRITES for a
    given mission id -- the first candidate mission_pieces._mission_config_
    piece would also try, so the emitted file resolves on the very first
    lookup (whole id / verb-stripped tail, +"_sources" suffix for
    gather_* per the resolver's verified per-shape convention)."""
    stripped = mission_id
    for prefix in _MISSION_VERB_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    name = stripped or mission_id
    if mission_id.startswith("gather_"):
        return f"{name}_sources"
    return name


def _write(path: Path, content: str) -> None:
    if path.exists():
        return  # never clobber an operator-edited file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def render_mission_prompt_md(mission: Dict[str, Any], slug: str, display_name: str) -> str:
    """Render missions/<id>/PROMPT.md -- the mission-core piece
    mission_pieces._mission_core_piece resolves FIRST (before the flat-
    yaml fallback). Carries the same fields as the flat missions/<id>.yaml
    so nothing is lost, in a human-readable card the piece view's
    mission-core tile opens directly."""
    mission_id = str(mission.get("id") or "mission")
    description = str(mission.get("description") or "").strip()
    cadence = mission.get("cadence", "?")
    layer = mission.get("layer", "?")
    output_queue = mission.get("output_queue", "?")
    creates = mission.get("creates") or []
    input_sources = mission.get("input_sources") or []
    lines: List[str] = [
        f"# Mission `{mission_id}` — {display_name}",
        "",
        description or "(description TBD)",
        "",
        f"- Cadence : `{cadence}`",
        f"- Layer : {layer}",
        f"- Output queue : `{output_queue}`",
    ]
    if creates:
        lines.append(f"- Creates : `{', '.join(str(c) for c in creates)}`")
    if input_sources:
        rendered_sources = ", ".join(
            s if isinstance(s, str) else str(s) for s in input_sources
        )
        lines.append(f"- Input sources : `{rendered_sources}`")
    lines.append("")
    return "\n".join(lines)


def render_config_yaml(mission_id: str) -> str:
    return (
        f"# {mission_id} — runtime-editable config registry\n"
        "# Generated by the dept factory (card #688) as the mission's\n"
        "# `own_config` piece (console/services/mission_pieces.py::"
        "_mission_config_piece).\n"
        "# This file is the dept's to own/edit going forward (config/ is\n"
        "# runtime, not structural) -- fill in with the mission's real\n"
        "# source registry.\n"
        "sources: []\n"
    )


def render_memory_md(mission_id: str, display_name: str) -> str:
    return (
        f"# {mission_id} — mission memory\n\n"
        f"Per-mission working memory for {display_name}'s `{mission_id}` "
        "mission (console/services/mission_pieces.py::_memory_piece).\n"
        "Append durable learnings here across runs; this file is read at\n"
        "the start of every tick that touches this mission.\n"
    )


def render_voice_md(channel: str, display_name: str) -> str:
    return (
        f"# {channel} — voice guide\n\n"
        f"Tone + style guide for {display_name}'s `{channel}` channel "
        "(console/services/mission_pieces.py::_voice_piece).\n"
        "Fill in with the channel's real voice notes.\n"
    )


def render_skill_md(skill_name: str, mission_id: str) -> str:
    return (
        f"# {skill_name}\n\n"
        f"Skill stub for mission `{mission_id}`, auto-scaffolded by the "
        "dept factory (card #688) so the mission's own-skill piece "
        "(console/services/mission_pieces.py::_mission_skill_piece) "
        "resolves natively. Replace this stub with the skill's real "
        "instructions.\n"
    )


def render_pool_schema_md(display_name: str) -> str:
    return (
        f"# {display_name} — context pool schema\n\n"
        "Documents the shape of the items this dept's Layer-1 missions "
        "drop into `queues/research/` (the POOL), read by "
        "console/services/mission_pieces.py::resolve_layer_band as the "
        "pool-band's schema deep-link.\n\n"
        "## Fields\n\n"
        "- `id` — unique item id\n"
        "- `kind` — the queue-item kind (matches a mission's `creates[]`)\n"
        "- `payload` — mission-specific body\n"
    )


def scaffold_mission_pieces(
    root: Path, mission: Dict[str, Any], slug: str, display_name: str,
) -> List[Path]:
    """Emit every #642-convention file this mission's declared shape
    implies, so the cockpit's layered piece view renders it natively
    (card #688). Idempotent: never overwrites a file that already exists
    (an operator's own edits always win). Never raises -- an
    unresolvable piece (e.g. a `<channel>_voice` key with no comprehensible
    channel name) is silently skipped, mirroring the resolver's own
    degrade-gracefully doctrine.

    Returns the list of paths written (for tests / commit messages).
    """
    root = Path(root)
    mission_id = str(mission.get("id") or "")
    if not mission_id:
        return []

    written: List[Path] = []

    # 1. missions/<id>/PROMPT.md — the mission-core piece, preferred
    #    resolution over the flat missions/<id>.yaml (mission_pieces.py
    #    tries this path FIRST).
    prompt_path = root / "missions" / mission_id / "PROMPT.md"
    if not prompt_path.exists():
        _write(prompt_path, render_mission_prompt_md(mission, slug, display_name))
        written.append(prompt_path)

    # 2. skills/<dashed-id>/SKILL.md — the mission's own-skill piece
    #    (mission_pieces._mission_skill_piece).
    dashed = _dehyphenate(mission_id)
    skill_path = root / "skills" / dashed / "SKILL.md"
    if not skill_path.exists():
        _write(skill_path, render_skill_md(dashed, mission_id))
        written.append(skill_path)

    # 3. config/<glossary-name>.yaml — the mission's own-config piece
    #    (mission_pieces._mission_config_piece), only when the mission
    #    actually declares input/config-shaped state worth a registry
    #    (i.e. it has input_sources at all -- a mission with none has
    #    nothing to configure).
    input_sources = mission.get("input_sources") or []
    if isinstance(input_sources, list) and input_sources:
        config_name = _mission_config_glossary_name(mission_id)
        config_path = root / "config" / f"{config_name}.yaml"
        if not config_path.exists():
            _write(config_path, render_config_yaml(mission_id))
            written.append(config_path)

    # 4. memory/<id>.md — only when the mission actually declares a
    #    memory-shaped input_sources key (mission_pieces._memory_piece:
    #    "<id>_memory" or the literal "working_memory").
    wants_memory = any(
        isinstance(k, str) and (k == "working_memory" or k.endswith("_memory") or k == f"{mission_id}_memory")
        for k in input_sources
    )
    if wants_memory:
        memory_path = root / "memory" / f"{mission_id}.md"
        if not memory_path.exists():
            _write(memory_path, render_memory_md(mission_id, display_name))
            written.append(memory_path)

    # 5. <channel>/VOICE.md — only for input_sources keys shaped
    #    "<channel>_voice" (mission_pieces._voice_piece).
    for key in input_sources:
        if not isinstance(key, str) or not key.endswith("_voice"):
            continue
        channel = key[: -len("_voice")]
        if not re.match(r"^[a-z][a-z0-9_-]*$", channel):
            continue  # not a plausible channel dirname; skip rather than guess
        voice_path = root / channel / "VOICE.md"
        if not voice_path.exists():
            _write(voice_path, render_voice_md(channel, display_name))
            written.append(voice_path)

    return written


def scaffold_pool_schema(root: Path, display_name: str) -> Optional[Path]:
    """Emit docs/CONTEXT_POOL_SCHEMA.md — the pool-band's optional schema
    deep-link (mission_pieces.resolve_layer_band). Idempotent; returns the
    path written, or None if it already existed."""
    root = Path(root)
    path = root / "docs" / "CONTEXT_POOL_SCHEMA.md"
    if path.exists():
        return None
    _write(path, render_pool_schema_md(display_name))
    return path
