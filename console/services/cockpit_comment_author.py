"""Recover the HUMAN author of a cockpit-posted board comment (board #1136).

Every decision / reply the cockpit writes onto a board issue is POSTed with the
board bot's token, so GitHub attributes it to `bubble-ops-bot`. But those
comments are machine-appended on behalf of a human who acted through the cockpit
gate (`routes/kanban.py::_apply_card_decision`), and each marker carries that
human's name, e.g.:

    ✅ Approved by Joris via cockpit
    ❌ Rejected by Jade via cockpit
    ⏳ Deferred by Joris — back to Rick's queue
    📝 Note de Joris : …

This module extracts that name so the cockpit UI can show "Joris" (with an
initial avatar) instead of "bubble-ops-bot" on the card-detail comment thread.

IMPORTANT — display-only. We NEVER alter the stored marker text: rnd_loop's
answered-detection greps the raw comment body for "<name> via cockpit", so the
marker must stay byte-intact on GitHub. This layer only annotates the in-memory
comment dicts the template renders.
"""

from __future__ import annotations

import re
from typing import Any

# Marker prefixes the cockpit machine-appends when a HUMAN acts via the gate.
# Anchored at the START of the body so a human quoting the phrase mid-comment on
# GitHub can never spoof a name onto their own comment. The clarify marker
# («🔍 Pas clair») is intentionally absent: it is byte-exact and carries no name,
# so such comments correctly keep their GitHub (bot) attribution.
_MARKER_PATTERNS = (
    re.compile(r"^\s*✅ Approved by (?P<name>.+?) via cockpit\b"),
    re.compile(r"^\s*❌ Rejected by (?P<name>.+?) via cockpit\b"),
    re.compile(r"^\s*⏳ Deferred by (?P<name>.+?) —"),
    re.compile(r"^\s*📝 Note de (?P<name>.+?) :"),
)


def cockpit_human_name(body: str | None) -> str | None:
    """Return the human name embedded in a cockpit-gate marker comment body, or
    None if `body` is not such a marker. Pure/side-effect-free."""
    if not body:
        return None
    for pat in _MARKER_PATTERNS:
        m = pat.match(body)
        if m:
            name = (m.group("name") or "").strip()
            if name:
                return name
    return None


def annotate_comment_authors(comments: list[dict[str, Any]] | None) -> list[dict]:
    """Return a shallow copy of `comments` with display fields added to each:

      - display_author      : human name (cockpit marker) else the GitHub login
      - display_via_cockpit  : True when the author was recovered from a marker
      - display_initial      : first letter of display_author, upper-cased

    Source dicts are not mutated. Robust to missing `user`/`body` keys."""
    out: list[dict] = []
    for c in comments or []:
        c = dict(c)
        login = ((c.get("user") or {}).get("login")) or "inconnu"
        human = cockpit_human_name(c.get("body") or "")
        c["display_author"] = human or login
        c["display_via_cockpit"] = human is not None
        c["display_initial"] = (c["display_author"] or "?")[:1].upper()
        out.append(c)
    return out
