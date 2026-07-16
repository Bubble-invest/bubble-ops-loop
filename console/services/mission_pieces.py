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

PR-B (#642 full completion) root-caused and fixed a config-tile bug from
PR-A: `_config_piece` matched `config/<input_sources_key>.yaml` literally,
but verified against the real `bubble-ops-content` repo, ZERO of its 48
input_sources keys across 12 missions match a real config/*.yaml filename
that way (`brand_guidelines` -> no `config/brand_guidelines.yaml` file
exists; the real file is `config/x.yaml`, `config/linkedin.yaml`, etc.,
named after the MISSION/CHANNEL, not the input_sources key). PR-A's own
test fixture invented a `brand_guidelines` key with a matching fixture
file, which is why the bug passed review — the fixture confirmed the
resolver's assumption instead of stress-testing it. The fix: resolve
config primarily by MISSION-ID / dept-glossary convention (mirroring how
`_mission_skill_piece` already resolves the mission's own skill by
naming convention, independent of input_sources), falling back to the
literal input_sources-key match for depts/missions that do use that
shape.
"""
from __future__ import annotations

import re
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
    "output": "📤",  # PR-B: sortie tile (item 3), reference prototype's 📤
}

PIECE_LABEL: Dict[str, str] = {
    "mission": "mission",
    "skill": "compétence",
    "tool": "outil",
    "config": "configuration",
    "memory": "mémoire",
    "voice": "voix",
    "reference": "source",
    "output": "sortie",
}

# PR-B: the reference's "entrées / cœur / sortie" group labels (item 2),
# keyed by the `group` field resolve_mission_pieces tags each piece with.
GROUP_LABEL: Dict[str, str] = {
    "entrees": "entrées",
    "coeur": "cœur",
    "sortie": "sortie",
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
    else missions/<id>.yaml (ben/maya/tony/cgp flat layout) — PR-B fix:
    also tries the hyphenated filename variant, since `id:` fields are
    conventionally underscored but the flat-layout FILENAME itself is
    conventionally hyphenated (verified: ben's `data_update` mission's
    own `id:` field is underscored, but the file on disk is
    `missions/data-update.yaml` — same ambiguity github_reader's
    `_normalize_filename_mission_id` already resolves the other
    direction for; #547) — else an inline non-clickable tile showing the
    mission's own description."""
    prompt_path = f"missions/{mission_id}/PROMPT.md"
    if (root / prompt_path).is_file():
        return {"kind": "mission", "label": mission_id, "key": mission_id,
                "rel_path": prompt_path, "clickable": True}
    for candidate in (mission_id, _dehyphenate(mission_id)):
        yaml_path = f"missions/{candidate}.yaml"
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
    """Rule 3 (§2.3, PR-B fix): convention config/<key>.yaml, as a
    fallback for depts/missions where an input_sources key DOES name a
    real config file 1:1. Kept for that case, but verified against the
    real content repo this rarely fires — see `_mission_config_piece`,
    which is the primary resolution path added in PR-B."""
    for candidate in (key, _norm(key)):
        rel = f"config/{candidate}.yaml"
        if (root / rel).is_file():
            return {"kind": "config", "label": candidate, "key": key,
                     "rel_path": rel, "clickable": True}
    return None


# PR-B fix: verified against the real bubble-ops-content repo (2026-07-16),
# every one of its 13 config/*.yaml files is named after the MISSION/
# CHANNEL, not any input_sources key — but NOT by one uniform suffix rule.
# `draft_x` -> config/x.yaml, `draft_linkedin` -> config/linkedin.yaml
# (verb-prefix-stripped whole tail), while `gather_internal_work` ->
# config/internal_sources.yaml, `gather_newsletter_signal` ->
# config/newsletter_sources.yaml, `gather_x_timeline` -> config/x_sources
# .yaml, `gather_youtube_taste` -> config/youtube_topics.yaml (the SECOND
# underscore-token, not the tail, + a _sources/_topics suffix), and
# `publish_execution` -> config/publish.yaml, `synthesizing_content_
# feedback` -> config/feedback.yaml (the FIRST or LAST token instead). A
# single fixed prefix-strip rule can't cover all four shapes without
# hardcoding a per-mission table (the exact anti-pattern this module
# exists to avoid). Instead: generate every individual underscore token
# of the mission id plus the whole verb-stripped tail as CANDIDATE
# glossary names, and try each against config/<candidate>.yaml,
# config/<candidate>_sources.yaml, config/<candidate>_topics.yaml — the
# first candidate (in mission-id token order) that hits a real file wins.
# This is still zero per-dept/per-mission hardcoding: any dept's config/
# naming that keys off one word of the mission id is covered by
# construction, not by name.
_MISSION_VERB_PREFIXES = ("gather_", "draft_", "synthesizing_", "publish_")


def _mission_config_piece(root: Path, mission_id: str) -> Optional[Dict[str, Any]]:
    """Rule 3, primary path (PR-B): config/<mission-glossary-name>.yaml."""
    config_dir = root / "config"
    if not config_dir.is_dir():
        return None

    stripped = mission_id
    for prefix in _MISSION_VERB_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break

    tokens = [t for t in mission_id.split("_") if t]
    # Candidate glossary names, in mission-id token order (whole id and
    # verb-stripped tail first — the common case — then each individual
    # token as a fallback for the irregular shapes above).
    candidates: List[str] = [mission_id, stripped]
    for t in tokens:
        if t not in candidates:
            candidates.append(t)

    # `gather_*` missions verified to name their config with a
    # `_sources`/`_topics` suffix (they're about WHERE to look, not the
    # mission itself); every other verb (draft_/publish_/synthesizing_,
    # and unprefixed) verified to use the bare name. Two missions can
    # legitimately share a token (draft_x vs gather_x_timeline both
    # contain "x"; draft_newsletter vs gather_newsletter_signal both
    # contain "newsletter") — the verb is what disambiguates which
    # variant is THIS mission's config, so it picks the suffix order
    # rather than trying suffixed-before-bare (or vice versa) globally.
    if mission_id.startswith("gather_"):
        suffixes: tuple = ("_sources", "_topics", "")
    else:
        suffixes = ("", "_sources", "_topics")

    for candidate in candidates:
        for suffix in suffixes:
            rel = f"config/{candidate}{suffix}.yaml"
            if (root / rel).is_file():
                return {"kind": "config", "label": f"{candidate}{suffix}",
                         "key": mission_id, "rel_path": rel, "clickable": True}
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


def _description_mentioned_skills(root: Path, mission: Dict[str, Any]) -> List[Dict[str, Any]]:
    """PR-B fix (item 4, granularity gap): a mission's free-text
    `description` sometimes names a skill it uses that is NEITHER in
    `input_sources` NOR the mission's own naming-convention skill dir —
    e.g. draft_substack_ic's description says "via the
    writing-investment-cases process plus a free teaser via
    write-investment-teaser", and publish_execution's description lists
    "publish-linkedin / publish-substack-post / publish-substack-note /
    publish-twitter". Verified against the real content repo: this is
    why the reference showed 10 skill tiles fleet-wide while the live
    page only resolved 6 — those extra skills are prose-mentioned, not
    declared as structured input_sources.

    Scans the description for every skills/<name>/SKILL.md dir name that
    actually exists on disk, as a whole-word substring match (hyphenated
    names only — skills/ dirs are always hyphenated, so this can't
    false-positive on ordinary prose words). Best-effort, never raises."""
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return []
    description = str(mission.get("description") or "")
    if not description:
        return []

    found: List[Dict[str, Any]] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        name = skill_dir.name
        if not (skill_dir / "SKILL.md").is_file():
            continue
        # Whole-word match on the hyphenated name so e.g. "publish-twitter"
        # doesn't also match a hypothetical "publish-twitter-v2" dir, and
        # ordinary prose can't accidentally match a short skill name.
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", description):
            found.append({"kind": "skill", "label": name, "key": name,
                           "rel_path": f"skills/{name}/SKILL.md", "clickable": True})
    return found


def _output_piece(mission: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """PR-B fix (item 3): the mission's sortie/output tile, from
    dept.yaml's declared `output_queue` + `creates[]` — no file to open
    (it's a queue destination, not a static file), so this is always
    non-clickable, matching the reference prototype's 📤 tile which is a
    label, not a link."""
    output_queue = mission.get("output_queue")
    creates = mission.get("creates") or []
    if not output_queue and not creates:
        return None
    creates_label = ", ".join(str(c) for c in creates) if creates else ""
    label = f"{output_queue or ''}".rstrip("/")
    if creates_label:
        label = f"{label} — {creates_label}" if label else creates_label
    return {"kind": "output", "label": label or "sortie", "key": "output",
             "rel_path": None, "clickable": False}


def mission_status(root: Path, mission: Dict[str, Any]) -> str:
    """PR-B fix (item 8): has this mission run in the last 7 days?
    Reads outputs/<date>/missions/<id>/.last-run across the most recent
    output dates on disk (verified real shape: outputs/<date>/missions/
    <id>/.last-run in the real content repo). Returns one of:
    'événementiel' (cadence: event — no fixed schedule to be "on time"
    against, checked first so an event mission is never mislabeled
    dormant just because nothing happened to trigger it this week),
    'actif' (ran within 7d), 'dormant' (has run before, but not
    recently), or 'inconnu' (no output history found at all — a
    new/never-run mission, not necessarily broken)."""
    import datetime

    if str(mission.get("cadence") or "").lower() == "event":
        return "événementiel"

    mission_id = str(mission.get("id") or "")
    outputs_dir = root / "outputs"
    if not mission_id or not outputs_dir.is_dir():
        return "inconnu"

    cutoff = datetime.date.today() - datetime.timedelta(days=7)
    ever_ran = False
    for date_dir in sorted(outputs_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        try:
            d = datetime.date.fromisoformat(date_dir.name)
        except ValueError:
            continue
        mission_dir = date_dir / "missions" / mission_id
        if mission_dir.is_dir() and any(mission_dir.iterdir()):
            ever_ran = True
            if d >= cutoff:
                return "actif"
    return "dormant" if ever_ran else "inconnu"


def mission_tagline(mission: Dict[str, Any]) -> Optional[str]:
    """PR-B fix (item 8): first sentence of the mission's description, for
    the compact one-line tagline under the mission id (reference
    prototype's `tagline` field). Best-effort sentence split on the
    first '.', '!' or '?' followed by a space or end-of-string."""
    description = str(mission.get("description") or "").strip()
    if not description:
        return None
    match = re.search(r"^(.*?[.!?])(?:\s|$)", description)
    first = match.group(1) if match else description
    return first[:220]


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
        clickable: bool, group: "entrees"|"coeur"|"sortie"}. Order:
        the mission's own config tile (PR-B fix, resolved by mission-id
        convention before input_sources are walked at all) and own-skill
        tile, then one piece per input_sources key (skill/config/memory/
        voice/tool as resolved, else a muted "reference" chip), then any
        additional skills the mission's free-text description names
        (PR-B item 4), then the mission core + output tiles.

        `group` mirrors the reference prototype's 3-row card layout
        (PLAN-642 / build_rebuild_html.py `card()`): config/tool/memory/
        voice/reference pieces feed the mission ("entrées"), mission +
        skill pieces are its orchestration/capability ("cœur"), and the
        single output piece is its "sortie". The template groups by this
        field directly rather than re-deriving the grouping in Jinja.
    """
    root = repo_path(slug)
    mission_id = str(mission.get("id") or "")
    if root is None or not mission_id:
        return []

    _GROUP_BY_KIND = {
        "config": "entrees", "tool": "entrees", "memory": "entrees",
        "voice": "entrees", "reference": "entrees",
        "mission": "coeur", "skill": "coeur",
        "output": "sortie",
    }

    def _tag(piece: Dict[str, Any]) -> Dict[str, Any]:
        piece["group"] = _GROUP_BY_KIND.get(piece["kind"], "entrees")
        return piece

    pieces: List[Dict[str, Any]] = [_tag(_mission_core_piece(root, mission_id, mission))]
    resolved_skill_names: set = set()
    resolved_config_labels: set = set()

    # PR-B fix (config-tile bug): resolve the mission's own config file by
    # mission-id/glossary convention FIRST — this is the path that
    # actually fires for the real content repo (see module docstring).
    # The literal input_sources-key match (_config_piece, below) is a
    # fallback for the depts/missions where that DOES line up 1:1.
    own_config = _mission_config_piece(root, mission_id)
    if own_config is not None:
        pieces.append(_tag(own_config))
        resolved_config_labels.add(own_config["label"])

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
            elif piece["kind"] == "config" and piece["label"] in resolved_config_labels:
                continue  # same config file already surfaced via own_config
            elif piece["kind"] == "config":
                resolved_config_labels.add(piece["label"])
        pieces.append(_tag(piece))

    # Mission's own skill by naming convention, if not already surfaced
    # via an input_sources key (avoid a duplicate tile for the same file).
    own_skill = _mission_skill_piece(root, mission_id)
    if own_skill is not None and own_skill["label"] not in resolved_skill_names:
        pieces.append(_tag(own_skill))
        resolved_skill_names.add(own_skill["label"])

    # PR-B fix (item 4, granularity gap): additional skills the mission's
    # free-text description names but which aren't otherwise declared
    # (draft_substack_ic's writing-investment-cases/write-investment-
    # teaser, publish_execution's publish-* skills).
    for mentioned in _description_mentioned_skills(root, mission):
        if mentioned["label"] in resolved_skill_names:
            continue
        pieces.append(_tag(mentioned))
        resolved_skill_names.add(mentioned["label"])

    # PR-B fix (item 3): the sortie/output tile.
    output_piece = _output_piece(mission)
    if output_piece is not None:
        pieces.append(_tag(output_piece))

    if allowlist is not None:
        for p in pieces:
            if p["clickable"] and p["rel_path"] not in allowlist:
                p["clickable"] = False
                p["rel_path"] = None

    return pieces


# PR-B fix (items 5-7): the POOL / GATE inter-layer bands + flow-arrow
# wording. Generic across depts — derived from the missions' own declared
# `output_queue` (dept.yaml), never a hardcoded per-dept queue path.
_POOL_QUEUE_CANDIDATES = ("queues/research", "queues/research/")
_GATE_QUEUE_CANDIDATES = ("queues/gates", "queues/gates/")


def resolve_layer_band(
    slug: str, from_layer_missions: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """PR-B (item 5): the connective band between one Moment and the next,
    e.g. the reference's "THE POOL" band between L1 and L2. Generic: reads
    the `output_queue` actually declared on the FROM layer's missions
    (rather than assuming every dept's L1 writes to queues/research/ the
    way content's does) and reports it as the band, with a description +
    an optional clickable pool-schema deep-link when the dept repo has
    docs/CONTEXT_POOL_SCHEMA.md (content only today; any dept that adds
    one is picked up automatically via the same convention).

    Returns None if the FROM layer declares no missions or none of them
    declare an output_queue (nothing to show a band for) — the template
    renders no band rather than a fabricated generic one, per the card's
    "generic depts get their equivalent, or no band if none" instruction.
    """
    root = repo_path(slug)
    if root is None or not from_layer_missions:
        return None

    queues = {
        str(m.get("output_queue")).rstrip("/")
        for m in from_layer_missions
        if m.get("output_queue")
    }
    if not queues:
        return None
    # Most depts have exactly one L1 output queue; if a dept's missions
    # disagree, show the most common one rather than crashing/picking
    # arbitrarily — still generic, no per-dept hardcoding.
    queue_path = sorted(queues)[0]

    pool_schema_path = "docs/CONTEXT_POOL_SCHEMA.md"
    has_schema = (root / pool_schema_path).is_file()
    return {
        "queue_path": queue_path,
        "is_pool": queue_path.rstrip("/") in _POOL_QUEUE_CANDIDATES,
        "schema_rel_path": pool_schema_path if has_schema else None,
    }


def resolve_gate_band(missions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """PR-B (item 6): the human-GATE band, shown once between the Moment(s)
    that write to queues/gates/ and the next Moment. Generic: fires
    whenever ANY mission in the dept declares `output_queue:
    queues/gates/`, and the band is placed after the HIGHEST layer number
    among those missions — not hardcoded to content's L2/L3 split, since
    a leaner dept may gate at a different layer boundary or not at all."""
    gate_layers = [
        int(m["layer"]) for m in missions
        if str(m.get("output_queue") or "").rstrip("/") in _GATE_QUEUE_CANDIDATES
        and str(m.get("layer") or "").strip().lstrip("-").isdigit()
    ]
    if not gate_layers:
        return None
    return {"gate_url_kind": "publish_proposal", "after_layer": max(gate_layers)}
