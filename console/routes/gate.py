"""
GET  /gate/<dept>/kind/<kind>   — BATCH view: all pending gates of one kind,
                                  each with an inline action form (triage many
                                  at once; deciding one swaps just that card).
GET  /gate/<dept>/chart         — serve a gate's price-comparison chart PNG
                                  (auth-gated, path-traversal-proof).
GET  /gate/<dept>/attachment    — serve a general gate attachment (image or file)
                                  (auth-gated, path-traversal-proof, ext-allowlisted).
GET  /gate/<dept>/<id>          — decision card with 4 actions (single gate)
POST /gate/<dept>/<id>/decide   — writes inbox/decisions/<id>.yaml
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from console.services import dept_registry, github_reader
from console.services.humanize import GATE_CHANNELS, gate_channel, humanize_kind
from console.services.markdown_render import render_markdown_safe

router = APIRouter()

ALLOWED_ACTIONS = {"approve", "reject", "modify", "defer"}


def _attach_thesis_rendered(gate: dict) -> dict:
    """Mutate `gate` in place, adding `thesis_rendered` — the sanitized
    markdown->HTML Markup of `gate.summary` (card #523-A).

    Ben's (and other depts') trade-proposal thesis was rendered with
    `white-space:pre-line` on the raw `gate.summary` string — newlines were
    preserved but markdown (**bold**, ## headings, - bullets) showed up as
    literal characters instead of formatted HTML. This mirrors the #507
    whiteboard-notes fix: agent-authored text is untrusted, so it goes
    through the same nh3-sanitized markdown pipeline before the template
    ever sees it.

    Only applies when `gate.summary` is a plain string — a handful of dept
    kinds (Miranda's content gates) emit `summary` as a structured dict
    (hook/theme/compliance/...), which the templates already render field-by
    -field; markdown-rendering a dict makes no sense, so we skip it there.
    """
    summary = gate.get("summary")
    if isinstance(summary, str):
        gate["thesis_rendered"] = render_markdown_safe(summary)
    else:
        gate["thesis_rendered"] = None
    return gate


def _attach_payload_rendered(slug: str, gate: dict) -> dict:
    """Mutate `gate` in place, adding `payload_rendered` — the sanitized
    HTML of the artifact `gate.approval_bridge` actually points at, so the
    gate DETAIL view can show what {{OPERATOR}} is being asked to approve
    (card #642 follow-up: Jade reported the cockpit rendered only the
    thesis/summary for gates whose real content — e.g. the 7 tweets of an
    X-thread gate — lives in a separate payload file).

    Two payload shapes, both read-only, both allowlisted server-side
    (github_reader.resolve_gate_payload_path / read_substack_queue_note —
    same containment model as the #622 mission-file reader and the existing
    chart/attachment resolvers):
      - source: payload             — item_ref is a repo-relative .md/.txt
        path under outputs/; rendered as sanitized markdown->HTML.
      - source: substack_queue_json — item_ref is a Note id looked up in
        substack/data/queue.json; rendered as sanitized markdown->HTML of
        the note's plain-text `text` field (markdown renderer handles plain
        text fine — no markdown syntax expected, but any incidental
        characters still go through the same sanitize path, not raw).

    Sets `gate.payload_rendered` to a Markup on success, or a short French
    "introuvable" message (also markupsafe-safe, plain text) on any failure
    — the card must never look broken because a file moved. No-op (None)
    when the gate has no approval_bridge or an unrecognized source.
    """
    bridge = gate.get("approval_bridge")
    gate["payload_rendered"] = None
    if not isinstance(bridge, dict):
        return gate
    source = bridge.get("source")
    item_ref = bridge.get("item_ref")
    if not item_ref or not isinstance(item_ref, str):
        return gate

    if source == "payload":
        text = github_reader.read_gate_payload_text(slug, item_ref)
    elif source == "substack_queue_json":
        text = github_reader.read_substack_queue_note(slug, item_ref)
    else:
        return gate

    if text is None:
        gate["payload_rendered"] = render_markdown_safe(
            "_Contenu à valider introuvable — le fichier source a peut-être bougé "
            "ou été renommé. Vérifie `queues/gates/" + gate.get("id", "") + ".yaml` "
            "(champ `approval_bridge`)._"
        )
    else:
        gate["payload_rendered"] = render_markdown_safe(text)
    return gate


SORT_DATE_ASC = "date_asc"
SORT_DATE_DESC = "date_desc"
ALLOWED_SORTS = {SORT_DATE_ASC, SORT_DATE_DESC}


# IMPORTANT: this MUST be declared before /gate/{slug}/{gate_id} — otherwise
# FastAPI would match "kind" as a gate_id. Specific routes before catch-all.
@router.get("/gate/{slug}/kind/{kind}", response_class=HTMLResponse)
def gate_batch(
    slug: str, kind: str, request: Request,
    sort: str = Query(SORT_DATE_ASC),
    channel: str = Query(""),
):
    """List every pending gate of `kind` for the dept, each with an inline
    decision form. Fixes the two triage pains (2026-06-01): see all at once,
    and act-then-advance in place instead of being stranded on gate #1.

    Jade's triage-UX ask (wave 2, card #666 follow-up): the pile of gates for
    a busy kind (e.g. 35 prospect_dm) needed a way to sort and narrow it down
    beyond scrolling. Adds two OPTIONAL query params, both graceful no-op
    when absent/invalid — the pre-existing behaviour (oldest-first, all
    channels) is the default, so a bookmarked/shared /gate/<slug>/kind/<kind>
    link keeps working exactly as before:
      sort    — "date_asc" (default, oldest first — matches list_pending_gates'
                own ordering, card #666) or "date_desc" (newest first). Any
                other value falls back to date_asc rather than erroring.
      channel — one of humanize.GATE_CHANNELS (linkedin/substack/x/newsletter/
                other). Empty/unknown value = no filter (show every channel).
    """
    if dept_registry.get_department(slug) is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    if sort not in ALLOWED_SORTS:
        sort = SORT_DATE_ASC
    channel = (channel or "").strip().lower()
    if channel not in GATE_CHANNELS:
        channel = ""

    gates = [_attach_thesis_rendered(g) for g in github_reader.list_pending_gates(slug)
             if (g.get("kind") or "decision") == kind]
    total_count = len(gates)

    # Per-channel counts (pre-filter) for the Option A chip row's count
    # badges ("LinkedIn 6", "Substack 4", ...) — computed once here so the
    # template never has to loop `gates` per channel to count.
    channel_counts: dict[str, int] = {ch: 0 for ch in GATE_CHANNELS}
    for g in gates:
        channel_counts[gate_channel(g)] += 1

    if channel:
        gates = [g for g in gates if gate_channel(g) == channel]

    # list_pending_gates already returns oldest-first (board #666) — that IS
    # date_asc, so no re-sort needed for the default. date_desc reverses it.
    # `_gate_date` is None for the rare gate with no determinable date; those
    # sort last in both directions (never crowd out dated gates at the top).
    if sort == SORT_DATE_DESC:
        gates = sorted(gates, key=lambda g: g.get("_gate_date") or date.min, reverse=True)

    return request.app.state.templates.TemplateResponse(
        "gate_batch.html",
        {
            "request": request,
            "slug": slug,
            "kind": kind,
            "kind_label": humanize_kind(kind),
            "gates": gates,
            "count": len(gates),
            "total_count": total_count,
            "actions": sorted(ALLOWED_ACTIONS),
            "sort": sort,
            "channel": channel,
            "channels": GATE_CHANNELS,
            "channel_counts": channel_counts,
        },
    )


# IMPORTANT: declared before /gate/{slug}/{gate_id} so "chart" is never matched
# as a gate_id. Specific routes before the catch-all (same rule as kind/ above).
@router.get("/gate/{slug}/chart")
def gate_chart(slug: str, path: str, request: Request):
    """Serve a gate card's price-comparison chart PNG, inline in the detail view.

    Ben (the fund agent) writes 90-day rebased charts to
    outputs/<date>/charts/<NAME>-90d.png in his repo and points a gate's
    optional `chart_path` field at one. The cockpit renders it via
    <img src="/gate/<slug>/chart?path=<chart_path>">.

    SECURITY — this is the one place a bug = arbitrary file disclosure:
      - bearer auth is enforced by the global middleware (same as every route);
      - `github_reader.resolve_chart_path` strictly validates the path is a
        .png inside THIS dept's <repo>/outputs/*/charts/ (no traversal, no
        symlink escape, no cross-dept reach). It returns None on ANY doubt.
    On None we 404 WITHOUT echoing the path or the reason (no oracle).
    """
    if dept_registry.get_department(slug) is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    resolved = github_reader.resolve_chart_path(slug, path)
    if resolved is None:
        # Single opaque outcome for every rejection class (missing, traversal,
        # wrong-extension, cross-dept, symlink-escape) — no disclosure oracle.
        raise HTTPException(404, "Chart not found")
    return FileResponse(
        str(resolved),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


# IMPORTANT: declared BEFORE /gate/{slug}/{gate_id} so "attachment" is never
# matched as a gate_id (same ordering rule as "kind" and "chart" above).
@router.get("/gate/{slug}/attachment")
def gate_attachment(slug: str, path: str, request: Request):
    """Serve a general gate attachment (image or file) inline or as download.

    Agents attach files to decision gates via the gate YAML `attachments:` list.
    Supported: images (.png .jpg .jpeg .gif .svg .webp) and documents (.pdf
    .csv .txt .md). Each is served with a tight CSP header so an SVG (which
    can carry embedded script) cannot execute in the cockpit origin.

    SECURITY — same model as /gate/{slug}/chart:
      - bearer auth enforced globally by middleware;
      - `github_reader.resolve_attachment_path` strictly validates the path is
        inside THIS dept's repo, under one of the known fixed attachment-root
        shapes (outputs/*/attachments/ or queues/gates/assets/, #666),
        extension is in the allowlist, no traversal, no symlink escape, no
        cross-dept reach. Returns None on ANY doubt.
    None → opaque 404 without echoing path or reason (no oracle).
    """
    if dept_registry.get_department(slug) is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    resolved = github_reader.resolve_attachment_path(slug, path)
    if resolved is None:
        # Single opaque outcome for all rejection classes — no disclosure oracle.
        raise HTTPException(404, "Attachment not found")
    media_type = github_reader.attachment_media_type(resolved)
    # CSP is mandatory for SVG (SVGs can contain <script>). We apply it to ALL
    # attachment responses — harmless for images/PDFs, essential for SVG.
    # `default-src 'none'` blocks script, fetch, and frame; `style-src
    # 'unsafe-inline'` allows the SVG to render its own style attributes.
    # X-Content-Type-Options: nosniff prevents browsers from sniffing the MIME
    # type and executing a misidentified SVG as something else.
    headers = {
        "Cache-Control": "private, max-age=300",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(str(resolved), media_type=media_type, headers=headers)


@router.get("/gate/{slug}/{gate_id}", response_class=HTMLResponse)
def gate_card(slug: str, gate_id: str, request: Request):
    if dept_registry.get_department(slug) is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    gate = github_reader.load_gate(slug, gate_id)
    raw = github_reader.load_gate_raw(slug, gate_id)
    if gate is None or raw is None:
        raise HTTPException(404, f"Gate not found: {gate_id}")
    gate = _attach_thesis_rendered(gate)
    gate = _attach_payload_rendered(slug, gate)
    return request.app.state.templates.TemplateResponse(
        "gate_card.html",
        {
            "request": request,
            "slug": slug,
            "gate_id": gate_id,
            "gate": gate,
            "gate_raw": raw,
            "actions": sorted(ALLOWED_ACTIONS),
        },
    )


@router.post("/gate/{slug}/{gate_id}/decide", response_class=HTMLResponse)
def gate_decide(
    slug: str, gate_id: str, request: Request,
    action: str = Form(...), comment: str = Form(""),
):
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(400, f"Invalid action: {action}")
    if dept_registry.get_department(slug) is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    if github_reader.load_gate(slug, gate_id) is None:
        raise HTTPException(404, f"Gate not found: {gate_id}")
    decision = {
        "gate_id": gate_id,
        "action": action,
        "comment": comment or "",
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decided_by": "operator",  # single-operator console
    }
    out_path = github_reader.write_gate_decision(slug, gate_id, decision)
    resp = request.app.state.templates.TemplateResponse(
        "partials/gate_decision_ok.html",
        {
            "request": request,
            "slug": slug,
            "gate_id": gate_id,
            "action": action,
            "out_path": str(out_path),
        },
    )
    # After a TERMINAL decision the gate is hidden server-side, but the operator
    # is still on the now-resolved single-gate page. Send them back to the dept
    # list (htmx HX-Redirect) so the card visibly disappears and the remaining
    # decisions are in view. `modify` is NOT terminal — the gate stays visible
    # "en révision" — so we keep the operator here to read the confirmation
    # instead of redirecting.
    if action in ("approve", "reject", "defer"):
        resp.headers["HX-Redirect"] = f"/dept/{slug}"
    return resp


@router.post("/gate/{slug}/{gate_id}/undo", response_class=HTMLResponse)
def gate_undo(slug: str, gate_id: str, request: Request):
    """Undo a gate decision — delete the un-processed decision file so the gate
    becomes pending again. Refused if the gate was already resolved by the agent
    (resolved/decided_by in the gate YAML means it is too late to undo).

    host=local depts: out of scope (decision committed to GitHub, not on disk here).
    """
    if dept_registry.get_department(slug) is None:
        raise HTTPException(404, f"Unknown dept: {slug}")

    # Guard: if the gate YAML already has resolved/decided_by, the agent has
    # already acted — we must NOT undo at this point.
    gate_doc = github_reader.load_gate_direct(slug, gate_id)
    if gate_doc is not None and (
        gate_doc.get("resolved") or gate_doc.get("decided_by")
    ):
        return HTMLResponse(
            content=(
                '<div style="font-size:13px; color: var(--danger, #c0392b); '
                'padding:6px 0;">Trop tard — déjà traité par l’agent.</div>'
            ),
            status_code=200,
        )

    deleted = github_reader.delete_gate_decision(slug, gate_id)
    if deleted:
        return HTMLResponse(
            content=(
                '<div style="font-size:13px; color: var(--sage); padding:6px 0;">'
                'Décision annulée — la porte est de nouveau en attente.'
                '</div>'
            ),
            status_code=200,
        )
    # Decision file not found (already processed or never existed).
    return HTMLResponse(
        content=(
            '<div style="font-size:13px; color: var(--body-muted); padding:6px 0;">'
            'Aucune décision non traitée à annuler.'
            '</div>'
        ),
        status_code=200,
    )
