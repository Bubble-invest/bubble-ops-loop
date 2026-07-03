"""
mgmt_note_state.py — filter `queues/management/*.yaml` to genuinely-PENDING
notes for the cockpit /dept/<slug> "À traiter" inbox (board card #459).

Root cause it fixes
--------------------
The mgmt-note protocol (scripts/lib/dispatch_helpers.py, STEP 0-ter in
scripts/lib/layer_templates.py) deliberately never moves or mutates note
files once a dept has acted on them — consumption state lives ENTIRELY in
two side files that live alongside the notes:

  - `.consumed.json`    — note IDs the dept has acted on (dept bookkeeping)
  - `.last-mgmt-scan`   — a timestamp watermark: "L1 has scanned the queue
                           as of this instant" (dispatcher bookkeeping)

`console.services.github_reader.list_layer_queues()` (card #376) lists every
`queues/management/*.yaml` file that is not a dotfile and not inside a
`.processed/` subdir as a pending "waiting to be treated" item — it never
reads `.consumed.json` or `.last-mgmt-scan`. Result on Tony's page: 30
identical `morning_brief` rows, one per day since Jun 2, every single one
long since consumed (#459).

This module is the read-only, console-side companion: it re-derives PENDING
per-note (not just "does at least one pending note exist", which is all
`_scan_mgmt_notes` answers) using the *exact same* consumed-first / fail-open
rules, then groups/collapses the result for rendering.

Semantics source of truth (DO NOT reimplement — import and reuse)
-------------------------------------------------------------------
scripts/lib/dispatch_helpers.py:
  - `read_last_mgmt_scan(repo_dir)`      — the watermark reader
  - `_load_consumed_ids(mgmt_dir)`       — the `.consumed.json` reader
  - `_parse_iso(s)`                      — tolerant ISO-8601 parser (handles
                                            trailing 'Z' on py<3.11)

We import these (not vendor/reimplement them) so the console can never drift
from the dispatcher's actual behaviour. `_load_consumed_ids`/`_parse_iso` are
underscore-prefixed (module-private by convention) but carry no `__all__`
restriction; the alternative — hand-copying the parsing/fail-open logic —
is exactly the drift risk card #459 warns against, so we accept the
import-a-private-helper trade-off with this comment as the paper trail.

Per-note PENDING rule (mirrors `_scan_mgmt_notes`'s per-note loop body,
including the #198 consumed-first fix and its fail-open behaviour):

  1. Not a dotfile, not inside a subdirectory (e.g. `.processed/`).
  2. `id` (top-level `id` field, else `directive_id` — fix #468; see
     `_note_id()` below) NOT IN `.consumed.json` — checked FIRST,
     unconditionally, regardless of `created_at`. A consumed note is never
     pending no matter how its timestamp looks.
  3. Unreadable/malformed YAML → fail-open (treat as pending; we would
     rather over-show than silently swallow real work).
  4. Missing/unparseable `created_at` → fail-open (treat as pending).
  5. Otherwise: pending iff `created_at > watermark` (watermark = None means
     "never scanned" → everything not-yet-consumed is pending).

This module additionally groups/collapses for display (NOT part of the
dispatcher's semantics — purely a rendering concern local to the console):
  - notes sharing the same `mission_id`/`kind` label collapse into one row
    "<label> ×N (latest created_at)" when there is more than one.
  - the consumed/seen notes are summarized as a single count, never listed.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Make the repo-root `scripts` namespace package importable so we can reuse
# scripts/lib/dispatch_helpers.py's consumption-state readers verbatim
# (single source of truth — see module docstring). Mirrors the existing
# console/routes/agents.py + console/services/backup_history.py precedent
# for importing scripts.lib.* from the console.
_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402  (see sys.path setup above)
    _load_consumed_ids,
    _parse_iso,
    read_last_mgmt_scan,
)
from console.services.humanize import humanize_queue_item  # noqa: E402


@dataclass
class MgmtNoteItem:
    """One `queues/management/*.yaml` note, annotated with pending state."""
    id: str
    kind: str            # `kind` field, else ""
    mission_id: str       # `mission_id` field, else `kind`, else "item"
    title: str
    created_at: str
    pending: bool


@dataclass
class MgmtNoteRow:
    """One row to render in the pending list — either a single pending note
    or a group of pending notes sharing a mission_id."""
    mission_id: str
    label: str            # "<mission_id>" or "<mission_id> ×N (latest <date>)"
    count: int
    latest_created_at: str
    items: List[MgmtNoteItem] = field(default_factory=list)


@dataclass
class MgmtInboxState:
    """Result of scanning a dept's queues/management/ dir."""
    pending_rows: List[MgmtNoteRow]
    consumed_count: int


def _mgmt_dir(root: Path) -> Path:
    return root / "queues" / "management"


def _note_id(data: Dict[str, Any], fallback: str) -> str:
    """A note's identity for `.consumed.json` lookup.

    MUST match `scripts/lib/dispatch_helpers.py:_scan_mgmt_notes` byte for
    byte: that function resolves the consumed-check key as
    `data.get("id") or data.get("directive_id")` (fix #468) — directive-shaped
    notes (`scripts/dispatch_directives.py`) carry `directive_id` and never a
    top-level `id`, so without the fallback a consumed directive note could
    never match `.consumed.json` and would fail open on its timestamp,
    re-triggering L1 forever (suspected root cause of #235). This module's
    entire job is to mirror the dispatcher's actual behaviour — see
    test_directive_id_used_as_consumption_key_fallback_mirrors_dispatcher.
    Falls back to the filename stem, same as the dispatcher effectively does
    (no `id`/`directive_id` → `note_id` is falsy → treated as unconsumed)."""
    v = data.get("id") or data.get("directive_id")
    if v:
        return str(v)
    return fallback


def _note_label(data: Dict[str, Any], fallback: str) -> str:
    """Grouping/display label: `mission_id`, else `kind`, else the note id."""
    for f in ("mission_id", "kind"):
        v = data.get(f)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return fallback


def _note_title(data: Dict[str, Any], label: str) -> str:
    """Human one-liner for a mgmt-note row.

    Checks a known-payload summary first (#460, Joris spec 2026-07-02) —
    e.g. a `morning_brief` note's payload is pure KPI numbers
    (`dept_health_score`/`children_in_warning`), never a free-text field, so
    without this it always fell through to the bare `label` (the exact
    "morning_brief: morning_brief" duplication #459 first flagged). Falls
    back to the pre-existing free-text-field scan, then the bare label, for
    any mission_id/kind this module doesn't specifically know how to
    summarize.
    """
    known = humanize_queue_item(data, label)
    if known:
        return known
    for f in ("title", "subject", "summary", "detail", "body"):
        v = data.get(f)
        if v and isinstance(v, str) and v.strip():
            excerpt = v.strip().replace("\n", " ")
            return f"{label}: {excerpt[:60]}" + ("…" if len(excerpt) > 60 else "")
    return label


def scan_mgmt_inbox(
    root: "Path | str",
    *,
    extra_stale_check: "Optional[Any]" = None,
) -> MgmtInboxState:
    """Scan `<root>/queues/management/*.yaml` and split into genuinely-pending
    rows (grouped by mission_id when >1 share it) vs. a consumed count.

    `root` is the dept repo root (same `repo_path(slug)` used elsewhere in
    the console) — i.e. the parent of `queues/management/`.

    `extra_stale_check`, if given, is `(kind: str, created_at: str) -> bool`
    — an ADDITIONAL, orthogonal "is this already-acted-on" predicate applied
    AFTER the consumed/watermark check. Card #376/#391 added a generic
    terminal-kind staleness guard (`github_reader._is_stale_terminal_item`)
    that applies to every queue dir, including `queues/management/` (an
    `executed_trade` record an agent forgot to move to `.processed/`). We
    take that predicate as a parameter rather than importing
    `github_reader` here to avoid a service-to-service import cycle
    (github_reader is the one that calls INTO this module).

    Returns `MgmtInboxState(pending_rows=[], consumed_count=0)` if the
    directory does not exist (nothing to show — same as today).
    """
    mgmt_dir = _mgmt_dir(Path(root))
    if not mgmt_dir.is_dir():
        return MgmtInboxState(pending_rows=[], consumed_count=0)

    consumed_ids = _load_consumed_ids(mgmt_dir)
    watermark = read_last_mgmt_scan(root)

    pending_items: List[MgmtNoteItem] = []
    consumed_count = 0

    for p in sorted(mgmt_dir.glob("*.yaml")):
        # Rule 1: not a dotfile, not in a subdirectory (.processed/ etc).
        if p.name.startswith("."):
            continue
        if not p.is_file():
            continue
        if p.parent != mgmt_dir:
            # Defensive; glob("*.yaml") never descends, but keep the check
            # explicit so a future glob("**/*.yaml") edit fails loudly. (Note:
            # this alone is the real guard — a `parent in mgmt_dir.parents`-
            # style check was previously ANDed in here but was vacuous, since
            # `p.parent == mgmt_dir` is exactly the depth-1 case this `!=`
            # already excludes; drop it rather than carry dead logic.)
            continue

        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            # Rule 3: unreadable/malformed → fail-open (treat as pending).
            # We cannot recover an id/label from unparseable YAML — use the
            # filename stem for both so the row is still identifiable.
            pending_items.append(MgmtNoteItem(
                id=p.stem, kind="", mission_id=p.stem,
                title=f"{p.stem} (fichier illisible)", created_at="",
                pending=True,
            ))
            continue
        if not isinstance(data, dict):
            continue

        note_id = _note_id(data, p.stem)
        kind = str(data.get("kind") or "")
        label = _note_label(data, note_id)
        created_at = str(data.get("created_at") or "")

        # Rule 2 (fix #198, consumed-first): check BEFORE the created_at
        # parse, unconditionally — a consumed note is never pending
        # regardless of what its timestamp looks like.
        if note_id in consumed_ids:
            consumed_count += 1
            continue

        # Rule 5 vs. rule 4: watermark comparison, fail-open on bad/missing ts.
        pending = True
        if watermark is not None and created_at:
            try:
                ts = _parse_iso(created_at)
                if ts.tzinfo is None:
                    from datetime import timezone
                    ts = ts.replace(tzinfo=timezone.utc)
                pending = ts > watermark
            except (ValueError, TypeError):
                pending = True  # unparseable → fail-open (rule 4)
        # watermark is None → never scanned → pending stays True (rule 5)
        # created_at missing → pending stays True (rule 4)

        if not pending:
            consumed_count += 1
            continue

        # Orthogonal terminal-kind staleness guard (#391), applied last —
        # an old executed_trade/wrap-up record an agent forgot to move to
        # .processed/. Not counted in consumed_count (it's a different
        # reason for hiding: "stale record", not "already consumed").
        if extra_stale_check is not None and extra_stale_check(kind, created_at):
            continue

        pending_items.append(MgmtNoteItem(
            id=note_id, kind=kind, mission_id=label,
            title=_note_title(data, label), created_at=created_at,
            pending=True,
        ))

    return MgmtInboxState(
        pending_rows=_group_pending(pending_items),
        consumed_count=consumed_count,
    )


def _group_pending(items: List[MgmtNoteItem]) -> List[MgmtNoteRow]:
    """Group pending items sharing a mission_id into one row each.

    Order: deterministic — mission_ids appear in the order first seen
    (items are already sorted by filename from the glob above).
    """
    from collections import OrderedDict
    buckets: "OrderedDict[str, List[MgmtNoteItem]]" = OrderedDict()
    for it in items:
        buckets.setdefault(it.mission_id, []).append(it)

    rows: List[MgmtNoteRow] = []
    for mission_id, group in buckets.items():
        latest = max((it.created_at for it in group if it.created_at), default="")
        count = len(group)
        if count > 1:
            label = f"{mission_id} ×{count}" + (f" ({latest})" if latest else "")
        else:
            label = group[0].title
        rows.append(MgmtNoteRow(
            mission_id=mission_id, label=label, count=count,
            latest_created_at=latest, items=group,
        ))
    return rows
