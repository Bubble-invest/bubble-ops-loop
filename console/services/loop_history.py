"""
loop_history.py — enumerate a dept's loop-run history + safely read outputs.

{{OPERATOR}} msg 1168 (2026-06-01): the dept page should show the history of loop
runs, with each output file clickable to read in-browser — even when a run
produced nothing.

The /loop writes one `outputs/<YYYY-MM-DD>/` dir per active day, with:
  outputs/<date>/heartbeat.log              (loop tick log)
  outputs/<date>/round_counter.json         ({"<layer>": <rounds>})
  outputs/<date>/<layer>/.last-run          (ISO ts)
  outputs/<date>/<layer>/summary.md
  outputs/<date>/<layer>/logs.jsonl
  outputs/<date>/<layer>/<artifact>.{md,yaml,jsonl}
  outputs/<date>/<layer>/artifacts/*

`list_loop_runs` turns that tree into one entry per date (newest first).
`read_output_file` resolves a single file for the viewer route, refusing any
path that escapes the dept's outputs/ dir.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from console.services.dept_registry import repo_path

_log = logging.getLogger(__name__)

# How many days of history to surface. The loop runs daily; a month is a
# generous browse window without an unbounded scan.
_MAX_RUNS = 45

# Max bytes we'll read into the viewer — guards against a runaway logs.jsonl.
_MAX_FILE_BYTES = 256 * 1024

# Layer → human "moment" name (mirrors dept_detail.html's moment_names).
MOMENT_NAMES: Dict[int, str] = {
    1: "Le matin",
    2: "La recherche",
    3: "L'exécution",
    4: "Le débrief du soir",
}

# Extension → viewer rendering kind.
_KIND_BY_SUFFIX: Dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".jsonl": "jsonl",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".log": "text",
    ".txt": "text",
    ".last-run": "text",
}


@dataclass
class OutputFile:
    name: str            # display name, e.g. "summary.md" or "artifacts/x.md"
    rel_path: str        # repo-relative, e.g. "outputs/2026-05-31/4/summary.md"
    empty: bool = False


@dataclass
class LayerRun:
    num: int
    name: str
    last_run: Optional[str]
    rounds: Optional[int]
    files: List[OutputFile] = field(default_factory=list)


@dataclass
class LoopRun:
    date: str                       # iso date
    layers: List[LayerRun] = field(default_factory=list)
    extra_files: List[OutputFile] = field(default_factory=list)  # heartbeat, etc.
    total_rounds: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        """No layer produced any output this day (heartbeat-only / bare dir)."""
        return not any(lr.files for lr in self.layers)


def _read_text(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _list_layer_files(layer_dir: Path, rel_prefix: str) -> List[OutputFile]:
    """All regular files directly in a layer dir, plus one level into
    artifacts/. Sorted, with summary.md surfaced first when present."""
    out: List[OutputFile] = []
    for child in sorted(layer_dir.iterdir()):
        if child.is_file():
            out.append(OutputFile(
                name=child.name,
                rel_path=f"{rel_prefix}/{child.name}",
                empty=child.stat().st_size == 0,
            ))
        elif child.is_dir() and child.name == "artifacts":
            for art in sorted(child.iterdir()):
                if art.is_file():
                    out.append(OutputFile(
                        name=f"artifacts/{art.name}",
                        rel_path=f"{rel_prefix}/artifacts/{art.name}",
                        empty=art.stat().st_size == 0,
                    ))
    # Surface summary.md first — it's the human-readable entry point.
    out.sort(key=lambda f: (f.name != "summary.md", f.name))
    return out


def list_loop_runs(slug: str) -> List[LoopRun]:
    """Return the dept's loop-run history, newest date first. Empty list if
    the dept has no outputs/ yet."""
    root = repo_path(slug)
    if root is None:
        return []
    outputs_dir = root / "outputs"
    if not outputs_dir.exists():
        return []

    dated: List[date] = []
    for child in outputs_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            dated.append(date.fromisoformat(child.name))
        except ValueError:
            continue  # skips dry-run/, onboarding/, playwright-screenshots/, …
    if not dated:
        return []

    dated.sort(reverse=True)
    runs: List[LoopRun] = []
    for d in dated[:_MAX_RUNS]:
        date_str = d.isoformat()
        date_dir = outputs_dir / date_str

        # round_counter.json → {"<layer>": rounds}
        rounds_map: Dict[str, int] = {}
        rc = date_dir / "round_counter.json"
        if rc.exists():
            try:
                parsed = json.loads(rc.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    rounds_map = {str(k): int(v) for k, v in parsed.items()
                                  if isinstance(v, (int, float))}
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        layers: List[LayerRun] = []
        for n in (1, 2, 3, 4):
            layer_dir = date_dir / str(n)
            if not layer_dir.is_dir():
                continue
            rel_prefix = f"outputs/{date_str}/{n}"
            last_run = None
            lr_file = layer_dir / ".last-run"
            if lr_file.exists():
                last_run = (_read_text(lr_file) or "").strip() or None
            layers.append(LayerRun(
                num=n,
                name=MOMENT_NAMES.get(n, f"Moment {n}"),
                last_run=last_run,
                rounds=rounds_map.get(str(n)),
                files=_list_layer_files(layer_dir, rel_prefix),
            ))

        # Date-level files (heartbeat, round_counter) — clickable too.
        extra: List[OutputFile] = []
        for f in sorted(date_dir.iterdir()):
            if f.is_file():
                extra.append(OutputFile(
                    name=f.name,
                    rel_path=f"outputs/{date_str}/{f.name}",
                    empty=f.stat().st_size == 0,
                ))

        total = sum(rounds_map.values()) if rounds_map else None
        runs.append(LoopRun(date=date_str, layers=layers,
                            extra_files=extra, total_rounds=total))
    return runs


def read_output_file(slug: str, rel_path: str) -> Optional[Dict[str, Any]]:
    """Safely read one output file for the viewer.

    `rel_path` is repo-relative and MUST live under outputs/. Returns
    {name, rel_path, kind, content, empty} or None if the dept/file is
    missing or the path escapes outputs/ (traversal guard).
    """
    root = repo_path(slug)
    if root is None:
        return None

    # Normalize and refuse traversal. We resolve against the repo root and
    # require the result to stay within <root>/outputs.
    rel = rel_path.lstrip("/")
    if ".." in Path(rel).parts:
        return None
    outputs_dir = (root / "outputs").resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(outputs_dir)
    except ValueError:
        return None  # outside outputs/
    if not target.is_file():
        return None

    size = target.stat().st_size
    if size == 0:
        return {"name": target.name, "rel_path": rel, "kind": "text",
                "content": "", "empty": True}

    raw = target.read_bytes()[:_MAX_FILE_BYTES]
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
    if size > _MAX_FILE_BYTES:
        content += f"\n\n… (tronqué — fichier de {size} octets)"

    kind = _KIND_BY_SUFFIX.get(target.suffix, "text")
    if target.name == ".last-run":
        kind = "text"
    return {"name": target.name, "rel_path": rel, "kind": kind,
            "content": content, "empty": False}


# ---------------------------------------------------------------------------
# Decision timeline — surfaced in the loop-history section
# ---------------------------------------------------------------------------


@dataclass
class DecisionEvent:
    """One human decision (approved / rejected / deferred / proposed)."""
    id: str
    kind: str                      # trade_proposal, prospect_dm, warming_comment, …
    date: str                      # iso date extracted from filename or created_at
    summary: str                   # one-line description
    status: str                    # pending / approved / rejected / deferred / executed
    url: str = ""                  # link to gate detail page, if applicable


# Maps gate YAML actions to status. A gate in queues/gates/ is pending;
# one in inbox/decisions/ was approved; one in .processed/ was executed.
_STATUS_FROM_DIR = {
    "gates": "pending",
    "decisions": "approved",
}


def list_decision_events(slug: str) -> List[DecisionEvent]:
    """Scan the dept's inbox/decisions/ and queues/gates/ for decision events.

    Returns a list of DecisionEvent sorted newest-first, drawing from:
      - queues/gates/*.yaml         → pending proposals (awaiting decision)
      - queues/gates/.processed/*.yaml → past proposals (executed)
      - inbox/decisions/*.yaml       → approved (awaiting execution)
      - inbox/decisions/.processed/*.yaml → past decisions (executed)
    """
    root = repo_path(slug)
    if root is None:
        return []

    out: List[DecisionEvent] = []

    def _scan(dir_path: Path, status: str) -> None:
        if not dir_path.exists():
            return
        seen_ids = set()  # de-duplicate by id
        for p in sorted(dir_path.glob("*.yaml")):
            if p.name.startswith("."):
                continue
            try:
                doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(doc, dict):
                continue

            # Determine kind: from YAML field, or parse from filename
            kind = doc.get("kind", "")
            if not kind or kind == "unknown":
                # Parse from filename: trade-proposal-DTLA-2026-06-04.yaml
                stem = p.stem
                # Try to extract kind from prefix before the first date-like segment
                parts = stem.split("-")
                kind_parts = []
                for part in parts:
                    if len(part) == 8 and part.isdigit():
                        break  # hit a date, stop
                    kind_parts.append(part)
                kind = "-".join(kind_parts) if kind_parts else stem

            # Extract date: try created_at, then scan filename for YYYY-MM-DD or YYYYMMDD
            date_str = (doc.get("created_at") or "")[:10]
            if not date_str:
                stem = p.stem
                # Try YYYY-MM-DD pattern first (e.g. trade-proposal-DTLA-2026-06-04)
                import re
                m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
                if m:
                    date_str = m.group(1)
                else:
                    # Try YYYYMMDD (e.g. prospect_dm-...-20260601-072044)
                    parts = stem.split("-")
                    for part in parts:
                        if len(part) == 8 and part.isdigit():
                            date_str = f"{part[:4]}-{part[4:6]}-{part[6:8]}"
                            break

            summary = doc.get("summary", "")
            if not summary:
                ticker = doc.get("ticker", "")
                side = doc.get("side", "")
                if ticker and side:
                    summary = f"{side} {ticker}"
                elif kind:
                    summary = kind.replace("_", " ")
                else:
                    summary = p.stem[:80]
            if len(summary) > 150:
                summary = summary[:147] + "…"

            ev_id = doc.get("id", p.stem)
            if ev_id in seen_ids:
                continue
            seen_ids.add(ev_id)

            out.append(DecisionEvent(
                id=ev_id,
                kind=kind,
                date=date_str,
                summary=summary.strip(),
                status=status,
                url=f"/gate/{slug}/{ev_id}" if status == "pending" else "",
            ))

    # Scan active queues first (most relevant)
    _scan(root / "queues" / "gates", "pending")
    _scan(root / "inbox" / "decisions", "approved")
    _scan(root / "queues" / "gates" / ".processed", "executed")
    _scan(root / "inbox" / "decisions" / ".processed", "executed")

    # Sort newest first by date, ties broken by status priority
    status_prio = {"pending": 0, "approved": 1, "executed": 2}
    out.sort(key=lambda e: (e.date, status_prio.get(e.status, 3)), reverse=True)
    return out
