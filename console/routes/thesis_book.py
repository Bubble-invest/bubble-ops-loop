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


def _latest_review_html(slug: str) -> str:
    """Ben's line-by-line Portfolio Review, pre-rendered by his L1 tool
    (outputs/<date>/portfolio-review-artifact.html — self-contained, #pr-root
    scoped so it cannot collide with the thesis-book styles). Read the most
    recent one on disk; empty string if none (the tab then hides itself)."""
    root = dept_registry.repo_path(slug)
    if root is None:
        return ""
    for i in range(7):
        day = (date.today() - timedelta(days=i)).isoformat()
        p = root / "outputs" / day / "portfolio-review-artifact.html"
        if p.exists():
            html = p.read_text(errors="replace")
            # drop the fragment's leading doc-level tags (harmless but tidy)
            html = re.sub(r"^\s*<title>.*?</title>", "", html, count=1, flags=re.I | re.S)
            html = re.sub(r"^\s*(?:<meta\b[^>]*>\s*)+", "", html, count=1, flags=re.I)
            return html
    return ""


def _latest_maps_html(slug: str) -> str:
    """Ben's self-contained Value-Chain Maps panel, pre-rendered by his L1 tool
    (outputs/<date>/value-chain-maps-panel.html — #vcm-root-scoped so it cannot
    collide with the thesis-book styles). Same read pattern as the Portfolio
    Review artifact; empty string if none (the 3rd tab then hides itself). This
    is board #1078 (Ben's design), replacing an earlier link-to-sub-page tab."""
    root = dept_registry.repo_path(slug)
    if root is None:
        return ""
    for i in range(7):
        day = (date.today() - timedelta(days=i)).isoformat()
        p = root / "outputs" / day / "value-chain-maps-panel.html"
        if p.exists():
            html = p.read_text(errors="replace")
            html = re.sub(r"^\s*<title>.*?</title>", "", html, count=1, flags=re.I | re.S)
            html = re.sub(r"^\s*(?:<meta\b[^>]*>\s*)+", "", html, count=1, flags=re.I)
            return html
    return ""


@router.get("/dept/{slug}/portfolio", response_class=HTMLResponse)
def thesis_book_page(slug: str, request: Request):
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Unknown dept: {slug}")
    data = thesis_book_service.build_thesis_data(slug)
    data_json = json.dumps(data, separators=(",", ":"))
    return request.app.state.templates.TemplateResponse(
        "thesis_book.html",
        {"request": request, "dept": d, "data_json": data_json,
         "review_html": _latest_review_html(slug),
         "maps_html": _latest_maps_html(slug)},
    )


@router.get("/dept/{slug}/chart/{name:path}")
def chart_file(slug: str, name: str, request: Request):
    """Serve pre-generated chart PNGs from outputs/<today>/charts/."""
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Unknown dept: {slug}")
    if not re.match(r"^[\w.-]+$", name):
        raise HTTPException(status_code=400, detail="Invalid chart name")
    root = dept_registry.repo_path(slug)
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
