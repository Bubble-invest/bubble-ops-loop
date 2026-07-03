"""GET / — cabinet d'éclosion home (décisions awaiting + équipe + KPIs)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from console.services import (
    backup_history,
    concierge_reader,
    dept_registry,
    github_reader,
    merge_ready_reader,
)
from console.services.gate_grouping import group_gates_by_kind

router = APIRouter()


# Back-compat alias for tests that import the private helper.
# Canonical home lives in console.services.gate_grouping — also used by
# dept.py so / and /dept/<slug> apply identical grouping rules (msg 3030).
def _group_gates_by_kind(gates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return group_gates_by_kind(gates)


def _kanban_queue_counts() -> Dict[str, int]:
    """Return kanban queue counts by status, reusing the kanban route's in-process cache.

    Imports lazily to avoid a circular-import at module load time (home and
    kanban are both registered on the same router set, but neither imports the
    other at the top level).  The cache (_cache_data / _cache_ts) lives in
    kanban.py and is shared — hitting / does NOT add a second uncached network
    call when /kanban has already populated the cache.

    Returns a dict with keys:
      total_open, needs_attention, investigating, waiting, done
    All values are int (0 if the board is unreachable or empty).
    """
    from console.routes import kanban as _kanban  # lazy — avoids circular import

    issues, _err = _kanban._fetch_issues()

    counts: Dict[str, int] = defaultdict(int)
    for issue in issues:
        card = _kanban.issue_to_card(issue)
        counts[card["column"]] += 1

    total_open = sum(
        counts[col] for col in ("needs_attention", "investigating", "waiting")
    )
    return {
        "total_open":      total_open,
        "needs_attention": counts["needs_attention"],
        "investigating":   counts["investigating"],
        "waiting":         counts["waiting"],
        "done":            counts["done"],
    }


def _board_decision_cards() -> list:
    """ALL open needs:human board cards Joris owes a decision on — surfaced on
    the landing page (board #358, widened by #505). Reuses the kanban
    in-process cache, so no extra network call.

    Board #505: the dept gates do NOT cover every dept (8+ needs:human cards
    carry no host: label, so no gate ever surfaces them) — so this must return
    EVERY open needs:human card, not just dept:rnd's. Each carries its
    id/title/summary/owner/due for the decision-card UI, sorted
    overdue/soonest-due first (undated last) WITHIN owner group."""
    from console.routes import kanban as _kanban  # lazy — avoid circular import
    issues, _err = _kanban._fetch_issues()
    out = []
    for issue in issues:
        card = _kanban.issue_to_card(issue)
        if card.get("needs_human"):
            out.append(card)
    # overdue/soonest-due first, undated last
    def _key(c):
        d = c.get("due") or ""
        return (0, d) if d else (1, c.get("id", ""))
    return sorted(out, key=_key)


def _group_decision_cards_by_dept(cards: list) -> list:
    """Group needs:human cards by owner dept for the landing page (board #505).

    Order: 'rnd' first (Rick's own build decisions — kept where operators
    already expect them), then every other dept alphabetically, then a
    no-dept bucket ("") last for cards with no dept: label at all. Cards are
    already due-sorted by `_board_decision_cards`; that order is preserved
    within each group (stable sort).

    Returns a list of {"owner": str, "label": str, "cards": [...]} dicts —
    empty groups are omitted.
    """
    by_owner: Dict[str, list] = defaultdict(list)
    for c in cards:
        by_owner[c.get("owner") or ""].append(c)

    other_owners = sorted(o for o in by_owner if o not in ("rnd", ""))
    ordered_owners = [o for o in (["rnd"] + other_owners + [""]) if by_owner.get(o)]

    groups = []
    for owner in ordered_owners:
        label = f"dept:{owner}" if owner else "sans dept"
        groups.append({
            "owner": owner,
            "label": label,
            "cards": by_owner[owner],
        })
    return groups


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    # Exclude anciens (Retired/Cancelled) — they have their own section on
    # /agents and their gates are stale by definition. Regression caught
    # 2026-05-24 msg 3041: after retiring fixture, its 9 stale gates kept
    # surfacing on home and fixture itself was listed as "en éclosion".
    # Also exclude concierges (morty, claudette) — they are surfaced separately
    # via concierge_reader.list_concierges() in the "Les concierges" subsection
    # below. Including them in list_departments() caused them to render twice
    # (once here in "L'équipe" and once in "Les concierges"). Fix #212.
    depts = [
        d for d in dept_registry.list_departments()
        if not d.is_ancien
        and d.slug not in dept_registry.KNOWN_CONCIERGE_SLUGS
    ]
    columns = []
    for d in depts:
        gates = github_reader.list_pending_gates(d.slug)
        columns.append({
            "dept": d,
            "gates": gates,
            "gate_count": len(gates),
            "gate_groups": _group_gates_by_kind(gates),
        })
    total_gates = sum(c["gate_count"] for c in columns)
    # Safety-net roll-up — last loop-backup fire across all depts ({{OPERATOR}} msg
    # 1171). One compact banner so the operator sees the net is live + acting.
    backup_rollup = backup_history.rollup()
    # Concierges (Morty, Claudette) — reactive assistants, not loop-depts;
    # listed in their own home sub-section with a link to their live page.
    concierges = concierge_reader.list_concierges()
    # Kanban queue counts — reuse kanban.py's in-process cache (no added latency).
    kanban_counts = _kanban_queue_counts()
    # ALL open needs:human board cards (board #358, widened to every dept by
    # #505 — previously filtered to dept:rnd only, hiding 14/15 cards from
    # Joris because the dept gates don't cover cards with no host: label).
    # `rnd_decisions`/`rnd_decision_count` names kept for back-compat (hero
    # counter math + any external readers); `decision_groups` is the new
    # dept-grouped structure the template iterates to render every card.
    rnd_decisions = _board_decision_cards()
    decision_groups = _group_decision_cards_by_dept(rnd_decisions)
    # Merge-ready PRs — reviewed + waiting for Joris to merge (board #469).
    # Read-only surface; fails safe to [] on token-missing/API error.
    merge_ready = merge_ready_reader.list_merge_ready()
    # Recent decisions tray — last ~10 decisions across all live depts, newest first.
    all_slugs = [col["dept"].slug for col in columns]
    recent_decisions = github_reader.list_recent_decisions(all_slugs, limit=10)
    return request.app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "columns": columns,
            "total_gates": total_gates,
            "rnd_decisions": rnd_decisions,
            "rnd_decision_count": len(rnd_decisions),
            "decision_groups": decision_groups,
            "merge_ready": merge_ready,
            "live_count": len([c for c in columns if c["dept"].is_live]),
            "eclore_count": len([c for c in columns if not c["dept"].is_live]),
            "backup_rollup": backup_rollup,
            "concierges": concierges,
            "kanban_counts": kanban_counts,
            "recent_decisions": recent_decisions,
        },
    )
