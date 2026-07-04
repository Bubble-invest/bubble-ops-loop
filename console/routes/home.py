"""GET / — cabinet d'éclosion home (décisions awaiting + équipe + KPIs)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from console.services import (
    backup_history,
    concierge_reader,
    cost_tracker,
    dept_registry,
    github_reader,
    merge_ready_reader,
)
from console.services.gate_grouping import group_gates_by_kind
from console.services.humanize import humanize_kind, humanize_risk
from console.services.markdown_render import render_markdown_safe

router = APIRouter()

# Highest → lowest signal ordering for the "Cartes riches" featured section.
# Higher-risk proposals surface first (they most deserve the operator's eye).
_RISK_RANK = {"critical": 0, "high": 1, "moderate": 2, "medium": 2, "low": 3}

# Card-sized excerpt: truncate the RAW thesis text (at a word boundary) BEFORE
# markdown-rendering, so we never cut inside an HTML tag the renderer emits.
_THESIS_EXCERPT_CHARS = 320


def _thesis_excerpt(summary: Any):
    """Return a card-sized rendered-markdown excerpt of a gate's thesis.

    `summary` is agent-authored (untrusted) — it goes through the same
    nh3-sanitized markdown pipeline gate.py uses (`_attach_thesis_rendered`).
    Only plain-string summaries are excerpted+rendered; structured (dict)
    summaries — a few content-dept kinds — return None so the card falls back
    to its title only (rendering a dict as markdown makes no sense).
    """
    if not isinstance(summary, str):
        return None
    text = summary.strip()
    if len(text) > _THESIS_EXCERPT_CHARS:
        cut = text[:_THESIS_EXCERPT_CHARS]
        # back off to the last whitespace so we don't split a word/markdown token
        sp = cut.rfind(" ")
        if sp > 0:
            cut = cut[:sp]
        text = cut.rstrip() + "…"
    return render_markdown_safe(text)


def _rich_cards(columns: list, limit: int = 3) -> list:
    """Up to `limit` REAL pending gate-proposals to feature in the home
    "Cartes riches" section (board #533).

    Reuses the per-dept gates ALREADY fetched into `columns` by the route (no
    redundant network scan). Across all depts, ranks pending gates by
    risk_level (higher first) then keeps a small, high-signal set. Malformed
    placeholder gates are skipped — they surface on the dept page, not here.

    Each card carries: slug, display_name, id, title, kind_label, risk_label,
    thesis (rendered-markdown excerpt or None), href (/gate/<slug>/<id>).
    Fully guarded — any per-gate hiccup is skipped, never a 500.
    """
    candidates = []
    for col in columns:
        dept = col["dept"]
        for g in col.get("gates", []):
            if not isinstance(g, dict) or g.get("_malformed"):
                continue
            candidates.append((dept, g))

    def _key(item):
        _dept, g = item
        risk = str(g.get("risk_level") or "").strip().lower()
        return _RISK_RANK.get(risk, 2)

    candidates.sort(key=_key)

    cards = []
    for dept, g in candidates[:limit]:
        gid = g.get("id")
        if not gid:
            continue
        # Prefer an explicit title; else the humanized kind as a stand-in.
        title = g.get("title") or humanize_kind(g.get("kind"))
        cards.append({
            "slug": dept.slug,
            "display_name": dept.display_name,
            "id": gid,
            "title": title,
            "kind_label": humanize_kind(g.get("kind")),
            "risk_label": humanize_risk(g.get("risk_level")),
            "thesis": _thesis_excerpt(g.get("summary")),
            "href": f"/gate/{dept.slug}/{gid}",
        })
    return cards


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


def _dept_card_counts() -> Dict[str, int]:
    """Per-dept count of OPEN board cards tagged `dept:<slug>` (board #531).

    Powers the 3rd stat-tile ("cartes kanban") on each dept widget. Matches the
    /kanban by-department view's semantics exactly: bucket every open issue by
    `_kanban._canon_dept(card["owner"])` (owner derived from the `dept:<x>`
    label, then case/alias-normalised so e.g. `dept:rick / rnd` still counts
    toward `rnd`). Reuses `_kanban._fetch_issues()` — the same in-process cache
    the decision cards + queue counts already hit, so NO extra network call.

    Returns a dict keyed by canonical dept slug → int. Fully guarded: any fetch
    error degrades to an empty dict (→ every widget shows 0), never a 500.
    """
    from console.routes import kanban as _kanban  # lazy — avoid circular import

    counts: Dict[str, int] = defaultdict(int)
    try:
        issues, _err = _kanban._fetch_issues()
        for issue in issues:
            card = _kanban.issue_to_card(issue)
            counts[_kanban._canon_dept(card.get("owner"))] += 1
    except Exception:  # noqa: BLE001 — a board hiccup must never 500 the home page
        return {}
    return dict(counts)


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


def _dept_budgets(columns: list) -> list:
    """Per-LIVE-dept budget-vs-spend rows for the home 'Coûts' section (#524d).

    For each live dept: budget = Σ budget_usd across its recurring_missions[]
    (read-only, operator-set, via cost_tracker.mission_budget_total on its
    dept.yaml); spent = its real-$ week spend rolled up from the cost report.
    Returns [{slug, display_name, spent, budget, pct, level, defined}], sorted
    over-budget first then by spend desc so the tightest budgets surface on top.

    Fully guarded: a cost-scan error, a missing dept.yaml, or an unset budget
    never raises — the whole thing degrades to [] (on report failure) or to
    individual "budget non défini" rows.
    """
    try:
        report = cost_tracker.build_report(refresh=False)
        spent_map = cost_tracker.spent_by_dept(report, span="week")
    except Exception:  # noqa: BLE001 — cost scan must never 500 the home page
        return []

    rows = []
    for col in columns:
        d = col["dept"]
        if not d.is_live:
            continue
        try:
            dept_yaml = github_reader.load_dept_yaml(d.slug)
            budget = cost_tracker.mission_budget_total(dept_yaml)
        except Exception:  # noqa: BLE001 — a bad yaml on one dept must not sink the row
            budget = None
        spent = spent_map.get(d.slug, 0.0)
        status = cost_tracker.budget_status(spent, budget)
        rows.append({
            "slug": d.slug,
            "display_name": d.display_name,
            **status,
        })

    # over-budget first, then by spend desc (defined budgets before undefined
    # within equal spend so the meaningful bars lead).
    def _key(r):
        over = 0 if r["level"] == "over" else 1
        return (over, -r["spent"], 0 if r["defined"] else 1)
    return sorted(rows, key=_key)


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
    # Per-dept count of OPEN board cards tagged dept:<slug> (board #531) — feeds
    # the 3rd stat-tile on each widget. Reuses the kanban in-process cache (no
    # extra network call); degrades to {} → every widget shows 0 on any error.
    dept_card_counts = _dept_card_counts()
    columns = []
    for d in depts:
        gates = github_reader.list_pending_gates(d.slug)
        columns.append({
            "dept": d,
            "gates": gates,
            "gate_count": len(gates),
            "gate_groups": _group_gates_by_kind(gates),
            "card_count": dept_card_counts.get(d.slug, 0),
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
    # Per-dept BUDGET vs SPEND this week (board #524d). One row per LIVE dept:
    # budget = Σ budget_usd across its recurring_missions[] (operator-set, read-
    # only); spent = its real-$ week cost from cost_tracker. Fully guarded — a
    # cost-scan or missing-budget hiccup degrades to [] / "budget non défini",
    # never a 500. Budgets are unset in most dept.yaml today → the bars render
    # "budget non défini" until Joris sets them (graceful degradation).
    dept_budgets = _dept_budgets(columns)
    # Cartes riches (board #533) — REAL pending gate-proposals featured on the
    # home page, replacing the old hardcoded illustrative INDA/EEM/SPY chart.
    # Reuses the gates already on `columns` (no extra scan); empty → the
    # template shows a quiet placeholder, never the fake chart.
    rich_cards = _rich_cards(columns)
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
            "dept_budgets": dept_budgets,
            "rich_cards": rich_cards,
        },
    )
