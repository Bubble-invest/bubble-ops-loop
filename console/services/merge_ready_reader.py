"""
merge_ready_reader.py — surface merge-ready PRs on the cockpit home page (board #469).

Today, PRs that Rick's independent-reviewer has approved and that are just waiting
for Joris to click "merge" only live in Rick's Telegram digests. The cockpit's
"décisions qu'on attend de toi" surface misses them entirely. This module lists
the open PRs on Bubble-invest/bubble-ops-loop and marks a PR "merge-ready" iff any
of its issue-comments contains the marker phrase `Merge-ready for Joris` (the phrase
Rick's independent-review verdicts already end with).

For each merge-ready PR we produce a small, easy-to-read (French) card payload:
  - number, title, html_url, age ("il y a 2 h")
  - explanation:  a `RÉSUMÉ:` line from the PR body if present, else the PR title
                  with its conventional-commit prefix ("fix(console): ") stripped.
  - chips:        evidence parsed from the marker comment — test count («N tests ✓»),
                  mutation testing («mutation-testé»), and always «revue indépendante
                  ✓» (the marker phrase itself implies an independent-review PASS).

Data path mirrors routes/kanban.py exactly: same short-lived board token
(/run/bubble-board/token, GH_TOKEN fallback), same direct REST calls via urllib,
same 60s in-process TTL cache with a single-flight lock (PR #190 pattern). On a
missing token or ANY API error we return [] and log a warning — this surface must
NEVER break the home page.

The repo list is a parameter (default ["bubble-ops-loop"]); the #470 token-widening
enabler will widen coverage with zero code change here.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from console.routes.kanban import _read_board_token

_ORG = "Bubble-invest"
_DEFAULT_REPOS = ["bubble-ops-loop"]

# The independent-review verdict comment Rick's workers post ends with this phrase.
_MARKER = "merge-ready for joris"

# Cache: 60s TTL + single-flight lock, mirroring kanban._fetch_issues (PR #190).
# We cache the result even when it is EMPTY (no merge-ready PR is the common case);
# `_cache_ts == 0.0` is the "never populated" sentinel, so a fresh/expired cache is
# distinguished from a genuinely-empty-but-fresh one.
_CACHE_TTL_SECONDS = 60.0
_cache_data: list = []
_cache_ts: float = 0.0
_cache_lock = threading.Lock()

_log = logging.getLogger(__name__)


def _api_headers(token: str) -> dict:
    """Auth + content headers for a board REST call (same shape as kanban.py)."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bubble-ops-console",
    }


def _get_json(url: str, token: str):
    """GET one JSON resource. Raises on HTTP/network error (caller handles)."""
    req = urllib.request.Request(url, headers=_api_headers(token))
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _age_human(created_at: str, now: Optional[datetime] = None) -> str:
    """Compact French relative age from an ISO-8601 'created_at' (e.g. "il y a 2 h").

    Mirrors morty_reader._age_human's vocabulary. Unparseable → "".
    """
    if not created_at:
        return ""
    raw = created_at.strip()
    try:
        # "2026-07-02T09:00:00Z" → aware datetime
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age_sec = (now - dt).total_seconds()
    if age_sec < 0:
        return "à l'instant"
    if age_sec < 90:
        return "il y a moins d'une minute"
    if age_sec < 3600:
        return f"il y a {round(age_sec / 60)} min"
    if age_sec < 36 * 3600:
        return f"il y a {round(age_sec / 3600)} h"
    return f"il y a {round(age_sec / 86400)} j"


# A conventional-commit prefix like "fix(console): " or "feat: " at the head of a title.
_CC_PREFIX = re.compile(r"^[a-z]+(?:\([^)]*\))?!?:\s*", re.IGNORECASE)


def _explanation(title: str, body: str) -> str:
    """Plain-French explanation for a PR card.

    Prefer a `RÉSUMÉ:` line from the PR body (the dogfooded convention Rick's
    workers now follow); fall back to the PR title with its conventional-commit
    prefix stripped.
    """
    if body:
        for line in body.splitlines():
            s = line.strip()
            # Tolerate a leading markdown marker / bold, e.g. "**RÉSUMÉ:** ..."
            # The bold can close right after the colon ("**RÉSUMÉ:**") or wrap
            # the whole line — strip any leading/trailing markdown emphasis.
            m = re.match(r"^[*_>\s]*RÉSUMÉ\s*:?\s*[*_]*\s*(.+)$", s, re.IGNORECASE)
            if m:
                summary = m.group(1).strip().strip("*_").strip()
                if summary:
                    return summary
    return _CC_PREFIX.sub("", (title or "").strip()).strip() or (title or "").strip()


def _chips(marker_comment: str) -> list[str]:
    """Evidence chips parsed from the marker (verdict) comment.

    - «N tests ✓»       from a "N passed" (e.g. "42 passed") occurrence
    - «mutation-testé»   if the comment mentions mutation testing
    - «revue indépendante ✓»  always (the marker phrase implies an approved
                               independent review)
    """
    chips: list[str] = []
    text = marker_comment or ""
    m = re.search(r"(\d+)\s+passed", text, re.IGNORECASE)
    if m:
        chips.append(f"{m.group(1)} tests ✓")
    if re.search(r"mutation", text, re.IGNORECASE):
        chips.append("mutation-testé")
    chips.append("revue indépendante ✓")
    return chips


def _find_marker_comment(comments: list[dict]) -> Optional[str]:
    """Return the body of the first comment containing the merge-ready marker
    (case-insensitive), or None if no comment carries it."""
    for c in comments:
        body = c.get("body") or ""
        if _MARKER in body.lower():
            return body
    return None


def _fetch_merge_ready_locked(repos: list[str], now: Optional[datetime] = None) -> list:
    """Do the real work of listing merge-ready PRs (assumes the lock is held).

    Returns a list of card dicts. Token missing or ANY API error → [] + a logged
    warning (never raises to the route)."""
    token = _read_board_token()
    if not token:
        _log.warning("merge_ready_reader: no board API token available — skipping")
        return []

    cards: list = []
    try:
        for repo in repos:
            list_url = (
                f"https://api.github.com/repos/{_ORG}/{repo}/pulls"
                f"?state=open&per_page=100"
            )
            pulls = _get_json(list_url, token)
            for pr in pulls:
                number = pr.get("number")
                if number is None:
                    continue
                comments_url = (
                    f"https://api.github.com/repos/{_ORG}/{repo}/issues/{number}/comments"
                )
                comments = _get_json(comments_url, token)
                marker = _find_marker_comment(comments)
                if not marker:
                    continue
                title = pr.get("title") or ""
                cards.append({
                    "repo": repo,
                    "number": number,
                    "title": title.strip(),
                    "html_url": pr.get("html_url") or "",
                    "created_at": pr.get("created_at") or "",
                    "age": _age_human(pr.get("created_at") or "", now=now),
                    "explanation": _explanation(title, pr.get("body") or ""),
                    "chips": _chips(marker),
                })
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            json.JSONDecodeError) as exc:
        _log.warning("merge_ready_reader: board API error — returning empty (%s)", exc)
        return []
    except Exception as exc:  # defensive — this surface must never break home
        _log.warning("merge_ready_reader: unexpected error — returning empty (%s)", exc)
        return []

    return cards


def list_merge_ready(repos: Optional[list[str]] = None) -> list:
    """List merge-ready PRs across `repos` (default ["bubble-ops-loop"]).

    60s in-process TTL cache guarded by a single-flight lock (PR #190 pattern):
    concurrent callers within the TTL share one fetch instead of stampeding the
    GitHub API. Fails safe — returns [] on token-missing / API error, never raises.
    """
    global _cache_data, _cache_ts
    repos = repos if repos is not None else _DEFAULT_REPOS

    now = time.monotonic()
    if _cache_ts and now - _cache_ts < _CACHE_TTL_SECONDS:
        return _cache_data

    with _cache_lock:
        # Re-check inside the lock: another thread may have just populated it.
        now = time.monotonic()
        if _cache_ts and now - _cache_ts < _CACHE_TTL_SECONDS:
            return _cache_data

        cards = _fetch_merge_ready_locked(repos)
        _cache_data = cards
        _cache_ts = time.monotonic()
        return cards
