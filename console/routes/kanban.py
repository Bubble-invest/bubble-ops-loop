"""Kanban board — reads from GitHub Issues (Bubble-invest/bubble-ops-board) and renders all cards.

Visual planning board with three grouping views:
  - by_status   (4 status columns mapped from status:<x> labels)
  - by_project  (proj:<x> label wins; falls back to keyword matching)
  - by_department (grouped by dept:<x> label → card.owner)

Data source change (2026-06-20, issue #164):
  Previously fetched http://{{VPS_IP}}:3847/api/inbox (Mac dashboard).
  Now fetches open GitHub Issues from Bubble-invest/bubble-ops-board via `gh`.
  Token is minted at runtime via bubble-board-token.sh (no interactive gh auth
  needed on the VPS, where gh is not interactively authed as claude).

All grouping/view logic and kanban.html template are unchanged.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import date as _dt_date
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
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
# Single-flight lock: kanban_board is threadpooled (issue #447), so N concurrent
# requests on a stale cache would each pass the TTL check and fire duplicate
# 5-page GitHub fetches (issue #458). This lock serialises the fetch; the first
# thread in refreshes the cache, the rest re-check under the lock and reuse it.
_fetch_lock = threading.Lock()


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

    ttl = settings.GH_CACHE_TTL_SECONDS
    # Fast path: a fresh, non-empty cache is served without taking the lock
    # (an unsynchronised read of a still-valid cache is safe — GIL-atomic).
    if time.monotonic() - _cache_ts < ttl and _cache_data:
        return _cache_data, None

    # Slow path: cache is stale/empty. Serialise so N concurrent threads produce
    # exactly ONE GitHub fetch. Double-checked: whoever loses the race to the
    # lock re-tests the cache under it and reuses the fetch the winner just did.
    with _fetch_lock:
        if time.monotonic() - _cache_ts < ttl and _cache_data:
            return _cache_data, None

        return _fetch_issues_locked()


def _fetch_issues_locked() -> tuple[list, str | None]:
    """Perform the actual GitHub fetch + cache write. Callers MUST hold
    `_fetch_lock` (this is the single-flight body of `_fetch_issues`)."""
    global _cache_data, _cache_ts

    now = time.monotonic()

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
                    "createdAt": it.get("created_at") or "",
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
    "client-dev":           "Client Dev",
    "bubble-shield":        "Bubble Shield",
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


def _parse_body_sections(body_text: str) -> dict[str, str]:
    """Extract named sections from a kanban issue body (Job, Inputs, etc.).

    Returns a dict like {'Job': '...', 'Inputs': '...', ...}.
    Sections are defined by '## SectionName' headers.
    """
    sections: dict[str, str] = {}
    if not body_text:
        return sections
    # Split on ## headers, keeping the header with its content
    parts = re.split(r"\n(?=## )", body_text)
    for part in parts:
        m = re.match(r"##\s+(\S.*?)(?:\s*\n|$)", part)
        if m:
            name = m.group(1).strip()
            content = part[m.end():].strip()
            # Drop visual-only sections (already rendered separately)
            if name.lower() not in ("diagram", "visual attachments"):
                sections[name] = content
    return sections


_LINK_KINDS = ("parent", "relates", "blocks")


def parse_card_links(sections: dict) -> dict:
    """Parse the '## Links' section body into typed link lists of issue numbers.

    The Links section (written by emit_kanban_item.sh) looks like:
        - **Parent:** #258
        - **Relates:** #318, #324
        - **Blocks:** #340
    Returns {"parent": [258], "relates": [318, 324], "blocks": [340]} (ints, deduped,
    empty kinds omitted). Tolerant of casing / missing kinds / stray text.
    """
    out: dict[str, list[int]] = {}
    raw = sections.get("Links") or ""
    if not raw:
        return out
    for line in raw.splitlines():
        m = re.match(r"\s*[-*]\s*\*\*\s*(parent|relates|blocks)\s*:?\s*\*\*\s*:?(.*)$",
                     line.strip(), re.I)
        if not m:
            continue
        kind = m.group(1).lower()
        nums = [int(n) for n in re.findall(r"#?(\d+)", m.group(2))]
        if nums:
            seen: list[int] = []
            for n in nums:
                if n not in seen:
                    seen.append(n)
            out.setdefault(kind, []).extend(seen)
    return out


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
    # Card-face preview: show clean prose, NOT the raw "## Job / ## Inputs (none) /
    # ## Allowed …" scaffolding (which bled into the card face and read as noise).
    # Prefer the Job section's content; fall back to the first non-header,
    # non-"(none)"/"(to be scoped…)" line; finally the raw body.
    _sections = _parse_body_sections(body_text)
    _links = parse_card_links(_sections)
    _preview_src = (_sections.get("Job") or "").strip()
    if not _preview_src:
        for _line in body_text.splitlines():
            _l = _line.strip()
            if _l and not _l.startswith("#") and not _l.startswith("---") \
               and _l.lower() not in ("(none)",) and not _l.lower().startswith("(to be scoped"):
                _preview_src = _l
                break
    if not _preview_src:
        _preview_src = body_text
    summary   = _preview_src[:140] + ("…" if len(_preview_src) > 140 else "")

    # ── Visual fields (Mermaid diagram + repo image paths) ─────────────────────
    mermaid_src, visual_attachments = _parse_visual_fields(body_text)

    raw_ts    = issue.get("updatedAt") or ""
    # updatedAt is ISO-8601 with Z: "2026-06-19T12:34:56Z" → show first 16 chars
    ts_display = raw_ts[:16].replace("T", " ") if raw_ts else ""

    # P3: due date (due:<YYYY-MM-DD> label), created date, host (host:<x> label).
    due       = _extract_label_value(labels, "due") or ""
    budget    = _extract_label_value(labels, "budget") or ""   # "$N" real-$ budget (board #358)
    budget_pct = ""                                            # % of the weekly envelope
    if budget:
        try:
            _bn = float(str(budget).lstrip("$"))
            _wk = float(getattr(settings, "WEEKLY_BUDGET_USD", 4500) or 4500)
            if _wk > 0:
                _p = 100.0 * _bn / _wk
                budget_pct = (f"{_p:.0f}%" if _p >= 1 else f"{_p:.1f}%")
        except (ValueError, TypeError):
            budget_pct = ""
    # needs:human = a decision Rick (or a dept) owes Joris — surface it (board #358).
    _label_names = {(l.get("name") or "").lower() for l in labels}
    needs_human = "needs:human" in _label_names
    _overdue  = False
    if due:
        try:
            _overdue = _dt_date.fromisoformat(due) < _dt_date.today()
        except ValueError:
            _overdue = False
    created_raw = issue.get("createdAt") or ""
    created_display = created_raw[:10] if created_raw else ""
    host_val  = _extract_label_value(labels, "host") or ""

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
        "due":          due,
        "budget":       budget,
        "budget_pct":   budget_pct,
        "overdue":      _overdue,
        "needs_human":  needs_human,
        "links":        _links,
        "created":      created_display,
        "created_raw":  created_raw,
        "host":         host_val,
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


# ── Single-issue detail fetch ─────────────────────────────────────────────────

def _fetch_single_issue(number: int) -> tuple[dict | None, str | None]:
    """Fetch ONE board issue by number (no cache) + its comments.

    Returns (issue_dict, error_string). issue_dict is the raw GitHub REST API
    issue object, plus a 'comments' key with the issue's comment list."""
    token = _read_board_token()
    if not token:
        return None, "no board API token available"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bubble-ops-console",
    }
    try:
        url = f"https://api.github.com/repos/{_BOARD_REPO}/issues/{number}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            issue = json.load(resp)
        # Fetch comments
        comments_url = f"https://api.github.com/repos/{_BOARD_REPO}/issues/{number}/comments"
        req2 = urllib.request.Request(comments_url, headers=headers)
        comments: list = []
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            comments = json.load(resp2)
        issue["comments_list"] = comments
        # Normalize the timestamp field name so issue_to_card (which expects the
        # _fetch_issues-normalized "updatedAt") shows the detail-view timestamp.
        issue["updatedAt"] = issue.get("updated_at") or ""
        return issue, None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "issue not found"
        return None, f"GitHub API error: {e.code}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return None, f"network error: {e}"


# ── Board WRITE layer (card decisions — board #427) ──────────────────────────
#
# Joris can already approve/reject DEPT gate cards from the cockpit, but Rick's
# own `needs:human` board cards (dept:rnd) had no action buttons — they were a
# read-only "Décider →" link to GitHub. This write layer lets the cockpit record
# the decision ON THE BOARD ISSUE itself (label + comment + close), reusing the
# SAME short-lived board token + REST approach as the read path above.
#
# RECORD-ONLY v1: the decision is written to the issue; Rick picks it up on his
# next loop tick. There is intentionally NO auto-trigger / dispatch here.
#
# Each REST verb is a tiny module-level function so the route stays readable AND
# tests can monkeypatch the HTTP layer cleanly (no real GitHub calls in CI).

# Actions the cockpit may record on an R&D decision card.
CARD_DECISION_ACTIONS = {"approve", "reject", "defer"}

# Dynamic labels we apply on a decision. Created idempotently (create-if-missing)
# before use so the call "just works" even on a fresh board. (name, hex_color).
_DECISION_LABELS = {
    "decision:approved": "0e8a16",  # green
    "decision:rejected": "d73a4a",  # red
}


def _board_write_headers(token: str) -> dict:
    """Auth + content headers for a board WRITE call (same shape as the read path)."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bubble-ops-console",
        "Content-Type": "application/json",
    }


def _board_api(method: str, path: str, token: str, payload: dict | None = None):
    """Issue one authenticated REST call against the board repo.

    `path` is appended to https://api.github.com/repos/<BOARD_REPO>. Returns the
    parsed JSON body (or None for empty/204). Raises urllib.error.HTTPError on a
    non-2xx so the caller can decide whether it is fatal (the route catches it
    and renders an error partial instead of 500-ing)."""
    url = f"https://api.github.com/repos/{_BOARD_REPO}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers=_board_write_headers(token), method=method
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None


def _ensure_board_label(name: str, color: str, token: str) -> None:
    """Create a board label if it is missing (idempotent). A 422 from the create
    means it already exists — that is success, not failure. Any other error is
    swallowed (a missing label only matters when we then try to APPLY it, and the
    apply call surfaces its own error to the route)."""
    try:
        _board_api("POST", "/labels", token,
                   {"name": name, "color": color})
    except urllib.error.HTTPError as exc:
        if exc.code == 422:  # already exists
            return
        # Non-422: don't make label-creation fatal here; the subsequent
        # add-label call is the real gate and will report if something's wrong.
        return
    except Exception:
        return


def _apply_card_decision(number: int, action: str, comment: str, token: str) -> None:
    """Record `action` on board issue #number: label + comment (+ close, or
    un-queue for defer). Reuses the board token. Raises urllib.error.HTTPError /
    URLError on a failed REST call so the route can render an error partial.

    - approve : +decision:approved, comment, close
    - reject  : +decision:rejected, comment, close
    - defer   : -needs:human, comment (stays OPEN — back to Rick's queue)
    """
    extra = (comment or "").strip()
    suffix = ("\n\n" + extra) if extra else ""

    if action == "approve":
        _ensure_board_label("decision:approved", _DECISION_LABELS["decision:approved"], token)
        _board_api("POST", f"/issues/{number}/labels", token,
                   {"labels": ["decision:approved"]})
        _board_api("POST", f"/issues/{number}/comments", token,
                   {"body": "✅ Approved by Joris via cockpit" + suffix})
        _board_api("PATCH", f"/issues/{number}", token, {"state": "closed"})
    elif action == "reject":
        _ensure_board_label("decision:rejected", _DECISION_LABELS["decision:rejected"], token)
        _board_api("POST", f"/issues/{number}/labels", token,
                   {"labels": ["decision:rejected"]})
        _board_api("POST", f"/issues/{number}/comments", token,
                   {"body": "❌ Rejected by Joris via cockpit" + suffix})
        _board_api("PATCH", f"/issues/{number}", token, {"state": "closed"})
    elif action == "defer":
        # Remove the needs:human label so the card drops off Joris's queue and
        # returns to Rick. DELETE a label that isn't present 404s — tolerate it
        # (the desired end-state, label-absent, is already achieved).
        try:
            _board_api("DELETE", f"/issues/{number}/labels/needs:human", token)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        _board_api("POST", f"/issues/{number}/comments", token,
                   {"body": "⏳ Deferred by Joris — back to Rick's queue" + suffix})
    else:  # pragma: no cover — route validates before calling
        raise ValueError(f"unknown action: {action}")


# ── Routes ─────────────────────────────────────────────────────────────────────

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


@router.get("/kanban/card/{number}", response_class=HTMLResponse)
def kanban_card_detail(number: int, request: Request):
    """Single kanban card detail view — in-cockpit, not GitHub.

    Mirrors gate.py's gate_card route: fetches one issue + its comments,
    maps to a card dict (reusing issue_to_card + _parse_visual_fields),
    renders a detail template so the card body, Mermaid diagram,
    attachments, and comment thread are all visible in one cockpit page.
    """
    issue, error = _fetch_single_issue(number)
    if error or issue is None:
        raise HTTPException(404, "Issue not found" if not error else error)

    card = issue_to_card(issue)
    comments = issue.get("comments_list") or []

    # Extract structured sections from body for readable layout
    body_text = issue.get("body") or ""
    sections = _parse_body_sections(body_text)

    return request.app.state.templates.TemplateResponse(
        "kanban_card.html",
        {
            "request": request,
            "card": card,
            "sections": sections,
            "comments": comments,
            "issue_url": issue.get("html_url") or issue.get("url") or "",
        },
    )


def _decision_error_partial(request: Request, number: int, message: str,
                            status_code: int):
    """Render the decision partial in error mode (no 500 — the card stays put
    with a readable message). Used for no-token (503) and GitHub API failures."""
    resp = request.app.state.templates.TemplateResponse(
        "partials/kanban_decision_ok.html",
        {
            "request": request,
            "number": number,
            "action": "",
            "error": message,
        },
        status_code=status_code,
    )
    return resp


@router.post("/kanban/card/{number}/decide", response_class=HTMLResponse)
def kanban_card_decide(
    number: int, request: Request,
    action: str = Form(...), comment: str = Form(""),
):
    """Record Joris's decision on an R&D `needs:human` board card (board #427).

    Mirrors the dept-gate decide pattern, but the target is the GitHub BOARD
    issue (not a YAML file): approve/reject add a decision:<x> label + a comment
    then close the issue; defer removes needs:human and comments (stays open,
    back to Rick's queue). RECORD-ONLY — Rick acts on his next loop tick.

    Fails SAFE: no board token (dev/CI) → 503 partial, never a crash; a GitHub
    API error → an error partial, never a 500. The card swaps in place either way.
    """
    if action not in CARD_DECISION_ACTIONS:
        raise HTTPException(400, f"Invalid action: {action}")

    token = _read_board_token()
    if not token:
        # Dev/CI or a token-refresh gap — degrade gracefully, do not crash.
        return _decision_error_partial(
            request, number,
            "Board write token unavailable — décision non enregistrée.",
            503,
        )

    try:
        _apply_card_decision(number, action, comment, token)
    except urllib.error.HTTPError as exc:
        return _decision_error_partial(
            request, number,
            f"Erreur GitHub ({exc.code}) — décision non enregistrée.",
            502,
        )
    except (urllib.error.URLError, OSError) as exc:
        return _decision_error_partial(
            request, number,
            f"Erreur réseau — décision non enregistrée ({exc}).",
            502,
        )

    # Invalidate the in-process board cache so the (now closed / un-queued) card
    # disappears on the next /kanban or / load instead of lingering until TTL.
    global _cache_ts
    _cache_ts = 0.0

    return request.app.state.templates.TemplateResponse(
        "partials/kanban_decision_ok.html",
        {
            "request": request,
            "number": number,
            "action": action,
            "error": "",
        },
    )


# Longest comment body GitHub accepts (65536); reject before the API call so the
# operator gets a clear French message rather than an opaque 422 (board #482).
_MAX_COMMENT_CHARS = 65000


def _render_comments_block(
    request: Request, card: dict, comments: list, error: str, status_code: int,
):
    """Render the comments-thread + reply-form partial (the htmx swap target for
    the «Répondre» form). `error` (non-empty) shows a French message above the
    form while preserving the passed-in comments."""
    return request.app.state.templates.TemplateResponse(
        "partials/kanban_comments_block.html",
        {
            "request": request,
            "card": card,
            "comments": comments,
            "comment_error": error,
        },
        status_code=status_code,
    )


@router.post("/kanban/card/{number}/comment", response_class=HTMLResponse)
def kanban_card_comment(
    number: int, request: Request, body: str = Form(""),
):
    """Post Joris's reply as a GitHub issue comment on board card #number (#482).

    This is THE mechanism that ANSWERS a needs:human card from the cockpit: the
    operator's words are posted verbatim (no prefix — GitHub attribution shows
    the board bot, same as decide comments), then the comment thread is
    re-rendered inline (htmx) with the new comment. Rick's loop detects the reply
    and advances the card.

    Fails SAFE, mirroring kanban_card_decide:
      - empty/whitespace body  → 400 + visible French message (no board write)
      - body > 65k chars        → 400 + visible French message (no board write)
      - no board token (dev/CI) → 503 partial, never a crash
      - GitHub API error        → 502 partial, never a 500
    Every path re-renders THIS block so the form swaps in place.
    """
    # A minimal card dict is enough for the partial (it only needs the id) — but
    # keep the interface identical to the detail page by carrying the number.
    card = {"id": str(number)}

    text = (body or "").strip()
    if not text:
        return _render_comments_block(
            request, card, [],
            "Le commentaire est vide — écris une réponse avant d'envoyer.", 400,
        )
    if len(text) > _MAX_COMMENT_CHARS:
        return _render_comments_block(
            request, card, [],
            f"Commentaire trop long ({len(text)} caractères, max {_MAX_COMMENT_CHARS}).",
            400,
        )

    token = _read_board_token()
    if not token:
        return _render_comments_block(
            request, card, [],
            "Board write token unavailable — commentaire non enregistré.", 503,
        )

    try:
        _board_api("POST", f"/issues/{number}/comments", token, {"body": text})
    except urllib.error.HTTPError as exc:
        return _render_comments_block(
            request, card, [],
            f"Erreur GitHub ({exc.code}) — commentaire non enregistré.", 502,
        )
    except (urllib.error.URLError, OSError) as exc:
        return _render_comments_block(
            request, card, [],
            f"Erreur réseau — commentaire non enregistré ({exc}).", 502,
        )

    # Invalidate the in-process board cache so any board-derived view reflects the
    # freshly-touched issue on its next load (mirrors kanban_card_decide).
    global _cache_ts
    _cache_ts = 0.0

    # Re-fetch the issue's comments so the re-rendered thread includes the new
    # one. If the re-fetch fails (network blip after a successful POST), fall back
    # to showing the just-posted comment so the operator still sees their reply.
    issue, err = _fetch_single_issue(number)
    if issue is not None and not err:
        comments = issue.get("comments_list") or []
    else:
        comments = [{
            "user": {"login": "bubble-board-bot"},
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "body": text,
        }]

    return _render_comments_block(request, card, comments, "", 200)


import datetime as _dt


def build_project_graph(cards: list) -> str:
    """Build a Mermaid `graph LR` of the typed links among the given cards.
    Edges: parent (──▷ hierarchy), blocks (──■ sequencing), relates (┄┄ soft).
    Only cards that have a link (in or out) appear. Returns '' if no links."""
    ids = {int(c["id"]) for c in cards if str(c.get("id", "")).isdigit()}
    title = {int(c["id"]): (c.get("title") or "")[:38].replace('"', "'") for c in cards if str(c.get("id","")).isdigit()}
    edges = []
    nodes = set()
    for c in cards:
        if not str(c.get("id", "")).isdigit():
            continue
        src = int(c["id"])
        links = c.get("links") or {}
        for tgt in links.get("parent", []):
            edges.append((src, "parent", tgt)); nodes.add(src); nodes.add(tgt)
        for tgt in links.get("blocks", []):
            edges.append((src, "blocks", tgt)); nodes.add(src); nodes.add(tgt)
        for tgt in links.get("relates", []):
            a, b = sorted((src, tgt))
            edges.append((a, "relates", b)); nodes.add(src); nodes.add(tgt)
    if not edges:
        return ""
    # dedupe edges
    seen = set(); uniq = []
    for e in edges:
        if e not in seen:
            seen.add(e); uniq.append(e)
    out = ["graph LR"]
    for n in sorted(nodes):
        label = f"#{n}" + (": " + title[n] if n in title and title[n] else "")
        out.append(f'  N{n}["{label}"]')
    for src, kind, tgt in uniq:
        if kind == "parent":
            out.append(f"  N{tgt} -->|parent| N{src}")     # parent points down to child
        elif kind == "blocks":
            out.append(f"  N{src} -.->|blocks| N{tgt}")
        else:
            out.append(f"  N{src} ---|relates| N{tgt}")
    return "\n".join(out)


def project_graphs(by_project: dict) -> dict:
    """One Mermaid graph per project bucket (empty string if that project has no links)."""
    return {name: build_project_graph(cards) for name, cards in by_project.items()}





def group_by_timeline(cards: list) -> dict:
    """Bucket cards by due-date horizon for the timeline view. Only cards WITH a
    due: label appear (undated cards are not on a timeline). Buckets ordered
    overdue → today → this week → later. Within a bucket, soonest-due first."""
    today = _dt.date.today()
    week_end = today + _dt.timedelta(days=7)
    buckets = {"En retard": [], "Aujourd'hui": [], "Cette semaine": [], "Plus tard": []}
    for c in cards:
        due = (c.get("due") or "").strip()
        if not due:
            continue
        try:
            d = _dt.date.fromisoformat(due)
        except ValueError:
            continue
        if d < today:
            buckets["En retard"].append(c)
        elif d == today:
            buckets["Aujourd'hui"].append(c)
        elif d <= week_end:
            buckets["Cette semaine"].append(c)
        else:
            buckets["Plus tard"].append(c)
    for k in buckets:
        buckets[k] = sorted(buckets[k], key=lambda c: c.get("due") or "9999")
    # drop empty buckets, preserve order
    return {k: v for k, v in buckets.items() if v}


def sort_by_date_added(cards: list) -> list:
    """Flat list of all cards, newest-created first (the 'date added' view)."""
    return sorted(cards, key=lambda c: c.get("created_raw") or "", reverse=True)


@router.get("/kanban", response_class=HTMLResponse)
def kanban_board(request: Request):
    """Full kanban board — all open issues from the GitHub board repo.

    Passes three groupings to the template:
      - by_status       dict[status_name -> list[card]]  (4 status columns)
      - by_department   dict[dept -> list[card]]
      - by_project      dict[project_name -> list[card]]

    Sync `def` (not `async def`) so FastAPI runs this in its threadpool —
    `_fetch_issues()` does blocking urllib I/O (up to 5 sequential GitHub
    API pages, timeout=20 each) and must never run on the event loop
    (issue #447: a hung GitHub call would otherwise stall every other
    request on this single-worker console for up to ~100s).
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
    proj_graphs   = project_graphs(by_project)
    by_timeline   = group_by_timeline(all_cards)
    by_date_added = sort_by_date_added(all_cards)

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
        "proj_graphs":   proj_graphs,
        "by_timeline":   by_timeline,
        "by_date_added": by_date_added,
        # Pre-computed accent colours {group_name -> hex}
        "accents_status":  build_accent_map(by_status_labelled),
        "accents_dept":    build_accent_map(by_department),
        "accents_project": build_accent_map(by_project),
        "accents_timeline": build_accent_map(by_timeline),
        "error":        error,
        "generated_at": generated_at,
        "counts":       counts,
    })
