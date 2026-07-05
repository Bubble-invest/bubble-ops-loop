"""
github_reader.py — read repo content (dept.yaml, queues/gates/*, outputs/, etc).

Two modes:
  - disk mode (READ_FROM_DISK set)  : direct filesystem reads from fixture root
  - github mode (default)           : `gh api repos/<org>/<repo>/contents/...`

gh api calls are NOT cached here — each read hits disk (disk mode) or shells
out to `gh` (github mode) fresh. (The kanban route has its own short-TTL cache
around its snapshot; that lives in routes/dept.py, not here.) For v1 we ship
the disk-mode reader (tests + local dev). The gh subprocess wrapper is a thin
stub; UX-5 will flesh it out when wiring to live Morty.
"""
from __future__ import annotations

import logging
import base64
import os
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from console import settings
from console.services.dept_registry import repo_path
from console.services.humanize import humanize_queue_item

_log = logging.getLogger("console.github_reader")

# A short-lived GitHub-App installation token scoped to **contents:write** on the
# Bubble-invest dept repos, minted by a root timer into this tmpfs file (0640,
# root:claude), mirroring the issues-only board token used by routes/kanban.py.
# host=local depts (e.g. content/Miranda) have NO repo on the cockpit disk, so
# their decisions must be committed to GitHub — which needs auth the console
# service otherwise lacks (it runs NoNewPrivileges, no `gh auth`, no PAT). The
# console only reads this file and passes the token to `gh` via GH_TOKEN.
_CONTENTS_TOKEN_FILE = "/run/bubble-ops-contents/token"


def _read_contents_token() -> Optional[str]:
    """Read the short-lived contents:write token the root timer mints.
    Fallback to GH_TOKEN/GITHUB_TOKEN env (dev/CI). None if none available."""
    try:
        tok = open(_CONTENTS_TOKEN_FILE).read().strip()
        if tok:
            return tok
    except OSError:
        pass
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


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


# Platform default model — mirrors isolation_scaffold.DEFAULT_MODEL (the
# fallback isolation_scaffold writes into .claude/settings.json when a dept
# doesn't pin its own `department.model`). Kept as a plain string constant
# here (not imported from the onboarding-guide skill) to avoid a cross-
# package import from console -> skills for a single literal.
DEFAULT_AGENT_MODEL = "claude-opus-4-8[1m]"  # canonical Opus id (verified via claude-api docs) + fleet 1M-context modifier; matches isolation_scaffold.DEFAULT_MODEL
DEFAULT_AGENT_RUNTIME = "claude"


def load_agent_model_info(dept_yaml: Optional[dict]) -> Dict[str, Any]:
    """Return the per-dept agent deployment facts surfaced on /dept/<slug>:
    which model the dept's agent process is launched with, its runtime
    (claude vs a non-Claude backend like deepseek), hierarchy level, and
    subscribed layers.

    `department.model` already exists in dept.schema.yaml as "the value
    written into .claude/settings.json `model` by isolation_scaffold" — the
    ground-truth deployment fact for a dept's orchestrator process. This
    function reads the SAME field (no new parallel field for the model
    itself) and adds a `runtime` field (new, defaults to "claude") so a
    dept whose agent isn't a Claude process at all can declare that
    distinctly from "no model pinned, using the platform default".

    Never raises: a missing/malformed dept_yaml degrades to the platform
    defaults, never a KeyError.
    """
    dept = {}
    if isinstance(dept_yaml, dict):
        dept = dept_yaml.get("department") or {}
        if not isinstance(dept, dict):
            dept = {}

    model = dept.get("model")
    if not (isinstance(model, str) and model.strip()):
        model = DEFAULT_AGENT_MODEL
        model_declared = False
    else:
        model = model.strip()
        model_declared = True

    runtime = dept.get("runtime")
    if not (isinstance(runtime, str) and runtime.strip()):
        runtime = DEFAULT_AGENT_RUNTIME
    else:
        runtime = runtime.strip()

    hierarchy = {}
    layers = None
    host = None
    if isinstance(dept_yaml, dict):
        h = dept_yaml.get("hierarchy")
        if isinstance(h, dict):
            hierarchy = h
        l = dept_yaml.get("layers")
        if isinstance(l, dict):
            subscribed = l.get("subscribed")
            if isinstance(subscribed, list):
                layers = subscribed
        host = dept_yaml.get("host")

    hierarchy_level = hierarchy.get("level") if isinstance(hierarchy, dict) else None
    if not hierarchy_level:
        # Back-compat: some fixtures only set department.level, not the
        # hierarchy.level mirror (schema says the two MUST agree).
        hierarchy_level = dept.get("level")

    return {
        "model": model,
        "model_declared": model_declared,
        "runtime": runtime,
        "hierarchy_level": hierarchy_level,
        "layers": layers,
        "host": host,
    }


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


def _parse_gate_files(gates_dir: Path) -> List[tuple]:
    """Glob + parse every queues/gates/*.yaml once. Returns a list of
    (path, doc_or_None, yaml_error_or_None) tuples — doc is the parsed dict
    (or None if the parse failed / wasn't a mapping). Shared by
    list_pending_gates and list_layer_queues so a request only walks/parses
    this directory a single time instead of twice."""
    out: List[tuple] = []
    for p in sorted(gates_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            out.append((p, None, e))
            continue
        if isinstance(doc, dict):
            out.append((p, doc, None))
        else:
            out.append((p, None, None))  # parsed but not a mapping
    return out


def list_pending_gates(slug: str) -> List[Dict[str, Any]]:
    """Return all gate YAMLs in queues/gates/ for the dept.

    Gates are hidden (excluded) when they have a terminal decision, EXCEPT
    when action == 'modify': a modify decision means "redraft, I'll re-review",
    so the gate is kept visible and annotated with _revision_requested=True and
    _revision_comment so the UI can show it as pending-but-awaiting-redraft.

    NOTE: a true end-to-end "agent links the redraft to the original gate id"
    requires a dept-side convention (out of scope here). The cockpit side is
    correct: modify stays visible + flagged until the agent resolves the gate.
    """
    root = repo_path(slug)
    if root is None:
        return []
    gates_dir = root / "queues" / "gates"
    if not gates_dir.exists():
        return []
    return _filter_pending_gates(slug, root, _parse_gate_files(gates_dir))


def _filter_pending_gates(slug: str, root: Path, gate_files: List[tuple]) -> List[Dict[str, Any]]:
    """Apply the decision-filter logic (terminal decisions hidden, `modify`
    stays visible + flagged) to an already-parsed gate_files list. Split out
    of list_pending_gates so list_layer_queues can reuse a single parse of
    queues/gates/ without re-globbing/re-parsing it (board #450)."""
    # Pre-compute the set of gate ids that already have a decision recorded in
    # inbox/decisions/.  The approval path (write_gate_decision) writes the
    # decision file there immediately when {{OPERATOR}} clicks Approve/Reject in the
    # cockpit — BEFORE the dept's agent loop processes and resolves the gate
    # (which is when resolved:true would normally appear in the gate YAML).
    # Without this check there is a window — between {{OPERATOR}} approving and the
    # dept agent draining the inbox — where the gate still appears as pending
    # in "Décisions qu'on attend de toi".  Skipping it as soon as its decision
    # file exists closes that window immediately.
    #
    # This check applies to host=vps depts (decision file written to disk by
    # write_gate_decision).  For host=local depts the decision is committed to
    # GitHub, not to the cockpit's local disk, so the inbox/ directory here may
    # not yet reflect it — but host=local gates are a separate case (Miranda on
    # {{OPERATOR_2}}'s Mac).  The live bug is Maya = vps, so the disk check fixes the
    # reported issue.  A future improvement could call the GitHub API for
    # host=local depts, but we keep the scope narrow here.
    decisions_dir = root / "inbox" / "decisions"
    # Map gate_id → decision doc (for modify detection).
    decided_map: Dict[str, Any] = {}
    if decisions_dir.is_dir():
        for dp in decisions_dir.glob("*.yaml"):
            try:
                ddoc = yaml.safe_load(dp.read_text(encoding="utf-8"))
                if isinstance(ddoc, dict):
                    decided_map[dp.stem] = ddoc
                else:
                    decided_map[dp.stem] = {}
            except yaml.YAMLError:
                decided_map[dp.stem] = {}

    out: List[Dict[str, Any]] = []
    for p, doc, err in gate_files:
        if err is not None:
            # A malformed gate card used to vanish here (silent `continue`) — that
            # is exactly how a TLT trade gate disappeared from the UI on
            # 2026-06-06 (unquoted colon in `instrument:`), so {{OPERATOR}} never saw it
            # to approve it. Surface it as an error card instead of swallowing it.
            out.append(_malformed_gate_card(slug, p, str(err).splitlines()[0]))
            continue
        if doc is None:
            # Parsed but not a mapping → surface a synthetic error card so a
            # malformed gate is VISIBLE in the cockpit, never silently dropped.
            out.append(_malformed_gate_card(slug, p, "not a YAML mapping"))
            continue
        # Skip gates that have been decided — they are no longer "pending."
        # Gate resolution writes `resolved: true` + `decided_by` (not
        # `approved_by`, which nothing ever sets), so the old single-field
        # check was dead: a decided-but-not-yet-archived gate kept showing
        # in the dept UI as a still-open choice. Honour all the fields the
        # resolution actually writes. ({{OPERATOR}} 2026-06-19: approved trades
        # still appeared as pending in each agent's cockpit dept page.)
        if doc.get("approved_by") or doc.get("resolved") or doc.get("decided_by"):
            continue
        # Also skip if a decision file already exists in inbox/decisions/
        # for this gate — the operator has acted but the agent hasn't
        # processed the inbox yet (see decided_map computation above).
        # Use doc.get('id') so that a gate whose YAML id field differs
        # from its filename is still correctly matched against the
        # decision file written by write_gate_decision (which keys on
        # the gate's logical id, not the stem).  Fallback to p.stem
        # only when the YAML has no id field, for safety.
        gate_id = doc.get("id", p.stem)
        if gate_id in decided_map:
            ddoc = decided_map[gate_id]
            if ddoc.get("action") == "modify":
                # modify is NOT terminal: keep the gate visible so {{OPERATOR}}
                # can re-review after the agent redrafts it. Flag it so
                # the UI can show the "En révision" banner.
                doc["_revision_requested"] = True
                doc["_revision_comment"] = ddoc.get("comment", "")
                out.append(doc)
            # approve / reject / defer → hide (terminal decisions)
            continue
        out.append(doc)
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


def load_gate_direct(slug: str, gate_id: str) -> Optional[Dict[str, Any]]:
    """Return the raw gate YAML as a dict, ignoring decision-filter state.

    Unlike load_gate (which goes through list_pending_gates and hides decided
    gates), this reads the gate YAML on disk directly. Used by the undo route
    to check whether the gate was already resolved by the agent (resolved/
    decided_by set in the YAML means the agent has acted and undo is too late).
    Returns None if the gate YAML does not exist or cannot be parsed.

    SECURITY: gate_id is validated to contain no path separators or parent-dir
    components before being used to construct a filesystem path.
    """
    # Reject gate_ids that could escape the gates directory.
    if not gate_id or "/" in gate_id or "\\" in gate_id or ".." in gate_id:
        return None
    root = repo_path(slug)
    if root is None:
        return None
    gates_dir = (root / "queues" / "gates").resolve()
    p = (root / "queues" / "gates" / f"{gate_id}.yaml").resolve()
    # Containment check: the resolved path must stay inside the gates dir.
    if not _is_within(p, gates_dir):
        return None
    if not p.exists():
        return None
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except yaml.YAMLError:
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
      - host=local (e.g. Miranda on {{OPERATOR_2}}'s Mac): the repo is NOT on the cockpit's
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
        out = _write_gate_decision_github(slug, gate_id, decision)
        # Instant-hide: a host=local decision lands on GitHub, so the card would
        # otherwise linger in the cockpit until the agent pulls it, resolves the
        # gate, and pushes `resolved:true` back (minutes when the loop is alive,
        # up to the launchd backstop otherwise). If the dept's repo is ALSO
        # mirrored on the cockpit disk (the hybrid case — gates render from it),
        # drop the same decision file there as a local hide-marker so
        # list_pending_gates filters the card immediately, exactly like host=vps.
        # Best-effort, only on a successful GitHub commit; never fails the call.
        if out is not None:
            _write_local_hide_marker(slug, gate_id, decision)
        return out

    # host=vps — write to the on-disk repo. Atomic: write to a temp file in the
    # same dir + os.replace, so a reader (the dept's loop, or another cockpit
    # request) never observes a partially-written decision file.
    root = repo_path(slug)
    if root is None:
        return None
    decisions_dir = root / "inbox" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    out = decisions_dir / f"{gate_id}.yaml"
    _atomic_write_text(out, yaml.safe_dump(decision, sort_keys=False, allow_unicode=True))
    return out


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: temp file in the same dir, then
    os.replace. Avoids a reader ever seeing a truncated/partial file."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_local_hide_marker(slug: str, gate_id: str, decision: Dict[str, Any]
                             ) -> None:
    """For a host=local dept whose repo is ALSO mirrored on the cockpit disk,
    drop the decision file into the local inbox/decisions/ as a hide-marker so
    list_pending_gates filters the card immediately (the authoritative copy still
    went to GitHub). Best-effort: silently no-op if the repo isn't on disk or the
    write fails — it must never turn a successful GitHub commit into an error."""
    try:
        root = repo_path(slug)
        if root is None:
            return  # no on-disk mirror (pure local dept) — nothing to mark
        decisions_dir = root / "inbox" / "decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        (decisions_dir / f"{gate_id}.yaml").write_text(
            yaml.safe_dump(decision, sort_keys=False, allow_unicode=True),
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "local hide-marker write failed for %s/%s: %s", slug, gate_id, exc)


def _write_gate_decision_github(slug: str, gate_id: str, decision: Dict[str, Any]
                                ) -> Optional[Path]:
    """Commit inbox/decisions/<gate_id>.yaml to the dept's GitHub repo via gh api
    (for host=local depts, whose repo isn't on the cockpit's disk). Returns a
    sentinel Path on success (the repo-relative path), None on failure."""
    repo = f"bubble-ops-{slug}"
    rel_path = f"inbox/decisions/{gate_id}.yaml"
    contents_api = f"repos/{settings.GITHUB_ORG}/{repo}/contents/{rel_path}"
    body = yaml.safe_dump(decision, sort_keys=False, allow_unicode=True)
    content_b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")

    # The console runs without ambient `gh auth` (NoNewPrivileges, no PAT), so we
    # inject a short-lived contents:write token via GH_TOKEN. Without it the PUT
    # 401s and the decision silently never reaches GitHub (the host=local
    # delivery bug: Miranda/content never received cockpit decisions). gh prefers
    # GH_TOKEN over any stored auth, so this is the one auth source.
    token = _read_contents_token()
    env = {**os.environ, "GH_TOKEN": token} if token else dict(os.environ)

    def _gh(args: list) -> "subprocess.CompletedProcess":
        return subprocess.run(["gh", "api", *args],
                              capture_output=True, text=True, check=False, env=env,
                              timeout=30)

    def _put(extra: list) -> "subprocess.CompletedProcess":
        # PUT repos/<org>/<repo>/contents/<path> creates OR updates a file in one
        # commit. gate_id travels as a DISCRETE argv element (no shell), so a
        # gate_id with shell metacharacters can't break out — see hybrid tests.
        return _gh(["-X", "PUT", contents_api,
                    "-f", f"message=cockpit: gate {gate_id} decision",
                    "-f", f"content={content_b64}", *extra])

    try:
        r = _put([])
        if r.returncode != 0 and _looks_like_already_exists(r):
            # File already there (re-decision): GitHub needs the existing blob
            # sha to update. Fetch it and retry once as an update. A bare 422
            # without this would otherwise look like a hard failure.
            sha = _gh_contents_sha(contents_api, env)
            if sha:
                r = _put(["-f", f"sha={sha}"])
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("gate decision gh api failed: %s", exc)
        return None
    if r.returncode != 0:
        logging.getLogger(__name__).warning(
            "gate decision gh api PUT failed (rc=%s): %s",
            r.returncode, (r.stderr or r.stdout or "")[:200])
        return None
    return Path(rel_path)


def _looks_like_already_exists(r: "subprocess.CompletedProcess") -> bool:
    """True if a contents PUT failed because the file exists (needs blob sha)."""
    blob = (r.stderr or "") + (r.stdout or "")
    return "sha" in blob and ("422" in blob or "wasn't supplied" in blob
                              or "already exists" in blob)


def _gh_contents_sha(contents_api: str, env: Dict[str, str]) -> Optional[str]:
    """GET a contents path's current blob sha (for an update PUT). None on any
    failure — caller then leaves the original error in place."""
    import json
    try:
        r = subprocess.run(["gh", "api", contents_api, "--jq", ".sha"],
                           capture_output=True, text=True, check=False, env=env,
                           timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        logging.getLogger(__name__).warning(
            "gh contents sha lookup failed for %s (rc=%s): %s",
            contents_api, r.returncode, (r.stderr or r.stdout or "")[:200])
    except subprocess.TimeoutExpired as exc:
        logging.getLogger(__name__).warning(
            "gate decision gh api sha lookup timed out: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "gh contents sha lookup raised for %s: %s", contents_api, exc)
    return None


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
            except yaml.YAMLError as e:
                # A broken risk-KPI file otherwise silently blanks this child in
                # the management rollup with no trace — log it.
                _log.warning("_load_child_entry: malformed %s: %s", kpis_path, e)
                risk_kpis = None

        if brief_path.exists():
            risk_brief_md = brief_path.read_text(encoding="utf-8")

        if export_path.exists():
            try:
                management_export = yaml.safe_load(export_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                _log.warning("_load_child_entry: malformed %s: %s", export_path, e)
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
    KPIs and a free-text commentary for {{OPERATOR}}. Expected schema:

        title: str
        updated_at: str (ISO datetime)
        kpis:
          - label: str
            value: str
            trend: up | down | stable  (optional)
            note: str  (optional)
        notes: str OR list[str] (free-text, optional)

    `notes` is documented as a single free-text string, but some depts
    (Ben) populate it as a YAML list of dated decision-log entries instead.
    Jinja renders a Python list via repr() when dumped directly, producing
    an unreadable `['...', '...']` wall on the cockpit (card #507). Rather
    than push that str-vs-list branching into the template, we normalize
    here: this function ALSO adds a `notes_list` key — always a list of
    strings, one entry per note (a single-item list when `notes` was a
    plain string) — so the template can render N readable entries uniformly
    regardless of the authored shape. The original `notes` key is left
    untouched for any other consumer.
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
                    data["notes_list"] = _normalize_whiteboard_notes(data.get("notes"))
                    return data
            except yaml.YAMLError as exc:
                _log.warning("yaml parse error for whiteboard %s: %s", p, exc)
                return None

    return None


def _normalize_whiteboard_notes(notes: Any) -> List[str]:
    """Normalize whiteboard.yaml's `notes` field (str OR list) into a flat
    list[str] of non-empty entries, in order. Non-str list items are
    stringified defensively (a dept could author a number/date scalar).
    """
    if notes is None:
        return []
    if isinstance(notes, str):
        return [notes] if notes.strip() else []
    if isinstance(notes, list):
        out: List[str] = []
        for item in notes:
            if item is None:
                continue
            text = item if isinstance(item, str) else str(item)
            if text.strip():
                out.append(text)
        return out
    # Unexpected scalar shape (int/dict/etc.) — stringify rather than drop,
    # so authoring mistakes are still visible instead of silently vanishing.
    return [str(notes)]


def list_recent_decisions(slugs: List[str], limit: int = 10) -> List[Dict[str, Any]]:
    """Return the most recent gate decisions across all listed dept slugs.

    Reads each dept's inbox/decisions/*.yaml (unprocessed) AND
    inbox/decisions/.processed/*.yaml (already drained by the agent), parses
    gate_id / action / decided_at / comment, sorts by decided_at descending,
    returns up to `limit` entries. Malformed / missing fields are skipped
    safely (no crash).

    Each returned dict has at minimum:
        slug        str   — dept this decision belongs to
        gate_id     str
        action      str   — approve | reject | modify | defer
        decided_at  str   — ISO timestamp (may be absent → empty string)
        comment     str   — operator comment (may be empty)
        processed   bool  — True if the file came from .processed/ subdir
    """
    results: List[Dict[str, Any]] = []

    for slug in slugs:
        root = repo_path(slug)
        if root is None:
            continue
        decisions_dir = root / "inbox" / "decisions"
        if not decisions_dir.is_dir():
            continue

        # Scan both the live inbox and the .processed/ sub-directory.
        for processed, glob_dir in (
            (False, decisions_dir),
            (True, decisions_dir / ".processed"),
        ):
            if not glob_dir.is_dir():
                continue
            for dp in glob_dir.glob("*.yaml"):
                try:
                    raw = dp.read_text(encoding="utf-8")
                    ddoc = yaml.safe_load(raw)
                    if not isinstance(ddoc, dict):
                        continue
                    gate_id = ddoc.get("gate_id") or dp.stem
                    action = ddoc.get("action", "")
                    decided_at = ddoc.get("decided_at", "")
                    comment = ddoc.get("comment", "")
                    results.append({
                        "slug": slug,
                        "gate_id": str(gate_id),
                        "action": str(action),
                        "decided_at": str(decided_at) if decided_at else "",
                        "comment": str(comment) if comment else "",
                        "processed": processed,
                    })
                except Exception as exc:  # noqa: BLE001 — skip malformed files, but log it
                    _log.warning("list_recent_decisions: skipping malformed %s: %s", dp, exc)

    # Sort by decided_at descending (ISO strings sort lexicographically correctly).
    results.sort(key=lambda d: d["decided_at"], reverse=True)
    return results[:limit]


def delete_gate_decision(slug: str, gate_id: str) -> bool:
    """Delete the un-processed decision file for a gate, if it exists.

    Only removes inbox/decisions/<gate_id>.yaml (the live, un-processed file).
    Does NOT touch inbox/decisions/.processed/ — once the agent has moved the
    file there, we must NOT undo it here (the agent already acted).

    host=local depts: out of scope — the decision lives on GitHub, not on
    the cockpit disk.  Guard and return False with a log.

    SECURITY: gate_id is validated to contain no path separators or parent-dir
    components, and the resolved path is confirmed inside inbox/decisions/
    before any delete is attempted.

    Returns True on success (file existed and was deleted), False otherwise.
    """
    # Reject gate_ids that could escape the decisions directory via traversal.
    if not gate_id or "/" in gate_id or "\\" in gate_id or ".." in gate_id:
        _log.warning("delete_gate_decision: rejected unsafe gate_id %r for dept %s", gate_id, slug)
        return False

    from console.services import dept_registry as _dr
    dept = _dr.get_department(slug)
    host = getattr(dept, "host", "vps") if dept is not None else "vps"

    if host == "local":
        # host=local: decision was committed to GitHub by write_gate_decision;
        # we have no on-disk copy to delete from here.
        _log.info("delete_gate_decision: host=local dept %s — no-op (decision on GitHub)", slug)
        return False

    root = repo_path(slug)
    if root is None:
        return False

    decisions_dir = (root / "inbox" / "decisions").resolve()
    decision_file = (root / "inbox" / "decisions" / f"{gate_id}.yaml").resolve()
    # Containment check: the resolved path must stay inside inbox/decisions/.
    if not _is_within(decision_file, decisions_dir):
        _log.warning(
            "delete_gate_decision: path traversal attempt — gate_id %r resolved outside decisions dir",
            gate_id,
        )
        return False

    if not decision_file.exists():
        return False
    try:
        decision_file.unlink()
        return True
    except OSError as exc:
        _log.warning("delete_gate_decision: failed to delete %s: %s", decision_file, exc)
        return False


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


# ── Queue-to-layer mapping for list_layer_queues() ────────────────────────────
# Dispatch convention (card #376):
#   L1 produces into queues/research/   (research items consumed by L2)
#   L2 produces decisions/items toward  queues/management/ (management notes)
#   L3 reads     inbox/decisions/ + queues/inbox/decisions/  (approved decisions)
#   Cross-cutting: queues/gates/ = human-approval pending (all layers, annotated)
# "feeds layer N" means the items are *waiting* to be consumed at that layer.
_QUEUE_LAYER_MAP: List[tuple] = [
    # (relative_dir_from_repo_root, layer_it_feeds, is_gate_queue)
    ("queues/research",          1, False),
    ("queues/management",        2, False),
    ("inbox/decisions",          3, False),
    ("queues/inbox/decisions",   3, False),
    ("queues/gates",             0, True),   # layer 0 = cross-cutting gates
]

# Queue items of these kinds are RECORDS of work already done (a trade that
# executed, a wrap-up already drafted/posted), not pending to-dos. Agents are
# meant to move them to `.processed/` once acted, but when they forget, the
# left column shows day-old records as "À traiter" (#391). As a console-side
# guard we treat such items as drained once they're older than the window
# below. (The durable fix is agent loop-hygiene: move acted items to
# .processed/ — tracked separately.)
_QUEUE_TERMINAL_KINDS: set = {
    "executed_trade", "market_wrapup_draft", "trade_receipt",
    "posted", "published", "completed",
}
_QUEUE_TERMINAL_STALE_DAYS: int = 1


def _is_stale_terminal_item(kind: str, created_at: str, now=None) -> bool:
    """True if an item is an already-acted RECORD older than the stale window.

    Only terminal kinds (a trade that executed, a wrap-up already drafted) are
    eligible — a normal pending item is never filtered by age. Items with an
    unparseable/absent created_at are kept (fail-open: never hide real work).
    """
    if kind not in _QUEUE_TERMINAL_KINDS:
        return False
    if not created_at:
        return False
    from datetime import datetime, timezone, timedelta
    raw = created_at.strip()
    dt = None
    # Try full ISO-8601 (with/without TZ), then date-only.
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return False  # unparseable → keep (fail-open)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return (ref - dt) > timedelta(days=_QUEUE_TERMINAL_STALE_DAYS)

_QUEUE_TITLE_FIELDS: List[str] = [
    "question_text", "post_body", "title", "subject", "summary",
    "body", "description", "content", "text",
]

# A short "subject" field names WHICH thing an item is about (a ticker, a theme,
# a name). When present, it makes two items of the same kind distinguishable
# (e.g. two `ideas_scout` items → "ideas_scout: LSEG" vs "ideas_scout: TDG")
# instead of both collapsing to the bare kind/source label (#391).
_QUEUE_SUBJECT_FIELDS: List[str] = [
    "ticker_or_theme", "ticker", "theme", "symbol", "name", "topic",
]

# Some queue schemas carry no `kind` field but name their producing mission in a
# `source` field (e.g. ideas_scout items use `source: ideas_scout`). Treat that
# as the label when `kind` is absent, so the prefix is meaningful (#391).
_QUEUE_KIND_FALLBACK_FIELDS: List[str] = ["source", "mission", "producer"]


def _derive_queue_item_title(doc: Dict[str, Any], kind: str, max_len: int = 60) -> str:
    """Derive a human-readable, DISTINCT title from a queue item YAML dict.

    Strategy (in order):
      0. A known payload shape (morning_brief, dept_kpi_analysis, directive,
         …) gets a hand-written few-words summary from
         `humanize.humanize_queue_item` — e.g. morning_brief renders
         "Brief du matin — santé dept 86.7/100, content en warning" instead
         of repeating the kind (#460, Joris spec 2026-07-02).
      1. Determine a label: `kind`, else a `source`/`mission` field, else "item".
      2. Use `title`/any known text-content field as the excerpt (truncated).
      3. Else use a `subject` field (ticker/theme/name) — this keeps two items of
         the same kind distinguishable (#391: two ideas_scout → LSEG vs TDG).
      4. Else fall back to the first non-meta string value.
      5. Generic fallback: "<label> · <created_at date> · <first payload
         scalar>" — never just the bare label repeated (#460 spec: "kind +
         created_at + first payload scalar").

    The label always prefixes the excerpt so the operator sees "<kind>: <what>".
    """
    # 1) Label — kind wins; else a source/mission field; else generic.
    label = kind.strip() if kind and kind.strip() else ""
    if not label:
        for f in _QUEUE_KIND_FALLBACK_FIELDS:
            v = doc.get(f)
            if v and isinstance(v, str) and v.strip():
                label = v.strip()
                break
    if not label:
        label = "item"

    # 0) Known payload shape — a hand-written few-words summary, not a
    #    generic field scan. Checked first: even if the doc also happens to
    #    carry a generic text field, the curated summary is more readable.
    known = humanize_queue_item(doc, label)
    if known:
        return known

    def _fmt(excerpt: str) -> str:
        excerpt = excerpt.strip().replace("\n", " ")
        full = f"{label}: {excerpt}"
        return full[:max_len] + ("…" if len(full) > max_len else "")

    # 2) Rich text-content field.
    for field in _QUEUE_TITLE_FIELDS:
        val = doc.get(field)
        if val and isinstance(val, str) and val.strip():
            return _fmt(val)

    # 3) Subject field (ticker/theme/name) — makes same-kind items distinct.
    for field in _QUEUE_SUBJECT_FIELDS:
        val = doc.get(field)
        if val and isinstance(val, str) and val.strip():
            return _fmt(val)

    # 4) Fallback: first non-meta string value (but never the label fields
    #    themselves — otherwise the excerpt just repeats the label).
    skip = {"id", "kind", "created_at", "updated_at", "slug"} | set(_QUEUE_KIND_FALLBACK_FIELDS)
    for k, v in doc.items():
        if k in skip:
            continue
        if isinstance(v, str) and v.strip():
            return _fmt(v)

    # 5) Generic fallback (#460 spec): label + created_at date + first
    #    payload scalar (any type — numbers/bools count here, unlike step 4
    #    which only looks at strings) — never the bare label alone, which is
    #    indistinguishable from "<kind>: <kind>".
    created_at = str(doc.get("created_at") or "").strip()
    date_part = created_at[:10] if created_at else ""
    scalar_part = ""
    for k, v in doc.items():
        if k in skip:
            continue
        if isinstance(v, (str, int, float, bool)) and str(v).strip():
            scalar_part = f"{k}={v}"
            break
    bits = [b for b in (label, date_part, scalar_part) if b]
    full = " · ".join(bits) if len(bits) > 1 else label
    # Same truncation contract as steps 2-4 (_fmt, above): a large payload
    # scalar must never blow the title past max_len (#503 — step 5 was the
    # one path added by #460/#213 that forgot to route through _fmt).
    return full[:max_len] + ("…" if len(full) > max_len else "")


# Sentinel `kind` for the one synthetic "N notes traitées" summary item
# `_mgmt_queue_items()` appends to the L2 list (#459). The template checks
# for this kind to render the collapsed muted line instead of a normal row.
MGMT_CONSUMED_SUMMARY_KIND = "_mgmt_consumed_summary"


def _mgmt_queue_items(root: Path) -> List[Dict[str, Any]]:
    """Build the L2 (`queues/management/`) item list using consumption-aware
    filtering (#459) instead of the generic "every non-.processed *.yaml
    file" rule the other queue dirs use.

    Delegates to `mgmt_note_state.scan_mgmt_inbox()` — the single source of
    truth for per-note PENDING derivation (mirrors
    scripts/lib/dispatch_helpers.py's `_scan_mgmt_notes` semantics exactly;
    see that module's docstring). Converts its dataclasses into the same
    plain-dict item shape the rest of `list_layer_queues()` returns, plus one
    extra synthetic item (kind=`MGMT_CONSUMED_SUMMARY_KIND`) carrying the
    consumed count, so callers/templates keep working with a single flat
    list per layer.

    Also passes `_is_stale_terminal_item` as `scan_mgmt_inbox`'s
    `extra_stale_check` — the generic #391 terminal-kind staleness guard
    (an `executed_trade`/wrap-up record an agent forgot to move to
    `.processed/`) applies to `queues/management/` exactly like it does to
    every other queue dir; #459's consumption-awareness is additive, not a
    replacement for it.
    """
    from console.services.mgmt_note_state import scan_mgmt_inbox

    state = scan_mgmt_inbox(root, extra_stale_check=_is_stale_terminal_item)
    items: List[Dict[str, Any]] = []
    for row in state.pending_rows:
        # A single-item row keeps that note's own id/kind (so id-based
        # lookups elsewhere still resolve to the real note); a grouped row
        # (>1 pending notes sharing a mission_id) gets a synthetic group id
        # — there is no single underlying note it could stand in for.
        solo = row.items[0] if row.count == 1 else None
        items.append({
            "id": solo.id if solo else f"{row.mission_id}-group",
            "kind": solo.kind if solo else "management_note",
            "title": row.label,
            "created_at": row.latest_created_at,
            "pending_human": False,
            "group_count": row.count,
        })
    if state.consumed_count:
        items.append({
            "id": "_mgmt_consumed_summary",
            "kind": MGMT_CONSUMED_SUMMARY_KIND,
            "title": f"{state.consumed_count} note"
                     f"{'s' if state.consumed_count > 1 else ''} traitée"
                     f"{'s' if state.consumed_count > 1 else ''}",
            "created_at": "",
            "pending_human": False,
            "group_count": state.consumed_count,
        })
    return items


def list_layer_queues(slug: str) -> Dict[int, List[Dict[str, Any]]]:
    """Return the currently-waiting queue items per layer for a dept.

    For each configured queue directory, reads all YAML files that are NOT
    inside a `.processed/` subdirectory (those have already been drained by
    the dept agent). Items in queues/gates/ are cross-referenced against the
    live pending-gates list so `pending_human=True` is set on items that still
    need operator approval.

    Queue → layer mapping (card #376):
      - queues/research/        → L1  (items L1 produced, consumed by L2)
      - queues/management/      → L2  (management notes produced by L2)
      - inbox/decisions/        → L3  (approved decisions ready for L3)
      - queues/inbox/decisions/ → L3  (same, alternate path)
      - queues/gates/           → all layers (cross-cutting; pending_human=True)

    `queues/management/` is special-cased (#459): unlike the other queue
    dirs, the mgmt-note protocol never moves/mutates note files once acted
    on — consumption state lives in `.consumed.json` + `.last-mgmt-scan`
    (see scripts/lib/dispatch_helpers.py). Listing every non-`.processed`
    *.yaml file there (the generic rule below) shows every note EVER
    produced, including ones consumed weeks ago. So for L2 we instead call
    `mgmt_note_state.scan_mgmt_inbox()`, which mirrors the dispatcher's
    consumed-first / fail-open semantics per-note and groups same-mission_id
    notes into one row. Its consumed count is exposed as a synthetic item
    (`kind: "_mgmt_consumed_summary"`) so `out[2]` stays a single flat list
    the template can render without a second parameter.

    Each item dict has:
        id            str   — YAML `id` field or filename stem
        kind          str   — YAML `kind` field or empty string
        title         str   — human-readable excerpt (~60 chars, never blank)
        created_at    str   — YAML `created_at` field or empty string
        pending_human bool  — True for items that need human approval (gates)

    Returns:
        {layer_num: [item_dict, ...], ...}  for layers 1..4.
        Gate items (layer 0 internally) are duplicated into ALL subscribed layers
        so the operator sees them regardless of which layer they drill into.
        Returns {} gracefully if the dept repo is not on disk.
    """
    root = repo_path(slug)
    if root is None:
        return {1: [], 2: [], 3: [], 4: []}

    # Parse queues/gates/ ONCE and reuse for both the pending-gate-ids
    # cross-reference below AND the queues/gates branch of the main loop —
    # this dir used to be globbed+parsed twice per request (list_pending_gates
    # internally, then again here).
    gates_dir = root / "queues" / "gates"
    gate_files = _parse_gate_files(gates_dir) if gates_dir.is_dir() else []

    # Pre-compute the set of pending-gate ids (for cross-referencing). Uses
    # _filter_pending_gates (the same decision-filter logic list_pending_gates
    # applies — same items that show in "Décisions qu'on attend de toi"),
    # called directly on the gate_files we already parsed above, rather than
    # through the public list_pending_gates(slug) (kept single-arg so it stays
    # freely monkeypatchable/callable by other routes/tests).
    pending_gate_ids: set = {
        g.get("id", "") for g in _filter_pending_gates(slug, root, gate_files) if isinstance(g, dict)
    }

    out: Dict[int, List[Dict[str, Any]]] = {1: [], 2: [], 3: [], 4: []}

    for rel_dir, layer, is_gate in _QUEUE_LAYER_MAP:
        queue_dir = root / Path(rel_dir)
        if not queue_dir.is_dir():
            continue

        if rel_dir == "queues/management":
            # #459 — consumption-aware path, see docstring above. Handled
            # before the generic parse below so mgmt notes never hit the
            # "every non-.processed *.yaml" rule.
            out[layer].extend(_mgmt_queue_items(root))
            continue

        if is_gate:
            # Already parsed above (#450) — reuse instead of re-globbing/re-parsing.
            file_entries = []
            for p, doc, err in gate_files:
                if err is not None:
                    _log.warning("list_layer_queues: skipping malformed %s: %s", p, err)
                    continue
                file_entries.append((p, doc))
        else:
            file_entries = []
            for p in sorted(queue_dir.glob("*.yaml")):
                # Skip items that live inside a .processed/ subdirectory —
                # those have already been handled by the dept agent.
                if ".processed" in p.parts:
                    continue
                try:
                    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
                except yaml.YAMLError as exc:
                    _log.warning("list_layer_queues: skipping malformed %s: %s", p, exc)
                    continue
                file_entries.append((p, doc))

        items: List[Dict[str, Any]] = []
        for p, doc in file_entries:
            if not isinstance(doc, dict):
                continue

            item_id = str(doc.get("id") or p.stem)
            kind = str(doc.get("kind") or "")
            created_at = str(doc.get("created_at") or "")

            # Skip already-acted records (executed trades, posted wrap-ups) that
            # an agent forgot to move to .processed/ — they are not pending work
            # and otherwise clutter "À traiter" with day-old artifacts (#391).
            if _is_stale_terminal_item(kind, created_at):
                continue

            title = _derive_queue_item_title(doc, kind)

            # For gate queue items: pending_human=True when the item is
            # still in list_pending_gates() (i.e. not yet decided).
            if is_gate:
                pending_human = item_id in pending_gate_ids
            else:
                pending_human = False

            items.append({
                "id": item_id,
                "kind": kind,
                "title": title,
                "created_at": created_at,
                "pending_human": pending_human,
            })

        if is_gate:
            # Gates are cross-cutting: add them to ALL layers so they
            # appear in the inbox regardless of which layer is shown.
            for ln in (1, 2, 3, 4):
                out[ln].extend(items)
        else:
            if layer in out:
                out[layer].extend(items)

    return out
