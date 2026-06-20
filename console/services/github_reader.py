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
import base64
import subprocess
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
    if missing. Joris reads this in the UI (both onboarding + operating
    phases) — it's the canonical contract for the dept's scope.
    Joris flag 2026-05-24 msg 3118.
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

    Joris msg 1174 (2026-06-01): the Tableau de bord needs a real blank
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
    if missing. Joris flag 2026-05-24 msg 3137 — the UI must surface
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
    # Pre-compute the set of gate ids that already have a decision recorded in
    # inbox/decisions/.  The approval path (write_gate_decision) writes the
    # decision file there immediately when Joris clicks Approve/Reject in the
    # cockpit — BEFORE the dept's agent loop processes and resolves the gate
    # (which is when resolved:true would normally appear in the gate YAML).
    # Without this check there is a window — between Joris approving and the
    # dept agent draining the inbox — where the gate still appears as pending
    # in "Décisions qu'on attend de toi".  Skipping it as soon as its decision
    # file exists closes that window immediately.
    #
    # This check applies to host=vps depts (decision file written to disk by
    # write_gate_decision).  For host=local depts the decision is committed to
    # GitHub, not to the cockpit's local disk, so the inbox/ directory here may
    # not yet reflect it — but host=local gates are a separate case (Miranda on
    # Jade's Mac).  The live bug is Maya = vps, so the disk check fixes the
    # reported issue.  A future improvement could call the GitHub API for
    # host=local depts, but we keep the scope narrow here.
    decisions_dir = root / "inbox" / "decisions"
    decided_ids: set = set()
    if decisions_dir.is_dir():
        for dp in decisions_dir.glob("*.yaml"):
            decided_ids.add(dp.stem)

    out: List[Dict[str, Any]] = []
    for p in sorted(gates_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                # Skip gates that have been decided — they are no longer "pending."
                # Gate resolution writes `resolved: true` + `decided_by` (not
                # `approved_by`, which nothing ever sets), so the old single-field
                # check was dead: a decided-but-not-yet-archived gate kept showing
                # in the dept UI as a still-open choice. Honour all the fields the
                # resolution actually writes. (Joris 2026-06-19: approved trades
                # still appeared as pending in each agent's cockpit dept page.)
                if doc.get("approved_by") or doc.get("resolved") or doc.get("decided_by"):
                    continue
                # Also skip if a decision file already exists in inbox/decisions/
                # for this gate — the operator has acted but the agent hasn't
                # processed the inbox yet (see decided_ids computation above).
                # Use doc.get('id') so that a gate whose YAML id field differs
                # from its filename is still correctly matched against the
                # decision file written by write_gate_decision (which keys on
                # the gate's logical id, not the stem).  Fallback to p.stem
                # only when the YAML has no id field, for safety.
                gate_id = doc.get("id", p.stem)
                if gate_id in decided_ids:
                    continue
                out.append(doc)
            else:
                # Parsed but not a mapping → surface a synthetic error card so a
                # malformed gate is VISIBLE in the cockpit, never silently dropped.
                out.append(_malformed_gate_card(slug, p, "not a YAML mapping"))
        except yaml.YAMLError as e:
            # A malformed gate card used to vanish here (silent `continue`) — that
            # is exactly how a TLT trade gate disappeared from the UI on
            # 2026-06-06 (unquoted colon in `instrument:`), so Joris never saw it
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


def resolve_chart_path(slug: str, rel_path: str) -> Optional[Path]:
    """Securely resolve a gate card's `chart_path` to an on-disk PNG, or None.

    Ben's data contract (fixed): chart PNGs live at
        outputs/<YYYY-MM-DD>/charts/<NAME>-90d.png
    relative to the dept repo root. The cockpit serves these inline in the
    gate detail view via GET /gate/<slug>/chart?path=<rel_path>.

    This is the ONE place a bug becomes an arbitrary-file-disclosure vuln, so
    the validation is deliberately strict and layered. A path is served ONLY
    when ALL of the following hold:

      1. `slug` resolves to a real dept repo on disk (dept-scoping: the chart
         can only ever come from THIS dept's repo root, never another dept's
         or an arbitrary host path).
      2. `rel_path` is relative (no leading "/", no drive), contains no parent
         refs, and after resolution still sits *inside* the repo root —
         specifically inside `<repo_root>/outputs/<date>/charts/`.
      3. The resolved real path (symlinks followed) is STILL inside that
         charts dir — so a symlink planted inside charts/ that points outside
         the repo is rejected.
      4. The basename ends in `.png` (case-insensitive) — exactly one
         extension, so `evil.png.txt` and `passwd` are rejected.
      5. The file actually exists and is a regular file.

    Returns the resolved Path on success, or None on ANY failure (caller maps
    None → 404, never leaking *why*). Never raises.
    """
    if not rel_path or not isinstance(rel_path, str):
        return None

    root = repo_path(slug)
    if root is None:
        return None
    root = root.resolve()

    # Reject absolute paths and Windows drive/UNC forms outright. We only ever
    # accept a repo-relative path.
    candidate = Path(rel_path)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        return None
    # Reject any explicit parent-dir component before we even touch the FS.
    if ".." in candidate.parts:
        return None

    # Extension gate: exactly a .png basename (case-insensitive).
    if candidate.suffix.lower() != ".png":
        return None

    # Resolve against the repo root WITHOUT requiring existence first, then
    # confirm containment. We require the path to be:
    #   <root>/outputs/<something>/charts/<name>.png  (>= depth 4 components)
    parts = candidate.parts
    if len(parts) < 4 or parts[0] != "outputs" or parts[2] != "charts":
        return None

    charts_dir = (root / parts[0] / parts[1] / parts[2]).resolve()
    # The expected charts dir must itself be inside the repo root (defends
    # against a weird parts[1] that resolves out — belt and suspenders).
    if not _is_within(charts_dir, root):
        return None

    target = (root / candidate)
    # Lexical containment check on the *unresolved* path (catches ../ that
    # slipped past the parts check via odd encodings).
    try:
        target_abs = target.resolve()
    except (OSError, RuntimeError):
        return None
    if not _is_within(target_abs, charts_dir):
        return None

    # Symlink-escape defence: the REAL path (all symlinks resolved) must still
    # be inside the charts dir. `.resolve()` already followed symlinks above;
    # re-confirm the real charts dir contains it.
    if not _is_within(target_abs, charts_dir):
        return None

    if not target_abs.is_file():
        return None
    # Final paranoia: the real file's parent must be the real charts dir, and
    # the file must not be a symlink pointing elsewhere (lstat the original).
    try:
        if target.is_symlink():
            real = target.resolve()
            if not _is_within(real, charts_dir):
                return None
    except OSError:
        return None

    return target_abs


def _is_within(path: Path, base: Path) -> bool:
    """True iff `path` is `base` or a descendant of it (both already resolved)."""
    try:
        return path == base or path.is_relative_to(base)
    except AttributeError:  # pragma: no cover — Path.is_relative_to is 3.9+
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False


# ── Attachments — allowed extensions (NO .html/.js/executable) ───────────────
# SVG is allowed but served with a restrictive CSP (see gate.py / resolve below).
# Double-extensions like "evil.pdf.html" are rejected because only the LAST
# suffix is checked AND the full basename is also validated to contain exactly
# one dot separator for the allowed suffix — enforced via suffix check.
_ATTACHMENT_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"})
_ATTACHMENT_FILE_EXTS  = frozenset({".pdf", ".csv", ".txt", ".md"})
_ATTACHMENT_ALLOWED_EXTS = _ATTACHMENT_IMAGE_EXTS | _ATTACHMENT_FILE_EXTS

# Content-Type map for every allowed extension.
_ATTACHMENT_MEDIA_TYPES: dict = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".svg":  "image/svg+xml",
    ".webp": "image/webp",
    ".pdf":  "application/pdf",
    ".csv":  "text/csv",
    ".txt":  "text/plain",
    ".md":   "text/markdown",
}


def attachment_media_type(path: Path) -> str:
    """Return the correct Content-Type for a resolved attachment path.

    Defaults to application/octet-stream for anything not in our allowlist
    (should never happen if resolve_attachment_path is the sole gatekeeper,
    but belt-and-suspenders so a caller getting a stale Path still behaves).
    """
    return _ATTACHMENT_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def resolve_attachment_path(slug: str, rel_path: str) -> Optional[Path]:
    """Securely resolve a gate attachment path to an on-disk file, or None.

    Attachments live at:
        outputs/<YYYY-MM-DD>/attachments/<NAME>.<ext>
    relative to the dept repo root.  The cockpit serves them via
    GET /gate/<slug>/attachment?path=<rel_path>.

    Modelled EXACTLY on resolve_chart_path; the same layered checks apply.
    A path is served ONLY when ALL of the following hold:

      1. `slug` resolves to a real dept repo on disk (dept-scoping).
      2. `rel_path` is relative, contains no parent refs, and resolves to a
         path still inside <repo_root>/outputs/<date>/attachments/ — mirrors
         the charts/ containment check (parts[0]=="outputs", parts[2]=="attachments",
         depth >= 4).
      3. The resolved real path (symlinks followed) is STILL inside that
         attachments dir — so a symlink planted inside attachments/ that points
         outside is rejected.
      4. The file extension (last suffix, case-insensitive) is in the
         _ATTACHMENT_ALLOWED_EXTS allowlist. A double-extension like
         "x.pdf.html" has suffix ".html" which is NOT in the allowlist — rejected.
      5. The file actually exists and is a regular file.

    Returns the resolved Path on success, or None on ANY failure.
    The caller maps None → opaque 404, never leaking why. Never raises.
    """
    if not rel_path or not isinstance(rel_path, str):
        return None

    root = repo_path(slug)
    if root is None:
        return None
    root = root.resolve()

    # Reject absolute paths and Windows drive/UNC forms outright.
    candidate = Path(rel_path)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        return None
    # Reject any explicit parent-dir component before touching the FS.
    if ".." in candidate.parts:
        return None

    # Extension gate: must be in our allowlist (last suffix only — so
    # "evil.pdf.html" has suffix ".html" and is rejected).
    if candidate.suffix.lower() not in _ATTACHMENT_ALLOWED_EXTS:
        return None

    # Structural check: must be outputs/<something>/attachments/<name>.<ext>
    # (>= 4 parts: outputs, <date>, attachments, <filename>).
    parts = candidate.parts
    if len(parts) < 4 or parts[0] != "outputs" or parts[2] != "attachments":
        return None

    # Resolve the containing directory and confirm it stays inside repo root.
    attachments_dir = (root / parts[0] / parts[1] / parts[2]).resolve()
    if not _is_within(attachments_dir, root):
        return None

    target = root / candidate
    # Resolve to real path (follows symlinks).
    try:
        target_abs = target.resolve()
    except (OSError, RuntimeError):
        return None

    # Containment: resolved path must be inside the resolved attachments dir.
    if not _is_within(target_abs, attachments_dir):
        return None

    # Symlink-escape defence: the original path must not be a symlink pointing
    # outside the attachments dir.
    try:
        if target.is_symlink():
            real = target.resolve()
            if not _is_within(real, attachments_dir):
                return None
    except OSError:
        return None

    if not target_abs.is_file():
        return None

    return target_abs


# ── Kanban attachment resolver ──────────────────────────────────────────────
# Mirror of resolve_attachment_path but repo-scoped instead of dept-slug-scoped.
# The kanban board (Bubble-invest/bubble-ops-board) and any fleet repo can have
# images in outputs/*/(diagrams|attachments|charts)/ that decision cards reference
# via visual_attachments: in the issue body. The cockpit serves them through
# GET /kanban/attachment?repo=<org/repo>&path=<rel_path>.

_KANBAN_ATTACHMENT_SUBDIRS = frozenset({"diagrams", "attachments", "charts"})


def resolve_kanban_attachment_path(repo: str, rel_path: str) -> Optional[Path]:
    """Securely resolve a kanban-card `visual_attachments` path to a file, or None.

    Attachments live at:
        outputs/<YYYY-MM-DD>/(diagrams|attachments|charts)/<NAME>.<ext>
    relative to the repo root. The cockpit serves them via
    GET /kanban/attachment?repo=<org/repo>&path=<rel_path>.

    Modelled EXACTLY on resolve_attachment_path; the same layered checks apply.
    The caller maps None → opaque 404, never leaking why. Never raises.
    """
    if not rel_path or not isinstance(rel_path, str):
        return None
    if not repo or not isinstance(repo, str):
        return None

    # Resolve repo root: look up via dept_registry.repo_root_for_org_repo(),
    # which normalises org/repo → on-disk checkout. Falls back to the board repo
    # itself (REPOS_ROOT / "bubble-ops-board") for cross-repo kanban attachments.
    root = repo_path_for_org_repo(repo)
    if root is None:
        return None
    root = root.resolve()

    # Reject absolute paths and Windows drive/UNC forms outright.
    candidate = Path(rel_path)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        return None
    # Reject any explicit parent-dir component before touching the FS.
    if ".." in candidate.parts:
        return None

    # Extension gate: must be in the same allowlist used for gate attachments.
    if candidate.suffix.lower() not in _ATTACHMENT_ALLOWED_EXTS:
        return None

    # Structural check: outputs/<date>/(diagrams|attachments|charts)/<name>.<ext>
    # (>= 4 parts: outputs, <date>, <subdir>, <filename>)
    parts = candidate.parts
    if len(parts) < 4 or parts[0] != "outputs" or parts[2] not in _KANBAN_ATTACHMENT_SUBDIRS:
        return None

    # Resolve the container dir and confirm it stays inside repo root.
    container_dir = (root / parts[0] / parts[1] / parts[2]).resolve()
    if not _is_within(container_dir, root):
        return None

    target = root / candidate
    # Resolve to real path (follows symlinks).
    try:
        target_abs = target.resolve()
    except (OSError, RuntimeError):
        return None

    # Containment: resolved path must be inside the resolved container dir.
    if not _is_within(target_abs, container_dir):
        return None

    # Symlink-escape defence.
    try:
        if target.is_symlink():
            real = target.resolve()
            if not _is_within(real, container_dir):
                return None
    except OSError:
        return None

    if not target_abs.is_file():
        return None

    return target_abs


def repo_path_for_org_repo(org_repo: str) -> Optional[Path]:
    """Resolve an org/repo string (e.g. 'Bubble-invest/bubble-ops-board') to a
    local checkout path. Returns None if the repo is not checked out on this host.
    Uses the same disk_root() base as repo_path() for dept repos."""
    import re
    if not re.match(r'^[\w.-]+/[\w.-]+$', org_repo):
        return None
    org, name = org_repo.split("/", 1)
    # Reject ".." and "." as either component — the regex above allows them
    # because [\w.-]+ matches "..", but they enable path traversal.
    if org in ("..", ".") or name in ("..", "."):
        return None
    disk_root = settings.disk_root().resolve()
    # Try as a dept repo first (bubble-ops-<slug> or <slug>)
    candidate = disk_root / f"bubble-ops-{name}"
    if candidate.is_dir():
        # Defensive containment: the resolved candidate must STAY inside disk_root
        try:
            if _is_within(candidate.resolve(), disk_root):
                return candidate
        except (OSError, RuntimeError):
            pass
        return None
    # Try unprefixed (for the board repo and concierges)
    candidate = disk_root / name
    if candidate.is_dir():
        try:
            if _is_within(candidate.resolve(), disk_root):
                return candidate
        except (OSError, RuntimeError):
            pass
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

    Joris flag 2026-05-24 msg 3137: the UI must surface ALL mission
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
                except yaml.YAMLError as e:
                    # Don't swallow silently — a malformed mission file would
                    # otherwise just disappear (same class as the gate bug).
                    _log.warning("malformed mission YAML %s: %s", p, str(e).splitlines()[0])
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
    """Land the operator's approval in inbox/decisions/<gate_id>.yaml where the
    dept's loop reads it. Returns the path (vps) / a sentinel path (local) on
    success, or None on failure.

    Hybrid local/VPS agent (2026-06-12): a dept declares its host in STATE.yaml.
      - host=vps (default): the dept repo is on the cockpit's disk → write to disk.
      - host=local (e.g. Miranda on Jade's Mac): the repo is NOT on the cockpit's
        disk — it lives on the Mac + GitHub. We commit the decision to the dept's
        GitHub repo via `gh api` so the Mac loop pulls it on its next safe_pull.
    """
    # Resolve the dept's host (default vps). Reference the function through the
    # MODULE (dept_registry.get_department) rather than a `from … import` binding,
    # so a test's monkeypatch on the module attribute is always honoured.
    from console.services import dept_registry as _dr
    dept = _dr.get_department(slug)
    host = getattr(dept, "host", "vps") if dept is not None else "vps"

    if host == "local":
        return _write_gate_decision_github(slug, gate_id, decision)

    # host=vps — write to the on-disk repo (unchanged).
    root = repo_path(slug)
    if root is None:
        return None
    decisions_dir = root / "inbox" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    out = decisions_dir / f"{gate_id}.yaml"
    out.write_text(yaml.safe_dump(decision, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    return out


def _write_gate_decision_github(slug: str, gate_id: str, decision: Dict[str, Any]
                                ) -> Optional[Path]:
    """Commit inbox/decisions/<gate_id>.yaml to the dept's GitHub repo via gh api
    (for host=local depts, whose repo isn't on the cockpit's disk). Returns a
    sentinel Path on success (the repo-relative path), None on failure."""
    repo = f"bubble-ops-{slug}"
    rel_path = f"inbox/decisions/{gate_id}.yaml"
    body = yaml.safe_dump(decision, sort_keys=False, allow_unicode=True)
    content_b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
    # PUT repos/<org>/<repo>/contents/<path> creates (or updates) a file in one
    # commit. -f content=<base64>, -f message=<msg>. (For an update GitHub also
    # needs the blob sha; a fresh gate decision id is new each time, so create.)
    try:
        r = subprocess.run(
            ["gh", "api", "-X", "PUT",
             f"repos/{settings.GITHUB_ORG}/{repo}/contents/{rel_path}",
             "-f", f"message=cockpit: gate {gate_id} decision",
             "-f", f"content={content_b64}"],
            capture_output=True, text=True, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("gate decision gh api failed: %s", exc)
        return None
    if r.returncode != 0:
        logging.getLogger(__name__).warning(
            "gate decision gh api PUT failed (rc=%s): %s",
            r.returncode, (r.stderr or r.stdout or "")[:200])
        return None
    return Path(rel_path)


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
        for gp in sorted(gates_dir.glob("*.yaml")):
            try:
                doc = yaml.safe_load(gp.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    pending_gates.append(doc)
                else:
                    pending_gates.append(_malformed_gate_card(slug, gp, "not a YAML mapping"))
            except yaml.YAMLError as e:
                # Same silent-drop trap as list_pending_gates (2026-06-06): a
                # malformed child gate must stay VISIBLE in the management rollup.
                pending_gates.append(_malformed_gate_card(slug, gp, str(e).splitlines()[0]))

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
    KPIs and a free-text commentary for Joris. Expected schema:

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
    the same content (Joris msg 3142, 2026-05-24).

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
