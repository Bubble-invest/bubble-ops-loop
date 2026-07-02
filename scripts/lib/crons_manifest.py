"""
crons_manifest.py — load + diff a dept's declarative durable-cron manifest.

Board card #461 (child of #456): `CronCreate`'s `durable: true` is
session-only on headless CLI — a systemd/launchd restart is a brand-new
process, so any cron armed only via CronCreate (mail-brief, etc.) silently
vanishes and never fires again until someone notices and re-arms it by hand.

Fix (#456 options b+c): each dept may ship a small declarative manifest at
`<dept-dir>/config/crons.yaml` (schema: schemas-draft/crons-manifest.schema.yaml)
listing the durable session-level wakes it needs. The boot-rearm turn (fired
by ops-loop-dept.service.template's ExecStartPost inject-file write on every
service (re)start — see deploy/telegram-plugin/README.md) is generalized to:
  1. load the dept's manifest (missing file => no-op, nothing to do);
  2. run CronList;
  3. diff manifest entries against the live cron names;
  4. re-create anything missing via CronCreate, and log it;
  5. (safety net, #456 option c) flag any `critical: true` entry still
     missing after the re-arm attempt, for a loud Telegram alert.

This module is the TESTABLE core (pure functions, no CronCreate/CronList
side effects — those are platform tools only reachable from inside a live
agent session, not from a test harness). The boot-rearm prompt text (in
ops-loop-dept.service.template) instructs the agent to run the equivalent
of `diff_manifest_against_live()` itself using its own CronList/CronCreate
tool calls; this module is what a unit test — and any future Python-side
tooling (console surfacing, drift audits) — calls directly.

IMPORTANT — this is a DIFFERENT concept from dept.yaml::recurring_missions:
  - recurring_missions  = Layer 1-4 OODA pipeline work (materializes queue
    items; consumed by scripts/lib/dispatch_helpers.py).
  - config/crons.yaml    = session-level CronCreate wakes the agent needs to
    keep functioning at all (self-pacing /loop cadence, a fixed-time daily
    brief, etc). No queue/layer semantics.
Do not conflate the two loaders; they load different files for different
purposes, deliberately kept separate so this manifest can evolve without
touching the v3 dept.schema.yaml contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


class CronsManifestError(ValueError):
    """Raised when a manifest file is malformed enough that it cannot be used."""


@dataclass(frozen=True)
class CronEntry:
    name: str
    schedule: str
    prompt_ref: str
    description: str = ""
    critical: bool = True

    def resolve_prompt(self, dept_dir: Path) -> str:
        """Resolve `prompt_ref` to literal prompt text.

        `file:<relpath>` is read relative to the dept dir (so long prompts
        live in their own file instead of bloating crons.yaml); anything
        else is treated as the literal prompt text.
        """
        if self.prompt_ref.startswith("file:"):
            rel = self.prompt_ref[len("file:"):]
            target = (dept_dir / rel).resolve()
            return target.read_text(encoding="utf-8")
        return self.prompt_ref


@dataclass(frozen=True)
class CronsManifest:
    crons: tuple[CronEntry, ...] = field(default_factory=tuple)
    version: int = 1

    def by_name(self) -> dict[str, CronEntry]:
        return {c.name: c for c in self.crons}


def load_manifest(dept_dir: Path) -> Optional[CronsManifest]:
    """Load `<dept_dir>/config/crons.yaml`.

    Returns None (no-op) when the file does not exist — a dept with no
    manifest has nothing durable beyond the /loop cadence, which boot-rearm
    already handles unconditionally regardless of this manifest's presence.

    Raises CronsManifestError on a present-but-malformed file (fail LOUD,
    not silent — a broken manifest must not look like "no crons to re-arm").
    """
    path = Path(dept_dir) / "config" / "crons.yaml"
    if not path.exists():
        return None

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CronsManifestError(f"{path}: invalid YAML — {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CronsManifestError(f"{path}: top-level must be a mapping, got {type(raw).__name__}")

    version = raw.get("version", 1)
    items = raw.get("crons")
    if items is None:
        raise CronsManifestError(f"{path}: missing required 'crons' key")
    if not isinstance(items, list):
        raise CronsManifestError(f"{path}: 'crons' must be a list")

    entries: list[CronEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise CronsManifestError(f"{path}: crons[{i}] must be a mapping")
        for required in ("name", "schedule", "prompt_ref"):
            if required not in item:
                raise CronsManifestError(f"{path}: crons[{i}] missing required '{required}'")
        name = item["name"]
        if name in seen:
            raise CronsManifestError(f"{path}: duplicate cron name '{name}'")
        seen.add(name)
        entries.append(
            CronEntry(
                name=name,
                schedule=item["schedule"],
                prompt_ref=item["prompt_ref"],
                description=item.get("description", ""),
                critical=bool(item.get("critical", True)),
            )
        )

    return CronsManifest(crons=tuple(entries), version=version)


@dataclass(frozen=True)
class ManifestDiff:
    present: tuple[str, ...]           # manifest names already in the live cron list
    missing: tuple[CronEntry, ...]     # manifest entries absent from the live cron list -> re-create
    missing_critical: tuple[CronEntry, ...]  # subset of `missing` with critical=True -> alert too


def diff_manifest_against_live(manifest: Optional[CronsManifest], live_names: list[str]) -> ManifestDiff:
    """Compare a loaded manifest against a CronList-style live name snapshot.

    `live_names` is the list of task/cron names currently registered in the
    running session (as returned by the CronList tool — callers pass just
    the names, keeping this function decoupled from the tool's exact
    response shape). Idempotent: calling this again after a successful
    re-arm (so the missing entries are now live) yields an empty `missing`.
    """
    if manifest is None or not manifest.crons:
        return ManifestDiff(present=(), missing=(), missing_critical=())

    live_set = set(live_names)
    present = tuple(c.name for c in manifest.crons if c.name in live_set)
    missing = tuple(c for c in manifest.crons if c.name not in live_set)
    missing_critical = tuple(c for c in missing if c.critical)
    return ManifestDiff(present=present, missing=missing, missing_critical=missing_critical)
