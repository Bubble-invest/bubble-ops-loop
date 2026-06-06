"""
github_reader.py — read repo content (dept.yaml, queues/gates/*, outputs/, etc).

Two modes:
  - disk mode (READ_FROM_DISK set)  : direct filesystem reads from fixture root
  - github mode (default)           : `gh api repos/<org>/<repo>/contents/...`

Per the brief: gh calls are cached 60s in memory. For v1 we ship the disk-mode
reader (tests + local dev). The gh subprocess wrapper is a thin stub; UX-5 will
flesh it out when wiring to live Morty.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from console import settings
from console.services.dept_registry import repo_path

_log = logging.getLogger("console.github_reader")


def load_dept_yaml(slug: str) -> Optional[dict]:
    """Return dept.yaml (live) or dept.yaml.draft (a-eclore) as a dict."""
    root = repo_path(slug)
    if root is None:
        return None
    for fname in ("dept.yaml", "dept.yaml.draft"):
        p = root / fname
        if p.exists():
            try:
                return yaml.safe_load(p.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                _log.warning("yaml parse error for %s: %s", p, exc)
                return None
    return None


def load_dept_yaml_raw(slug: str) -> Optional[str]:
    """Return the raw text of dept.yaml(.draft) for verbatim rendering."""
    root = repo_path(slug)
    if root is None:
        return None
    for fname in ("dept.yaml", "dept.yaml.draft"):
        p = root / fname
        if p.exists():
            return p.read_text(encoding="utf-8")
    return None


def load_mandate_md(slug: str) -> Optional[str]:
    """Return the verbatim text of <repo>/MANDATE.md for a dept, or None
    if missing. {{OPERATOR}} reads this in the UI (both onboarding + operating
    phases) — it's the canonical contract for the dept's scope.
    {{OPERATOR}} flag 2026-05-24 msg 3118.
    """
    root = repo_path(slug)
    if root is None:
        return None
    p = root / "MANDATE.md"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("could not read %s: %s", p, exc)
        return None


def load_whiteboard_freeform(slug: str) -> Optional[str]:
    """Return the verbatim text of <repo>/whiteboard.md — the dept manager's
    free-space whiteboard, or None if missing/empty.

    {{OPERATOR}} msg 1174 (2026-06-01): the Tableau de bord needs a real blank
    canvas card the dept manager can fill with anything (different per dept,
    populated by the agent — e.g. Maya adds department-specific data). This
    is intentionally unstructured: whatever the agent writes is rendered
    verbatim. Distinct from whiteboard.yaml (structured KPI cards).
    """
    root = repo_path(slug)
    if root is None:
        return None
    p = root / "whiteboard.md"
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("could not read %s: %s", p, exc)
        return None
    return text if text.strip() else None


def load_layer_prompt_md(slug: str, layer_num: int) -> Optional[str]:
    """Return the verbatim text of <repo>/layers/<N>/PROMPT.md, or None
    if missing. {{OPERATOR}} flag 2026-05-24 msg 3137 — the UI must surface
    each subscribed layer's prompt so he can audit what the agent does
    at each moment of the day.

    Args:
        slug: dept slug.
        layer_num: layer number (1..4 typically). Out-of-range / non-int
            returns None silently.
    """
    try:
        n = int(layer_num)
    except (TypeError, ValueError):
        return None
    if n < 1 or n > 99:  # generous upper bound; layers above 4 not used today
        return None
    root = repo_path(slug)
    if root is None:
        return None
    p = root / "layers" / str(n) / "PROMPT.md"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("could not read %s: %s", p, exc)
        return None


def list_pending_gates(slug: str) -> List[Dict[str, Any]]:
    """Return all gate YAMLs in queues/gates/ for the dept."""
    root = repo_path(slug)
    if root is None:
        return []
    gates_dir = root / "queues" / "gates"
    if not gates_dir.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(gates_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                out.append(doc)
            else:
                # Parsed but not a mapping → surface a synthetic error card so a
                # malformed gate is VISIBLE in the cockpit, never silently dropped.
                out.append(_malformed_gate_card(slug, p, "not a YAML mapping"))
        except yaml.YAMLError as e:
            # A malformed gate card used to vanish here (silent `continue`) — that
            # is exactly how a TLT trade gate disappeared from the UI on
            # 2026-06-06 (unquoted colon in `instrument:`), so {{OPERATOR}} never saw it
            # to approve it. Surface it as an error card instead of swallowing it.
            out.append(_malformed_gate_card(slug, p, str(e).splitlines()[0]))
    return out


def _malformed_gate_card(slug: str, path: "Path", err: str) -> Dict[str, Any]:
    """A placeholder gate card making a parse failure VISIBLE in the cockpit."""
    return {
        "id": path.stem,
        "slug": slug,
        "kind": "malformed_gate",
        "requires_human": True,
        "current_mode": "manual_required",
        "error": f"gate YAML failed to parse ({err}) — fix {path.name} so it renders",
        "_malformed": True,
    }


def load_gate(slug: str, gate_id: str) -> Optional[Dict[str, Any]]:
    for g in list_pending_gates(slug):
        if g.get("id") == gate_id:
            return g
    return None


def load_gate_raw(slug: str, gate_id: str) -> Optional[str]:
    root = repo_path(slug)
    if root is None:
        return None
    p = root / "queues" / "gates" / f"{gate_id}.yaml"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def list_missions(slug: str) -> List[Dict[str, str]]:
    """Return all recurring missions declared by the dept.

    Per Notion v5 line 526 and the merged PR #1 (commit 2429b9c), missions
    declared INLINE in `dept.yaml::recurring_missions:` are spec-equivalent
    to file-based `missions/<id>.yaml`. Layer 1 data-curator resolves both
    forms identically, so the console must surface both.

    Returns:
        List of {"id": str, "source": "inline"|"file"} dicts, sorted by id
        and de-duplicated on id (inline wins if both forms declare same id).
    """
    out: Dict[str, Dict[str, str]] = {}

    # 1) Inline recurring_missions from dept.yaml
    dept_yaml = load_dept_yaml(slug)
    if isinstance(dept_yaml, dict):
        inline = dept_yaml.get("recurring_missions") or []
        if isinstance(inline, list):
            for m in inline:
                if isinstance(m, dict) and m.get("id"):
                    mid = str(m["id"])
                    out[mid] = {"id": mid, "source": "inline"}

    # 2) File-based missions/*.yaml
    root = repo_path(slug)
    if root is not None:
        md = root / "missions"
        if md.exists():
            for p in sorted(md.glob("*.yaml")):
                try:
                    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
                except yaml.YAMLError:
                    doc = None
                # Prefer the id declared inside the file, else stem of filename
                mid = None
                if isinstance(doc, dict):
                    mid = doc.get("id")
                if not mid:
                    mid = p.stem
                mid = str(mid)
                # Inline wins on id collision; file fills in otherwise.
                out.setdefault(mid, {"id": mid, "source": "file"})

    return [out[k] for k in sorted(out)]


def list_missions_full(slug: str) -> List[Dict[str, Any]]:
    """Return the full mission docs (id, layer, cadence, time, day,
    active_hours, description, input_sources, output_queue, creates,
    gate_policy_id) for every recurring mission declared by the dept.

    Merges INLINE `dept.yaml::recurring_missions[]` with FILE-based
    `missions/<id>.yaml` (same precedent as list_missions(): inline wins
    on id collision). Sorted by id.

    {{OPERATOR}} flag 2026-05-24 msg 3137: the UI must surface ALL mission
    fields verbatim, not just the slug. list_missions() (kept for
    back-compat) returns the abbreviated {id, source} dicts; this
    helper returns the full body so the templates can render cadence /
    description / creates / etc.
    """
    out: Dict[str, Dict[str, Any]] = {}

    # 1) Inline recurring_missions from dept.yaml
    dept_yaml = load_dept_yaml(slug)
    if isinstance(dept_yaml, dict):
        inline = dept_yaml.get("recurring_missions") or []
        if isinstance(inline, list):
            for m in inline:
                if isinstance(m, dict) and m.get("id"):
                    mid = str(m["id"])
                    out[mid] = dict(m)
                    out[mid]["id"] = mid
                    out[mid]["_source"] = "inline"

    # 2) File-based missions/*.yaml — only fill IDs not already present
    root = repo_path(slug)
    if root is not None:
        md = root / "missions"
        if md.exists():
            for p in sorted(md.glob("*.yaml")):
                try:
                    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
                except yaml.YAMLError:
                    continue
                if not isinstance(doc, dict):
                    continue
                mid = doc.get("id") or p.stem
                mid = str(mid)
                if mid in out:
                    continue  # inline wins
                doc["id"] = mid
                doc["_source"] = "file"
                out[mid] = doc

    return [out[k] for k in sorted(out)]


def list_artifacts(slug: str) -> Dict[str, List[str]]:
    """Return categorized list of artifact filenames present in the repo."""
    root = repo_path(slug)
    if root is None:
        return {}
    cats: Dict[str, List[str]] = {}
    for sub in ("missions", "skills", "tools", "policies", "tests",
                "queues/gates"):
        d = root / sub
        if d.exists():
            cats[sub] = sorted(p.name for p in d.glob("*"))
    if (root / "dept.yaml.draft").exists():
        cats["root"] = ["dept.yaml.draft"]
    elif (root / "dept.yaml").exists():
        cats["root"] = ["dept.yaml"]
    return cats


def read_chat_log(slug: str, step_num: int, step_name: str) -> Optional[str]:
    """Read onboarding/<N-stepname>/chat.log if it exists."""
    root = repo_path(slug)
    if root is None:
        return None
    p = root / "onboarding" / f"{step_num}-{step_name}" / "chat.log"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def write_gate_decision(slug: str, gate_id: str, decision: Dict[str, Any]
                         ) -> Optional[Path]:
    """Write inbox/decisions/<gate_id>.yaml. Returns path on success."""
    root = repo_path(slug)
    if root is None:
        return None
    decisions_dir = root / "inbox" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    out = decisions_dir / f"{gate_id}.yaml"
    out.write_text(yaml.safe_dump(decision, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# GAP 11 — Management cross-dept aggregation surface
# ---------------------------------------------------------------------------


def _find_latest_output_date(outputs_dir: Path) -> Optional[date]:
    """Return the most recent YYYY-MM-DD subdir under outputs/, or None."""
    if not outputs_dir.exists():
        return None
    dates: List[date] = []
    for child in outputs_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            d = date.fromisoformat(child.name)
            dates.append(d)
        except ValueError:
            continue
    return max(dates) if dates else None


def _load_child_entry(slug: str, child_root: Path) -> Dict[str, Any]:
    """Build one entry in the management-view children list for a single child dept.

    Returns a dict with:
      slug            str
      risk_kpis       dict | None
      risk_brief_md   str | None
      management_export dict | None
      pending_gates   list[dict]   (from queues/gates/*.yaml)
      staleness_days  int          (0 if data is from today)
      last_seen_at    str | None   (ISO date of the most recent Layer-4 output, or None)
    """
    today = date.today()
    outputs_dir = child_root / "outputs"
    latest_date = _find_latest_output_date(outputs_dir)

    # Compute staleness relative to today for the child's most recent Layer-4 run.
    # Strategy: look for any date dir that has a "4/" sub-subdir.
    # If none found, last_seen_at is None and staleness = days since the latest
    # *any* output, or a large sentinel if no outputs at all.
    latest_layer4_date: Optional[date] = None
    if outputs_dir.exists():
        for child_dir in outputs_dir.iterdir():
            if not child_dir.is_dir():
                continue
            try:
                d = date.fromisoformat(child_dir.name)
            except ValueError:
                continue
            if (child_dir / "4").is_dir():
                if latest_layer4_date is None or d > latest_layer4_date:
                    latest_layer4_date = d

    if latest_layer4_date is not None:
        staleness_days = (today - latest_layer4_date).days
        last_seen_at = latest_layer4_date.isoformat()
    elif latest_date is not None:
        # Has outputs, but no Layer-4 — count staleness from last output date.
        staleness_days = (today - latest_date).days
        last_seen_at = latest_date.isoformat()
    else:
        staleness_days = 9999  # no outputs at all
        last_seen_at = None

    # Load Layer-4 artifacts from today or the most recent Layer-4 date.
    risk_kpis: Optional[Dict[str, Any]] = None
    risk_brief_md: Optional[str] = None
    management_export: Optional[Dict[str, Any]] = None

    if latest_layer4_date is not None:
        date_str = latest_layer4_date.isoformat()
        layer4_dir = outputs_dir / date_str / "4"
        kpis_path = layer4_dir / "risk-kpis.yaml"
        brief_path = layer4_dir / "risk-brief.md"
        export_path = outputs_dir / date_str / "management-export.yaml"

        if kpis_path.exists():
            try:
                risk_kpis = yaml.safe_load(kpis_path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                risk_kpis = None

        if brief_path.exists():
            risk_brief_md = brief_path.read_text(encoding="utf-8")

        if export_path.exists():
            try:
                management_export = yaml.safe_load(export_path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                management_export = None

    # Pending gates
    pending_gates: List[Dict[str, Any]] = []
    gates_dir = child_root / "queues" / "gates"
    if gates_dir.exists():
        for p in sorted(gates_dir.glob("*.yaml")):
            try:
                doc = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    pending_gates.append(doc)
            except yaml.YAMLError:
                continue

    return {
        "slug": slug,
        "risk_kpis": risk_kpis,
        "risk_brief_md": risk_brief_md,
        "management_export": management_export,
        "pending_gates": pending_gates,
        "staleness_days": staleness_days,
        "last_seen_at": last_seen_at,
    }


def load_management_exports(dept_slug: str) -> Dict[str, Any]:
    """Aggregate Layer-4 bubble-up artifacts from all children of a management dept.

    Reads the dept's dept.yaml to discover hierarchy.children, then for each
    child reads (from READ_FROM_DISK/bubble-ops-<child>/outputs/):
      - latest-date/4/risk-kpis.yaml       (parsed YAML)
      - latest-date/4/risk-brief.md         (raw text)
      - latest-date/management-export.yaml  (parsed YAML)
      - queues/gates/*.yaml                 (list of pending gates)

    Gracefully handles:
      - Missing files: the corresponding field is None.
      - Stale data (no Layer-4 today): staleness_days >= 1, last_seen_at set.
      - Child repo not found on disk: child entry with staleness_days=9999.

    Args:
        dept_slug: slug of the management dept (e.g. "tony-test").

    Returns:
        {
          "dept_slug": str,
          "children": [
            {
              "slug": str,
              "risk_kpis": dict | None,
              "risk_brief_md": str | None,
              "management_export": dict | None,
              "pending_gates": list[dict],
              "staleness_days": int,
              "last_seen_at": str | None,
            },
            ...
          ],
          "total_open_gates": int,
          "stale_children": [str],   # slugs with staleness_days > 0
        }
    """
    dept_yaml = load_dept_yaml(dept_slug)
    children_slugs: List[str] = []
    if isinstance(dept_yaml, dict):
        hierarchy = dept_yaml.get("hierarchy", {}) or {}
        children_slugs = list(hierarchy.get("children", []) or [])

    disk_root = settings.disk_root() if settings.disk_mode() else None

    children_entries: List[Dict[str, Any]] = []
    for child_slug in children_slugs:
        if disk_root is not None:
            child_root = disk_root / f"bubble-ops-{child_slug}"
        else:
            child_root = Path(f"/srv/bubble-ops/repos/bubble-ops-{child_slug}")

        if child_root.exists():
            entry = _load_child_entry(child_slug, child_root)
        else:
            _log.warning(
                "load_management_exports: child repo not found on disk: %s", child_root
            )
            entry = {
                "slug": child_slug,
                "risk_kpis": None,
                "risk_brief_md": None,
                "management_export": None,
                "pending_gates": [],
                "staleness_days": 9999,
                "last_seen_at": None,
            }
        children_entries.append(entry)

    total_open_gates = sum(len(e["pending_gates"]) for e in children_entries)
    stale_children = [e["slug"] for e in children_entries if e["staleness_days"] > 0]

    return {
        "dept_slug": dept_slug,
        "children": children_entries,
        "total_open_gates": total_open_gates,
        "stale_children": stale_children,
    }


def load_recent_layer_output(slug: str, layer_num: int) -> Optional[Dict[str, Any]]:
    """Return recent output info for a specific layer.

    Reads .last-run (timestamp) and summary.md (excerpt) from the latest
    output date directory for that layer. Used by the dept detail kanban
    to show activity without reading the full summary.

    Returns dict with {last_run, summary_excerpt, date} or None.
    """
    try:
        n = int(layer_num)
    except (TypeError, ValueError):
        return None

    root = repo_path(slug)
    if root is None:
        return None

    outputs_dir = root / "outputs"
    latest_date = _find_latest_output_date(outputs_dir)
    if latest_date is None:
        return None

    layer_dir = outputs_dir / latest_date.isoformat() / str(n)
    if not layer_dir.exists():
        return None

    last_run: Optional[str] = None
    last_run_path = layer_dir / ".last-run"
    if last_run_path.exists():
        try:
            last_run = last_run_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    summary_excerpt: Optional[str] = None
    summary_path = layer_dir / "summary.md"
    if summary_path.exists():
        try:
            lines = summary_path.read_text(encoding="utf-8").splitlines()
            excerpt_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    excerpt_lines.append(stripped)
                    if len(excerpt_lines) >= 3:
                        break
            summary_excerpt = " ".join(excerpt_lines) if excerpt_lines else None
        except OSError:
            pass

    if last_run is None and summary_excerpt is None:
        return None

    return {
        "last_run": last_run,
        "summary_excerpt": summary_excerpt,
        "date": latest_date.isoformat(),
    }


def load_whiteboard(slug: str) -> Optional[Dict[str, Any]]:
    """Return parsed whiteboard.yaml for a dept, or None if missing.

    The whiteboard is a per-dept configurable file where the agent surfaces
    KPIs and a free-text commentary for {{OPERATOR}}. Expected schema:

        title: str
        updated_at: str (ISO datetime)
        kpis:
          - label: str
            value: str
            trend: up | down | stable  (optional)
            note: str  (optional)
        notes: str (free-text, optional)
    """
    root = repo_path(slug)
    if root is None:
        return None

    for fname in ("whiteboard.yaml", "whiteboard.yml"):
        p = root / fname
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except yaml.YAMLError as exc:
                _log.warning("yaml parse error for whiteboard %s: %s", p, exc)
                return None

    return None


def group_missions_by_layer(
    missions: List[Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    """Bucket a flat mission list into {layer_num: [missions]}.

    Used by the dept_detail + onboarding pages to render the 4 "Moments
    de la journée" with each Moment's missions nested inside, rather
    than as a separate "Rendez-vous récurrents" section that duplicates
    the same content ({{OPERATOR}} msg 3142, 2026-05-24).

    Missions without a `layer` field default to layer 1 (the materializer
    layer — by convention, recurring missions live on L1 unless
    explicitly bucketed elsewhere).
    """
    out: Dict[int, List[Dict[str, Any]]] = {1: [], 2: [], 3: [], 4: []}
    if not missions:
        return out
    for m in missions:
        if not isinstance(m, dict):
            continue
        layer_raw = m.get("layer", 1)
        try:
            layer = int(layer_raw)
        except (TypeError, ValueError):
            layer = 1
        if layer not in (1, 2, 3, 4):
            layer = 1
        out.setdefault(layer, []).append(m)
    return out
