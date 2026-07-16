"""
mission_pieces.py — piece-resolution service for card #642 (layered
clickable-piece view). Maps a mission's declared `input_sources` logical
keys (plus its own id) to openable repo files, best-effort, generic
across every dept — never a hardcoded per-dept file map.

Origin: `Rick_RnD/projects/miranda-mission-map/build_rebuild_html.py`
prototypes this exact idea via a hand-written `FILEMAP` — 100+ lines of
Miranda-specific hand-mapping. That's the anti-pattern this module
replaces: `resolve_mission_pieces` reads live `input_sources` + disk
state and self-adapts to whatever shape a dept actually has (content's
full 6-piece taxonomy down to accountant's inline-only missions — see
PLAN-642 §5's per-dept shape matrix).

Never raises: every resolution step degrades to a non-clickable /
omitted piece rather than an exception, mirroring the rest of
github_reader's disk-read helpers (load_mandate_md et al).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from console.services.dept_registry import repo_path

# The 6-glyph taxonomy (Miranda prototype's GLYPH, ported verbatim — the
# reusable vocabulary per PLAN-642 §1). Depts whose input_sources keys
# resolve to none of these (ben's `broker_apis`, maya's `pool_db`, …)
# fall through to the neutral "reference" chip (§0-bis refinement 3).
PIECE_GLYPH: Dict[str, str] = {
    "mission": "📄",
    "skill": "🎓",
    "tool": "🔧",
    "config": "📇",
    "memory": "🧠",
    "voice": "🎙️",
    "reference": "",  # muted chip, no glyph — deliberately not part of the 6
}

PIECE_LABEL: Dict[str, str] = {
    "mission": "mission",
    "skill": "compétence",
    "tool": "outil",
    "config": "configuration",
    "memory": "mémoire",
    "voice": "voix",
    "reference": "source",
}

# Convention: L1 fetch tools live under scripts/lib/fetch_*.py (content) or
# tools/ (accountant, per PLAN-642 §2.6). Tried in this order.
_TOOL_SEARCH_DIRS = ("scripts/lib", "tools")


def _norm(key: str) -> str:
    """Normalize a logical input_sources key for file-name matching:
    hyphen<->underscore is the one ambiguity seen across depts (mission
    ids use underscores, skills/ dirs use hyphens — same convention
    _normalize_filename_mission_id already resolves for mission ids)."""
    return key.replace("-", "_")


def _dehyphenate(key: str) -> str:
    return key.replace("_", "-")


def _mission_core_piece(root: Path, mission_id: str, mission: Dict[str, Any]) -> Dict[str, Any]:
    """Resolution rule 1 (§2.1): missions/<id>/PROMPT.md first (content),
    else missions/<id>.yaml (ben/maya/tony/cgp flat layout), else an
    inline non-clickable tile showing the mission's own description."""
    prompt_path = f"missions/{mission_id}/PROMPT.md"
    if (root / prompt_path).is_file():
        return {"kind": "mission", "label": mission_id, "key": mission_id,
                "rel_path": prompt_path, "clickable": True}
    yaml_path = f"missions/{mission_id}.yaml"
    if (root / yaml_path).is_file():
        return {"kind": "mission", "label": mission_id, "key": mission_id,
                "rel_path": yaml_path, "clickable": True}
    return {"kind": "mission", "label": mission_id, "key": mission_id,
            "rel_path": None, "clickable": False}


def _skill_piece(root: Path, key: str) -> Optional[Dict[str, Any]]:
    """Rule 2 (§2.2): input_sources key matches skills/<name>/SKILL.md,
    normalizing _<->- (skills/ dirs are hyphenated, keys are usually
    underscored)."""
    for candidate in (key, _norm(key), _dehyphenate(key)):
        rel = f"skills/{candidate}/SKILL.md"
        if (root / rel).is_file():
            return {"kind": "skill", "label": candidate, "key": key,
                     "rel_path": rel, "clickable": True}
    return None


def _mission_skill_piece(root: Path, mission_id: str) -> Optional[Dict[str, Any]]:
    """Rule 2's second clause: also resolve the mission's OWN skill by
    naming convention (draft_x -> skills/draft-x/SKILL.md), independent
    of whether it's separately named in input_sources."""
    dashed = _dehyphenate(mission_id)
    rel = f"skills/{dashed}/SKILL.md"
    if (root / rel).is_file():
        return {"kind": "skill", "label": dashed, "key": mission_id,
                 "rel_path": rel, "clickable": True}
    return None


def _config_piece(root: Path, key: str) -> Optional[Dict[str, Any]]:
    """Rule 3 (§2.3): convention config/<key>.yaml."""
    for candidate in (key, _norm(key)):
        rel = f"config/{candidate}.yaml"
        if (root / rel).is_file():
            return {"kind": "config", "label": candidate, "key": key,
                     "rel_path": rel, "clickable": True}
    return None


def _memory_piece(root: Path, key: str, mission_id: str) -> Optional[Dict[str, Any]]:
    """Rule 4 (§2.4): `working_memory` -> WORKING_MEMORY.md;
    `<id>_memory` -> memory/<id>.md."""
    if key == "working_memory":
        rel = "WORKING_MEMORY.md"
        if (root / rel).is_file():
            return {"kind": "memory", "label": "WORKING_MEMORY.md", "key": key,
                     "rel_path": rel, "clickable": True}
        return None
    if key.endswith("_memory") or key == f"{mission_id}_memory":
        rel = f"memory/{mission_id}.md"
        if (root / rel).is_file():
            return {"kind": "memory", "label": f"memory/{mission_id}.md", "key": key,
                     "rel_path": rel, "clickable": True}
    return None


def _voice_piece(root: Path, key: str) -> Optional[Dict[str, Any]]:
    """Rule 5 (§2.5): `<channel>_voice` -> <channel>/VOICE.md (content
    only today — twitter/substack/newsletter)."""
    if not key.endswith("_voice"):
        return None
    channel = key[: -len("_voice")]
    rel = f"{channel}/VOICE.md"
    if (root / rel).is_file():
        return {"kind": "voice", "label": f"{channel}/VOICE.md", "key": key,
                 "rel_path": rel, "clickable": True}
    return None


def _tool_piece(root: Path, key: str) -> Optional[Dict[str, Any]]:
    """Rule 6 (§2.6): L1 fetch tools live in scripts/lib/fetch_*.py
    (content) or tools/ (accountant); resolve by convention if a matching
    file exists, else skip (no tile at all — tools are not part of the
    input_sources honesty contract the way memory/config/voice are)."""
    for d in _TOOL_SEARCH_DIRS:
        for candidate in (f"fetch_{_norm(key)}.py", f"{_norm(key)}.py"):
            rel = f"{d}/{candidate}"
            if (root / rel).is_file():
                return {"kind": "tool", "label": candidate, "key": key,
                         "rel_path": rel, "clickable": True}
    return None


def resolve_mission_pieces(
    slug: str, mission: Dict[str, Any], allowlist: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Resolve one mission's declared pieces to openable repo files,
    best-effort. Never raises — a dept/mission with nothing on disk
    still returns a single non-clickable mission-core piece.

    Args:
        slug: dept slug — the resolver only ever reads repo_path(slug),
            so a dept page can only surface its own repo's files (no
            cross-dept bleed by construction, PLAN-642 §3).
        mission: one entry from list_missions_full(slug) (must have "id").
        allowlist: optional pre-computed set of rel_paths from
            list_mission_files(slug) — passed in so a page rendering many
            missions builds the allowlist ONCE per request rather than
            re-walking the dept's directories per mission (§0-bis
            refinement 5). If given, any resolved rel_path not in this
            set is treated as unresolved (non-clickable) — this keeps
            the view's clickable surface in lockstep with the guard's
            allowlist, so a piece never renders as clickable and then
            404s when opened.

    Returns: list of Piece dicts: {kind, label, key, rel_path|None,
        clickable: bool}. Order: mission core first, then one piece per
        input_sources key (skill/config/memory/voice/tool as resolved,
        else a muted "reference" chip), plus the mission's own
        naming-convention skill tile if not already covered by an
        input_sources key.
    """
    root = repo_path(slug)
    mission_id = str(mission.get("id") or "")
    if root is None or not mission_id:
        return []

    pieces: List[Dict[str, Any]] = [_mission_core_piece(root, mission_id, mission)]
    resolved_skill_names: set = set()

    input_sources = mission.get("input_sources") or []
    if not isinstance(input_sources, list):
        input_sources = []

    for key in input_sources:
        # §0-bis refinement 2: maya's input_sources sometimes nests
        # policy keys under a dict rather than a flat string
        # (auto_if_policy_passed / auto_with_veto_window). Skip
        # defensively — never attribute error on a non-string entry.
        if not isinstance(key, str):
            continue

        piece = (
            _skill_piece(root, key)
            or _config_piece(root, key)
            or _memory_piece(root, key, mission_id)
            or _voice_piece(root, key)
            or _tool_piece(root, key)
        )
        if piece is None:
            # Rule 7 (§2.7): unresolved key -> muted non-clickable
            # "reference" chip so the mission's full declared input
            # surface stays honest without pretending it's a file.
            piece = {"kind": "reference", "label": key, "key": key,
                       "rel_path": None, "clickable": False}
        else:
            if piece["kind"] == "skill":
                resolved_skill_names.add(piece["label"])
        pieces.append(piece)

    # Mission's own skill by naming convention, if not already surfaced
    # via an input_sources key (avoid a duplicate tile for the same file).
    own_skill = _mission_skill_piece(root, mission_id)
    if own_skill is not None and own_skill["label"] not in resolved_skill_names:
        pieces.append(own_skill)

    if allowlist is not None:
        for p in pieces:
            if p["clickable"] and p["rel_path"] not in allowlist:
                p["clickable"] = False
                p["rel_path"] = None

    return pieces
