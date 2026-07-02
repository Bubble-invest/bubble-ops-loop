"""
merge_ready_reader.py — surface merge-ready PRs on the cockpit home page (board #469).

Today, PRs that Rick's independent-reviewer has approved and that are just waiting
for Joris to click "merge" only live in Rick's Telegram digests. The cockpit's
"décisions qu'on attend de toi" surface misses them entirely. This module lists
the open PRs on Bubble-invest/bubble-ops-loop and marks a PR "merge-ready" iff its
LATEST verdict comment carries the marker phrase `Merge-ready for Joris` (the phrase
Rick's independent-review verdicts already end with).

For each merge-ready PR we produce a small, easy-to-read (French) card payload:
  - number, title, html_url, age ("il y a 2 h")
  - explanation:  a `RÉSUMÉ:` line from the PR body if present, else the PR title
                  with its conventional-commit prefix ("fix(console): ") stripped.
  - chips:        evidence parsed from the marker comment — test count («N tests ✓»),
                  mutation testing («mutation-testé»), and always «revue indépendante
                  ✓» (the marker phrase itself implies an independent-review PASS).

Data path mirrors routes/kanban.py: same short-lived board token
(/run/bubble-board/token, GH_TOKEN fallback), same direct REST calls via urllib.
On a missing token or ANY API error we return [] and log a warning — this surface
must NEVER break the home page.

Latency (card forbids any home-page latency regression):
  * SERVE-STALE-WHILE-REVALIDATE — `list_merge_ready` returns the current cached
    list IMMEDIATELY (never blocks on the network). When the 60s TTL has expired it
    kicks a single daemon background thread to refresh the cache; a guard ensures at
    most one refresh runs at a time. The FIRST-EVER load (no cache at all) returns []
    immediately and lets the background refresh populate it — the panel then appears
    on a subsequent visit.
  * CONDITIONAL COMMENT-FETCH — a per-PR memo keyed by (repo, number) stores
    (updated_at, card_or_None). During a refresh we only fetch the (expensive)
    issue-comments for PRs whose `updated_at` changed since the memo; unchanged PRs
    reuse their memoized verdict. This bounds the fan-out to genuinely-changed PRs.

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
# A FAIL verdict may legitimately QUOTE the merge-ready phrase (e.g. "does not yet
# meet 'Merge-ready for Joris'"). We use the presence of a FAIL/request-changes
# verdict token to recognise a comment as a *verdict* comment even when its
# merge-ready mention is quoted — see `_comment_is_merge_ready`.
_FAIL_TOKENS = ("verdict: fail", "verdict : fail", "request changes",
                "request-changes", "changes requested")

# ── Cache + refresh guards ────────────────────────────────────────────────────
# 60s TTL. `_cache_ts == 0.0` is the "never populated" sentinel.
_CACHE_TTL_SECONDS = 60.0
_cache_data: list = []
_cache_ts: float = 0.0
_cache_lock = threading.Lock()          # protects _cache_data / _cache_ts reads+writes
# At most ONE background refresh runs at a time. acquire(blocking=False) is the guard:
# if a refresh is already in flight, a second expiry just serves stale and moves on.
_refresh_lock = threading.Lock()

# Per-PR memo: (repo, number) -> (updated_at, card_or_None). Lets a refresh skip the
# comments fetch for PRs whose updated_at is unchanged. Guarded by _memo_lock.
_pr_memo: dict[tuple[str, int], tuple[str, Optional[dict]]] = {}
_memo_lock = threading.Lock()

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


# A conventional-commit prefix at the head of a title: one of the canonical CC
# types, optional (scope), optional "!", then ":". Restricting to the type set
# (not "any word") avoids over-stripping ordinary "Word:" titles like "Bug: …".
_CC_PREFIX = re.compile(
    r"^(?:feat|fix|chore|docs|refactor|test|perf|ci|build|style|revert)"
    r"(?:\([^)]*\))?!?:\s*",
    re.IGNORECASE,
)


def _explanation(title: str, body: str) -> str:
    """Plain-French explanation for a PR card.

    Prefer a `RÉSUMÉ:` line from the PR body (the dogfooded convention Rick's
    workers now follow); fall back to the PR title with its conventional-commit
    prefix stripped (only genuine CC types are stripped — "Bug: x" stays intact).
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


def _marker_at_line_start(body: str) -> bool:
    """True iff the merge-ready marker appears as a genuine verdict line — i.e. on a
    line that starts with the phrase (allowing leading markdown emphasis / spaces)
    and is NOT a markdown blockquote ('>') or an indented code line. A FAIL comment
    that merely QUOTES the phrase carries it inside a '>' quote or mid-sentence, so
    this rule rejects it."""
    for raw in body.splitlines():
        # A blockquote line quotes someone else — never a verdict of THIS comment.
        if raw.lstrip().startswith(">"):
            continue
        # An indented (4-space / tab) code line is quoted content too.
        if raw.startswith("    ") or raw.startswith("\t"):
            continue
        # Strip leading markdown emphasis / bullet / spaces, then require the marker
        # at the very start of the remaining text.
        stripped = re.sub(r"^[*_>#\-\s]+", "", raw).lower()
        if stripped.startswith(_MARKER):
            return True
    return False


def _comment_is_merge_ready(body: str) -> bool:
    """Decide whether a single comment is a genuine merge-ready verdict.

    Rules (simplest robust form):
      - the comment must mention the marker phrase at all, AND
      - the phrase must appear at a line start OUTSIDE a quote/code context
        (`_marker_at_line_start`). A FAIL verdict that quotes "Merge-ready for
        Joris" carries it inside a '>' blockquote / mid-sentence, so it is rejected.
    """
    low = (body or "").lower()
    if _MARKER not in low:
        return False
    return _marker_at_line_start(body or "")


def _find_marker_comment(comments: list[dict]) -> Optional[str]:
    """Return the body of the LAST verdict comment iff that latest verdict is
    merge-ready, else None.

    We look only at the most recent VERDICT (a comment carrying either the
    merge-ready marker or a FAIL/request-changes token). A later FAIL that quotes
    the merge-ready phrase must NOT re-flag the PR — so if the last verdict comment
    is a FAIL, we return None even though an earlier comment said merge-ready.
    """
    last_verdict_body: Optional[str] = None
    for c in comments:
        body = c.get("body") or ""
        low = body.lower()
        is_fail = any(tok in low for tok in _FAIL_TOKENS)
        mentions_marker = _MARKER in low
        if is_fail or mentions_marker:
            last_verdict_body = body
    if last_verdict_body is None:
        return None
    return last_verdict_body if _comment_is_merge_ready(last_verdict_body) else None


def _build_card(repo: str, pr: dict, marker: str, now: Optional[datetime]) -> dict:
    """Assemble the home-card payload for one merge-ready PR."""
    title = pr.get("title") or ""
    return {
        "repo": repo,
        "number": pr.get("number"),
        "title": title.strip(),
        "html_url": pr.get("html_url") or "",
        "created_at": pr.get("created_at") or "",
        "age": _age_human(pr.get("created_at") or "", now=now),
        "explanation": _explanation(title, pr.get("body") or ""),
        "chips": _chips(marker),
    }


def _compute_merge_ready(repos: list[str], now: Optional[datetime] = None) -> list:
    """Do the real work of listing merge-ready PRs (network; runs off the request
    path in the background refresh thread).

    Uses the per-PR memo so unchanged PRs (same `updated_at`) skip the comments
    fetch and reuse their prior verdict. Token missing or ANY API error → [] + a
    logged warning (never raises to the caller). On error we leave the memo intact
    so the next refresh can still short-circuit unchanged PRs."""
    token = _read_board_token()
    if not token:
        _log.warning("merge_ready_reader: no board API token available — skipping")
        return []

    cards: list = []
    fresh_memo: dict[tuple[str, int], tuple[str, Optional[dict]]] = {}
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
                key = (repo, number)
                updated_at = pr.get("updated_at") or ""

                # Conditional comment-fetch: reuse the memoized verdict if this PR's
                # updated_at is unchanged since we last looked.
                with _memo_lock:
                    memoized = _pr_memo.get(key)
                if memoized and memoized[0] == updated_at:
                    prev_card = memoized[1]
                    fresh_memo[key] = memoized
                    if prev_card is not None:
                        # Refresh the age against `now`, keep the rest memoized.
                        card = dict(prev_card)
                        card["age"] = _age_human(card.get("created_at") or "", now=now)
                        cards.append(card)
                    continue

                # Changed (or never-seen) PR → fetch its comments.
                comments_url = (
                    f"https://api.github.com/repos/{_ORG}/{repo}/issues/{number}/comments"
                )
                comments = _get_json(comments_url, token)
                marker = _find_marker_comment(comments)
                if not marker:
                    fresh_memo[key] = (updated_at, None)
                    continue
                card = _build_card(repo, pr, marker, now)
                fresh_memo[key] = (updated_at, card)
                cards.append(card)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            json.JSONDecodeError) as exc:
        _log.warning("merge_ready_reader: board API error — returning empty (%s)", exc)
        return []
    except Exception as exc:  # defensive — this surface must never break home
        _log.warning("merge_ready_reader: unexpected error — returning empty (%s)", exc)
        return []

    # Swap in the fresh memo (drops PRs that closed since last refresh).
    with _memo_lock:
        _pr_memo.clear()
        _pr_memo.update(fresh_memo)
    return cards


def _refresh_cache(repos: list[str]) -> None:
    """Recompute the cache once. Guarded so only ONE refresh runs at a time: if a
    refresh is already in flight, this call returns immediately (the in-flight one
    will publish fresh data). Never raises — this runs on a daemon thread."""
    global _cache_data, _cache_ts
    if not _refresh_lock.acquire(blocking=False):
        return  # another refresh is already running
    try:
        cards = _compute_merge_ready(repos)
        with _cache_lock:
            _cache_data = cards
            _cache_ts = time.monotonic()
    finally:
        _refresh_lock.release()


def _kick_background_refresh(repos: list[str]) -> None:
    """Start a single daemon thread to refresh the cache, unless one is already
    running (the _refresh_lock guard inside _refresh_cache makes a redundant thread
    a cheap no-op, but we also avoid spawning it when we can cheaply tell)."""
    if _refresh_lock.locked():
        return
    t = threading.Thread(
        target=_refresh_cache, args=(repos,),
        name="merge-ready-refresh", daemon=True,
    )
    t.start()


def list_merge_ready(repos: Optional[list[str]] = None) -> list:
    """List merge-ready PRs across `repos` (default ["bubble-ops-loop"]).

    NEVER blocks on the network (card forbids home-page latency regression):
      - fresh cache (within 60s) → return it directly.
      - stale cache (TTL expired) → return the STALE list immediately and kick a
        single background refresh.
      - no cache yet (first-ever load) → return [] immediately and kick the refresh;
        the panel appears on a subsequent visit once the refresh completes.

    Fails safe — returns [] on token-missing / API error, never raises.
    """
    repos = repos if repos is not None else _DEFAULT_REPOS

    with _cache_lock:
        ts = _cache_ts
        data = _cache_data
    now = time.monotonic()

    if ts and now - ts < _CACHE_TTL_SECONDS:
        return data  # fresh — serve directly

    # Stale or never-populated: serve what we have (possibly []) and revalidate.
    _kick_background_refresh(repos)
    return data
