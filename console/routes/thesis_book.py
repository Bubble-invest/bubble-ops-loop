"""GET /dept/<slug>/portfolio — Living Portfolio Report (thesis book).
GET /dept/<slug>/chart/<name> — pre-generated chart PNGs.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from console.services import dept_registry
from console.services import thesis_book as thesis_book_service

router = APIRouter()


def _json_for_inline_script(data) -> str:
    """json.dumps(), but safe to splice DIRECTLY (unquoted) into an inline
    `<script>` block as a JS expression — i.e. `const DATA = ({{ this }});`.

    board #1116 (cockpit audit, stored-XSS): a plain `json.dumps(...)` can
    legitimately contain a literal `</script>` sequence (e.g. any string
    VALUE in `data` — a dept name, a theme title — that happens to contain
    it) or `<!--`, either of which the HTML PARSER (not the JS parser) acts
    on: the browser's HTML tokenizer ends the `<script>` element the moment
    it sees `</script`, REGARDLESS of what JS syntax says, letting whatever
    follows be parsed as ordinary HTML/attacker-controlled markup on the
    SAME origin as gate-approval/comment POSTs. This is the same class of
    bug `json.dumps(..., ensure_ascii=True)` does NOT fix (ensure_ascii only
    escapes non-ASCII codepoints, not `<`/`>`/`&`).

    Fix: escape the 3 characters that matter to an HTML parser inside a
    script body (`<`, `>`, `&`) as JSON unicode escapes (`\\u003c` etc.) —
    valid inside a JS string/object literal (json.dumps already produces
    only double-quoted string values + bare numbers/booleans/null, so this
    can only ever land INSIDE a quoted string, never break JS syntax) and
    invisible to the HTML tokenizer.
    """
    raw = json.dumps(data, separators=(",", ":"))
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _latest_artifact(slug: str, filename: str) -> tuple[str, str | None]:
    """Read the most-recent `outputs/<date>/<filename>` for a dept, walking back
    up to 7 days. Returns (html, day-it-was-found) — the day lets the page stamp
    an 'as-of' and warn on SILENT STALENESS (board #1203): L1 skips its whole
    regen on a degraded / weekend / broker-cold morning, and this most-recent-
    wins read would otherwise serve the prior day's copy with nothing telling the
    reader it isn't today's. Returns ("", None) when nothing is on disk within
    the window (the tab then hides itself)."""
    root = dept_registry.runtime_repo_path(slug)
    if root is None:
        return "", None
    for i in range(7):
        day = (date.today() - timedelta(days=i)).isoformat()
        p = root / "outputs" / day / filename
        if p.exists():
            html = p.read_text(errors="replace")
            # drop the fragment's leading doc-level tags (harmless but tidy)
            html = re.sub(r"^\s*<title>.*?</title>", "", html, count=1, flags=re.I | re.S)
            html = re.sub(r"^\s*(?:<meta\b[^>]*>\s*)+", "", html, count=1, flags=re.I)
            return html, day
    return "", None


@router.get("/dept/{slug}/portfolio", response_class=HTMLResponse)
def thesis_book_page(slug: str, request: Request):
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Unknown dept: {slug}")
    data = thesis_book_service.build_thesis_data(slug)
    data_json = _json_for_inline_script(data)
    review_html, review_day = _latest_artifact(slug, "portfolio-review-artifact.html")
    maps_html, maps_day = _latest_artifact(slug, "value-chain-maps-panel.html")
    # Kill silent staleness (board #1203): the report is only as fresh as its
    # STALEST pre-rendered tab. If any present tab's artifact is older than today
    # — L1 didn't regenerate this morning (degraded/weekend/broker-cold) — stamp
    # the page with the as-of date and a visible banner, rather than passing the
    # prior day's copy off as current. The Thesis tab self-generates on demand
    # (always today), so only the file-based Review + Maps tabs can go stale.
    today_iso = date.today().isoformat()
    present_days = [dy for dy in (review_day, maps_day) if dy]
    as_of = min(present_days) if present_days else None
    is_stale = bool(present_days) and as_of < today_iso
    return request.app.state.templates.TemplateResponse(
        "thesis_book.html",
        {"request": request, "dept": d, "data_json": data_json,
         "review_html": review_html, "maps_html": maps_html,
         "as_of": as_of, "today_iso": today_iso, "is_stale": is_stale},
    )


@router.get("/dept/{slug}/chart/{name:path}")
def chart_file(slug: str, name: str, request: Request):
    """Serve pre-generated chart PNGs from outputs/<today>/charts/."""
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Unknown dept: {slug}")
    if not re.match(r"^[\w.-]+$", name):
        raise HTTPException(status_code=400, detail="Invalid chart name")
    root = dept_registry.runtime_repo_path(slug)
    if root is None:
        raise HTTPException(status_code=404)
    today = date.today().isoformat()
    chart_path = root / "outputs" / today / "charts" / name
    if not chart_path.exists():
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        chart_path = root / "outputs" / yesterday / "charts" / name
    if not chart_path.exists():
        raise HTTPException(status_code=404, detail=f"Chart not found: {name}")
    return FileResponse(str(chart_path), media_type="image/png")
