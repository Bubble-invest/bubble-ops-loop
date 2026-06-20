"""Kanban board — reads from GitHub Issues (Bubble-invest/bubble-ops-board) and renders all cards.

Visual planning board with three grouping views:
  - by_status   (4 status columns mapped from status:<x> labels)
  - by_project  (proj:<x> label wins; falls back to keyword matching)
  - by_department (grouped by dept:<x> label → card.owner)

Data source change (2026-06-20, issue #164):
  Previously fetched http://100.75.151.47:3847/api/inbox (Mac dashboard).
  Now fetches open GitHub Issues from Bubble-invest/bubble-ops-board via `gh`.
  Token is minted at runtime via bubble-board-token.sh (no interactive gh auth
  needed on the VPS, where gh is not interactively authed as claude).

All grouping/view logic and kanban.html template are unchanged.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from console.services.github_reader import (
    attachment_media_type,
    resolve_kanban_attachment_path,
)

from console import settings

# GitHub repo holding the board issues
_BOARD_REPO = "Bubble-invest/bubble-ops-board"

router = APIRouter()

# ── In-process cache (mirrors GH_CACHE_TTL_SECONDS pattern from settings.py) ──
# Avoids hitting the GitHub API on every /kanban page load.
_cache_data: list = []      # last fetched raw issue list
_cache_ts: float = 0.0      # epoch-seconds when cache was populated


# The board API token is minted by a root timer (bubble-board-token-refresh)
# into this tmpfs file (0640, root:claude). The cockpit runs NoNewPrivileges=yes
# so it CANNOT sudo at request time — it just reads this file. The token is
# short-lived (~1h) + issues-scoped, never persisted to disk, refreshed every
# ~45min. We call the GitHub REST API directly (no `gh` CLI) so there's no
# config-dir / env-token-precedence / PrivateTmp fragility — the token is passed
# only as an in-memory Authorization header.
_BOARD_TOKEN_FILE = "/run/bubble-board/token"


def _read_board_token() -> str | None:
    """Read the short-lived board token from the tmpfs file the refresher writes.
    Fallback to GH_TOKEN env (dev/CI). None if neither available."""
    try:
        tok = open(_BOARD_TOKEN_FILE).read().strip()
        if tok:
            return tok
    except OSError:
        pass
    return os.environ.get("GH_TOKEN") or None


def _fetch_issues() -> tuple[list, str | None]:
    """Fetch open issues from the board repo via the GitHub REST API.

    Returns (issues_normalized, error_string_or_None). Each issue is normalized
    to the shape the rest of this module expects (matching the old `gh --json`
    fields): number, title, body, labels[{name}], url, updatedAt, state.
    """
    global _cache_data, _cache_ts

    now = time.monotonic()
    ttl = settings.GH_CACHE_TTL_SECONDS
    if now - _cache_ts < ttl and _cache_data:
        return _cache_data, None

    token = _read_board_token()
    if not token:
        return [], "no board API token available (/run/bubble-board/token missing)"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bubble-ops-console",
    }
    issues: list = []
    # Paginate the REST issues API (100/page). PR objects also come back from
    # this endpoint — drop them (they carry a "pull_request" key).
    try:
        for page in range(1, 6):  # up to 500 issues; the board is far smaller
            url = (f"https://api.github.com/repos/{_BOARD_REPO}/issues"
                   f"?state=open&per_page=100&page={page}")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                batch = json.load(resp)
            if not batch:
                break
            for it in batch:
                if it.get("pull_request"):
                    continue
                issues.append({
                    "number": it.get("number"),
                    "title": it.get("title") or "",
                    "body": it.get("body") or "",
                    # REST labels are [{name,...}] — same shape gh emitted.
                    "labels": it.get("labels") or [],
                    # normalize REST field names → what issue_to_card expects.
                    "url": it.get("html_url") or "",
                    "updatedAt": it.get("updated_at") or "",
                    "state": it.get("state") or "open",
                })
            if len(batch) < 100:
                break
    except urllib.error.HTTPError as exc:
        return [], f"board API HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return [], f"board API fetch failed: {exc}"

    _cache_data = issues
    _cache_ts = now
    return issues, None


# ── Label-name prefix maps ─────────────────────────────────────────────────────

# Maps proj:<x> label value → human project bucket name.
# If an issue has a proj: label its bucket is authoritative; no keyword guessing.
_PROJ_LABEL_MAP = {
    "cockpit":              "Cockpit & Dashboard",
    "wiki-memory":          "Wiki & Mémoire",
    "fund":                 "Fund & Investissement",
    "infra":                "Infra & Plateforme",
    "prospection-content":  "Prospection & Contenu",
    "voice":                "Voix & Audio",
    "security":             "Sécurité",
    "autre":                "Autre",
}

# Maps status:<x> label value → internal column key (matches template's column names).
_STATUS_LABEL_MAP = {
    "triage":       "needs_attention",
    "in-progress":  "investigating",
    "blocked":      "waiting",
    "done":         "done",
}

# Maps risk:<x> → canonical priority string.
_RISK_LABEL_MAP = {
    "high":   "high",
    "medium": "medium",
    "low":    "low",
}


def _extract_label_value(labels: list[dict], prefix: str) -> str | None:
    """Return the value after 'prefix:' for the first matching label, or None."""
    for lbl in labels:
        name = (lbl.get("name") or "").lower()
        if name.startswith(prefix + ":"):
            return name[len(prefix) + 1:].strip()
    return None


def _parse_visual_fields(body_text: str) -> tuple[str, list[str]]:
    """Extract optional visual fields from a kanban issue body.

    Returns (mermaid_source, visual_attachments_list).
    - mermaid_source: the raw content of a fenced ```mermaid ... ``` block,
      or "" if none found.
    - visual_attachments_list: paths from a "## Visual Attachments" section
      (one path per line, stripped), or [] if none found.
    """
    mermaid_src = ""
    attachments: list[str] = []

    if not body_text:
        return mermaid_src, attachments

    # ── Mermaid block ──────────────────────────────────────────────────────
    m = re.search(r"```mermaid\s*\n(.*?)```", body_text, re.DOTALL | re.IGNORECASE)
    if m:
        mermaid_src = m.group(1).strip()

    # ── Visual Attachments list ────────────────────────────────────────────
    # Lines under "## Visual Attachments" that start with "- " or "* "
    va_match = re.search(
        r"##\s+Visual Attachments\s*\n((?:\s*[-*]\s*[^\n]+\n?)+)",
        body_text, re.IGNORECASE
    )
    if va_match:
        for line in va_match.group(1).strip().splitlines():
            stripped = re.sub(r"^\s*[-*]\s*", "", line).strip()
            if stripped:
                attachments.append(stripped)

    return mermaid_src, attachments


def issue_to_card(issue: dict) -> dict:
    """Map one GitHub Issue dict → the card dict the three views consume.

    Fields consumed by kanban.html:
      id, title, body, summary, owner, priority, column, kanban_type,
      context_url, ts_display.
    Extra field we attach:
      project — resolved project bucket (from proj: label or keyword fallback).
    """
    labels: list[dict] = issue.get("labels") or []

    # ── Label extraction ───────────────────────────────────────────────────────
    # owner from dept:<x>
    dept_val  = _extract_label_value(labels, "dept")
    owner     = dept_val if dept_val else ""

    # priority from risk:<x>
    risk_val  = _extract_label_value(labels, "risk")
    priority  = _RISK_LABEL_MAP.get(risk_val or "", "")

    # status column from status:<x>; default → needs_attention
    status_val = _extract_label_value(labels, "status")
    column     = _STATUS_LABEL_MAP.get(status_val or "", "needs_attention")

    # type from type:<x>
    type_val   = _extract_label_value(labels, "type")
    kanban_type = type_val if type_val else ""

    # project from proj:<x> (authoritative); set to "" here, resolved below
    proj_val   = _extract_label_value(labels, "proj")
    project    = _PROJ_LABEL_MAP.get(proj_val or "", "") if proj_val else ""

    # ── Scalar fields ──────────────────────────────────────────────────────────
    body_text = (issue.get("body") or "").strip()
    summary   = body_text[:140] + ("…" if len(body_text) > 140 else "")

    # ── Visual fields (Mermaid diagram + repo image paths) ─────────────────────
    mermaid_src, visual_attachments = _parse_visual_fields(body_text)

    raw_ts    = issue.get("updatedAt") or ""
    # updatedAt is ISO-8601 with Z: "2026-06-19T12:34:56Z" → show first 16 chars
    ts_display = raw_ts[:16].replace("T", " ") if raw_ts else ""

    card = {
        "id":           str(issue.get("number", "")),
        "title":        (issue.get("title") or "").strip(),
        "body":         body_text,
        "summary":      summary,
        "owner":        owner,
        "priority":     priority,
        "column":       column,
        "kanban_type":  kanban_type,
        "context_url":  issue.get("url") or "",
        "ts_display":   ts_display,
        # Resolved project bucket (used by group_by_project; "" means fall through
        # to derive_project keyword heuristic).
        "project":      project,
        # Visual planning fields (B1 — ROUND2)
        "mermaid_src":          mermaid_src,
        "visual_attachments":   visual_attachments,
    }
    return card


# ── Keyword heuristics (fallback when no proj: label) ─────────────────────────
# Super-project buckets, in priority order (first match wins). Keywords are
# matched on WORD BOUNDARIES, not raw substrings, to avoid false buckets like
# "gate" ⊂ "inves*tigate*", "nav" ⊂ "*nav*igate", "risk" ⊂ "aste*risk*",
# "ben" ⊂ "*ben*chmark". (The earlier raw-substring version mis-filed every
# "Investigate …" card into Cockpit — Rick fix 2026-06-19.)
_PROJECT_BUCKETS = [
    ("Sécurité", ["security", "sécurité", "cve", "vulnerability", "vuln", "zero-day", "exploit", "eliot", "hardening", "breach", "pii", "rgpd"]),
    ("Cockpit & Dashboard", ["cockpit", "kanban", "dashboard", "gate", "whiteboard", "chart", "console", "/dept/"]),
    ("Wiki & Mémoire", ["wiki", "memory", "mémoire", "memory-hygiene", "synthesis", "compile"]),
    ("Fund & Investissement", ["fund", "alpaca", "saxo", "bourso", "crypto", "nav", "trade", "cluster", "portfolio", "risk", "ben"]),
    ("Infra & Plateforme", ["systemd", "loop", "scaffold", "sandbox", "sops", "secret", "deploy", "vps", "model", "subagent", "doctrine", "cron", "crons", "git", "repo", "service"]),
    ("Prospection & Contenu", ["maya", "prospect", "linkedin", "content", "miranda", "newsletter", "social", "substack"]),
    ("Voix & Audio", ["voice", "audio", "whisper", "listener", "vocal", "mic"]),
]


def _kw_matches(keyword: str, text: str) -> bool:
    """True if keyword occurs in text on word boundaries. A keyword containing a
    non-word char (e.g. '/dept/') falls back to a plain substring test, since \\b
    around slashes is meaningless."""
    if re.search(r"\W", keyword):
        return keyword in text
    return re.search(r"\b" + re.escape(keyword) + r"\b", text) is not None


def derive_project(card: dict) -> str:
    """Derive a super-project bucket for a card from its text. Keyword-based
    (word-boundary matched, first bucket wins); returns 'Autre' if nothing hits."""
    t = ((card.get("title") or "") + " " + (card.get("body") or "")).lower()
    for name, kws in _PROJECT_BUCKETS:
        if any(_kw_matches(k, t) for k in kws):
            return name
    return "Autre"


# ── Priority ordering for sorting ──────────────────────────────────────────────

_PRIO_ORDER = {"high": 0, "medium": 1, "low": 2}


def _prio_key(card: dict) -> tuple:
    """Sort key: (priority rank ASC, ts_display DESC)."""
    prio = (card.get("priority") or "").lower()
    rank = _PRIO_ORDER.get(prio, 3)
    ts   = card.get("ts_display") or ""
    return (rank, _RevStr(ts))


class _RevStr(str):
    """String subclass that reverses comparison order, giving descending sort."""
    def __lt__(self, other): return str.__gt__(self, other)
    def __gt__(self, other): return str.__lt__(self, other)
    def __le__(self, other): return str.__ge__(self, other)
    def __ge__(self, other): return str.__le__(self, other)


def _sort_cards(cards: list) -> list:
    """Return a new list sorted by priority (high→medium→low→none) then ts desc."""
    return sorted(cards, key=_prio_key)


# ── Dept alias normalisation ───────────────────────────────────────────────────
# Normalize owner-field drift (case + aliases) so a dept isn't split across
# "rnd" / "rick / rnd" / "Rick", or "Morty" / "morty". Maps a raw owner → canonical.
_DEPT_ALIASES = {
    "rick / rnd": "rnd", "rick": "rnd", "rick_rnd": "rnd", "r&d": "rnd", "lab": "rnd",
    "main-strategist": "tony", "main": "tony", "ricky": "tony", "ceo": "tony",
    "miranda": "content", "socials": "content",
    "eliot": "security", "géraldine": "accountant", "geraldine": "accountant",
}


def _canon_dept(owner: str) -> str:
    o = (owner or "").strip().lower()
    if not o:
        return "unassigned"
    return _DEPT_ALIASES.get(o, o)


# ── Accent palette ─────────────────────────────────────────────────────────────

# Six muted tones chosen to feel calm on the cream/paper background
_ACCENT_PALETTE = [
    "#b8804a",  # ochre
    "#5c7a6b",  # sage
    "#7a6b9a",  # lavender
    "#6b8fa8",  # slate-blue
    "#a87a5a",  # terracotta
    "#6b8a6b",  # moss
]


def group_accent(name: str) -> str:
    """Return a deterministic muted accent colour for a group name.
    Uses sum-of-ordinals mod palette-length — simple, stable, no hashing library."""
    return _ACCENT_PALETTE[sum(ord(c) for c in name) % len(_ACCENT_PALETTE)]


def build_accent_map(groups: dict) -> dict:
    """Return {group_name: hex_color} for all keys in a grouping dict."""
    return {name: group_accent(name) for name in groups}


# ── Three grouping views ───────────────────────────────────────────────────────

def group_by_department(cards: list) -> dict:
    """Group cards by owner field (normalised for case/alias drift). Falls back
    to 'unassigned' if owner is empty. Sorted by descending card count."""
    groups: dict[str, list] = defaultdict(list)
    for card in cards:
        dept = _canon_dept(card.get("owner"))
        groups[dept].append(card)
    sorted_groups = {
        dept: _sort_cards(cards_list)
        for dept, cards_list in groups.items()
    }
    return dict(sorted(sorted_groups.items(), key=lambda kv: -len(kv[1])))


def group_by_project(cards: list) -> dict:
    """Group cards by resolved project bucket.

    Priority: card["project"] (from proj: label, set by issue_to_card) wins.
    Fallback: derive_project(card) keyword heuristic if card["project"] is empty.
    Sorted by descending card count.
    """
    groups: dict[str, list] = defaultdict(list)
    for card in cards:
        # proj: label wins; keyword heuristic is the fallback.
        project = card.get("project") or derive_project(card)
        groups[project].append(card)
    sorted_groups = {
        proj: _sort_cards(cards_list)
        for proj, cards_list in groups.items()
    }
    return dict(sorted(sorted_groups.items(), key=lambda kv: -len(kv[1])))


def _flatten_columns(columns: dict) -> list:
    """Flatten all status columns into a single list of cards."""
    all_cards = []
    for cards_list in columns.values():
        all_cards.extend(cards_list)
    return all_cards


# ── Route ──────────────────────────────────────────────────────────────────────

@router.get("/kanban/attachment")
def kanban_attachment(repo: str, path: str, request: Request):
    """Serve a kanban card's visual attachment (image or diagram) inline.

    Cards reference repo images via `visual_attachments:` in the issue body.
    The cockpit renders them via
    <img src="/kanban/attachment?repo=<org/repo>&path=<rel_path>">.

    SECURITY — mirrors /gate/<slug>/attachment EXACTLY:
      - bearer auth is enforced by the global middleware;
      - `resolve_kanban_attachment_path` strictly validates the path is inside
        <repo>/outputs/*/(diagrams|attachments|charts)/, extension in allowlist,
        no traversal, no symlink escape. Returns None on ANY doubt.
    None → opaque 404 without echoing path or reason (no oracle).
    """
    resolved = resolve_kanban_attachment_path(repo, path)
    if resolved is None:
        raise HTTPException(404, "Attachment not found")
    media_type = attachment_media_type(resolved)
    # Same CSP header as gate.py's attachment route — mandatory for SVG
    # (SVGs can carry embedded <script>; CSP blocks execution).
    headers = {
        "Cache-Control": "private, max-age=300",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(str(resolved), media_type=media_type, headers=headers)


@router.get("/kanban", response_class=HTMLResponse)
async def kanban_board(request: Request):
    """Full kanban board — all open issues from the GitHub board repo.

    Passes three groupings to the template:
      - by_status       dict[status_name -> list[card]]  (4 status columns)
      - by_department   dict[dept -> list[card]]
      - by_project      dict[project_name -> list[card]]
    """
    columns: dict = {}
    generated_at = ""
    error: str | None = None
    counts: dict = {}

    issues, fetch_error = _fetch_issues()
    if fetch_error:
        error = fetch_error
    else:
        # Map each issue → card dict, then bucket by column (status label).
        raw_columns: dict[str, list] = defaultdict(list)
        for issue in issues:
            card = issue_to_card(issue)
            raw_columns[card["column"]].append(card)
        columns = dict(raw_columns)

        # generated_at: timestamp of newest issue's updatedAt (or now if empty)
        if issues:
            # Issues come back in gh default order; find the most-recent updatedAt.
            newest = max(
                (i.get("updatedAt") or "") for i in issues
            )
            generated_at = newest[:16].replace("T", " ") if newest else ""
        else:
            generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

        counts = {col: len(cards) for col, cards in columns.items()}

    # ── Build all three groupings from the flat card list ─────────────────────
    all_cards = _flatten_columns(columns)

    # by_status: preserve canonical column order, sort cards within each column
    status_order = ["needs_attention", "investigating", "waiting", "done"]
    by_status = {
        col: _sort_cards(columns.get(col, []))
        for col in status_order
        if col in columns or columns.get(col)
    }
    # Include any unexpected status columns not in the default order
    for col, cards_list in columns.items():
        if col not in by_status:
            by_status[col] = _sort_cards(cards_list)

    by_department = group_by_department(all_cards)
    by_project    = group_by_project(all_cards)

    # Pre-compute accent colours server-side — Jinja2 has no ord() filter
    status_labels = {
        "needs_attention": "À traiter",
        "investigating":   "En cours",
        "waiting":         "En attente",
        "done":            "Terminé",
    }
    by_status_labelled = {
        status_labels.get(k, k): v for k, v in by_status.items()
    }

    return request.app.state.templates.TemplateResponse("kanban.html", {
        "request": request,
        # Legacy: keep 'columns' for any other callers
        "columns": columns,
        # Three grouping views
        "by_status":     by_status_labelled,
        "by_department": by_department,
        "by_project":    by_project,
        # Pre-computed accent colours {group_name -> hex}
        "accents_status":  build_accent_map(by_status_labelled),
        "accents_dept":    build_accent_map(by_department),
        "accents_project": build_accent_map(by_project),
        "error":        error,
        "generated_at": generated_at,
        "counts":       counts,
    })
