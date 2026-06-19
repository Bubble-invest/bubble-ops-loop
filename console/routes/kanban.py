"""Kanban board — reads from the Mac dashboard API and renders all cards.

Visual planning board with three grouping views:
  - by_status   (original: 4 status columns)
  - by_project  (derived super-project bucket via keyword matching)
  - by_department (grouped by card.owner)
"""
import json
import re
import urllib.request
import urllib.error
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

DASHBOARD = "http://{{INTERNAL_IP}}:3847"
router = APIRouter()

# Priority ordering for sorting: high=0, medium=1, low=2, anything else=3
_PRIO_ORDER = {"high": 0, "medium": 1, "low": 2}

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


def _prio_key(card: dict) -> tuple:
    """Sort key: (priority rank, ts_display desc).
    ts_display desc = negate is not possible for strings, so we return it as-is;
    Python's sort is stable and strings sort lexicographically ascending, so we
    invert by wrapping in a negation-friendly proxy via tuple order trick:
    we pass ts_display as a string and rely on the caller reversing it, OR we
    just embed priority rank and leave ts secondary (same-priority cards will be
    in original order, which is fine for this use-case).
    """
    prio = (card.get("priority") or "").lower()
    rank = _PRIO_ORDER.get(prio, 3)
    # Negate timestamp string comparison by prefixing a high unicode char if we want desc.
    # Simpler: sort ascending by rank, then descending by ts_display.
    # We can't negate a string, so we use a negative-trick: wrap ts in a reverse-sortable key.
    ts = card.get("ts_display") or ""
    # Tuple: (prio_rank ASC, ts DESC) — achieved by (rank, RevStr(ts))
    return (rank, _RevStr(ts))


class _RevStr(str):
    """String subclass that reverses comparison order, giving descending sort."""
    def __lt__(self, other): return str.__gt__(self, other)
    def __gt__(self, other): return str.__lt__(self, other)
    def __le__(self, other): return str.__ge__(self, other)
    def __ge__(self, other): return str.__le__(self, other)


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


def _sort_cards(cards: list) -> list:
    """Return a new list sorted by priority (high→medium→low→none) then ts desc."""
    return sorted(cards, key=_prio_key)


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


def group_by_department(cards: list) -> dict:
    """Group cards by owner field (normalized for case/alias drift). Falls back
    to 'unassigned' if owner is empty. Sorted by descending card count."""
    groups: dict[str, list] = defaultdict(list)
    for card in cards:
        dept = _canon_dept(card.get("owner"))
        groups[dept].append(card)
    # Sort each group's cards by priority then ts
    sorted_groups = {
        dept: _sort_cards(cards_list)
        for dept, cards_list in groups.items()
    }
    # Sort groups by descending card count
    return dict(sorted(sorted_groups.items(), key=lambda kv: -len(kv[1])))


def group_by_project(cards: list) -> dict:
    """Group cards by derived super-project (derive_project).
    Returns a dict sorted by descending card count."""
    groups: dict[str, list] = defaultdict(list)
    for card in cards:
        project = derive_project(card)
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


@router.get("/kanban", response_class=HTMLResponse)
async def kanban_board(request: Request):
    """Full kanban board — all active cards from the dashboard API.

    Passes three groupings to the template:
      - by_status       dict[status_name -> list[card]]  (original view)
      - by_department   dict[dept -> list[card]]
      - by_project      dict[project_name -> list[card]]
    """
    columns = {}
    generated_at = ""
    error = None
    counts = {}
    try:
        req = urllib.request.Request(f"{DASHBOARD}/api/inbox")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        columns = data.get("columns", {})
        generated_at = data.get("generated_at", "")
        counts = data.get("counts", {})
    except urllib.error.HTTPError as e:
        error = f"Dashboard returned HTTP {e.code}"
    except Exception as e:
        error = f"Cannot reach dashboard: {e}"

    # Build all three groupings from the flat card list
    all_cards = _flatten_columns(columns)

    # by_status: preserve original column order, sort cards within each column
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
    by_project = group_by_project(all_cards)

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
        "by_status": by_status_labelled,
        "by_department": by_department,
        "by_project": by_project,
        # Pre-computed accent colours {group_name -> hex}
        "accents_status": build_accent_map(by_status_labelled),
        "accents_dept": build_accent_map(by_department),
        "accents_project": build_accent_map(by_project),
        "error": error,
        "generated_at": generated_at,
        "counts": counts,
    })
