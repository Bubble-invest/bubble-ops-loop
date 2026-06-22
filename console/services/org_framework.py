"""
org_framework.py — the organisation framework data for the cockpit.

{{OPERATOR}} msg 1183 → 1188 (2026-06-01): a flowchart of how the org works
(concierges, departments, layers), shown INSIDE the Carnet de bord page (not
a separate page). This service builds the data.

- build()       → legacy dict for any server-rendered consumers.
- build_graph() → nodes/edges/rails for the interactive React Flow chart
                  (2026-06-04); served as JSON by GET /health/graph.json and
                  drawn client-side by partials/_org_flow.html.

Shape from the Notion "bubble-ops-loop — Architecture finale simplifiée" page:
    Principal ({{OPERATOR}}·{{OPERATOR_2}}) → Management dept → Ops depts,  + Concierges beside.
Every department runs the same 4-moment OODA day (the layers).
"""
from __future__ import annotations

from typing import Any, Dict, List

from console.services import concierge_reader, dept_registry, github_reader, morty_reader

# The 4 layers (OODA "moments") every department runs each day. Mirrors
# loop_history.MOMENT_NAMES + the Notion architecture page.
LAYERS = [
    {"num": 1, "name": "Le matin", "ooda": "Data", "what": "rafraîchit les données, lit les directives"},
    {"num": 2, "name": "La recherche", "ooda": "Research", "what": "analyse, prépare les décisions"},
    {"num": 3, "name": "L'exécution", "ooda": "Exec", "what": "agit sur ce qui est validé"},
    {"num": 4, "name": "Le débrief du soir", "ooda": "Risk", "what": "audit, risques, améliorations"},
]

# Mac-local agents from the Notion architecture wishlist (hub §10 / meeting
# 2026-06-03). The console has no telemetry for these yet — they don't phone
# home — so they appear as static nodes flagged `telemetry: false`. A
# follow-up (see STATUS / FOLLOWUP-local-agents-phonehome) will wire their
# heartbeats via the SPEC-015 phone-home pattern, after which build_graph()
# can fold real status in the same way it does for VPS depts.
# NOTE (2026-06-21): Miranda was removed from this list. She is now a LIVE ops
# department (`content`, host:local on {{OPERATOR_2}}'s Mac) registered via dept.yaml, so
# she already renders as a real dept node with live telemetry. Keeping her here
# too made her appear TWICE — once as a live dept, once as a ghost Mac-local
# node. The Mac-local tier is now ONLY for agents with no dept registration at
# all (Rick, Tony-local). A dept that runs on the Mac is flagged via its
# `host: "local"` field (see _dept_node below), not duplicated here.
LOCAL_AGENTS = [
    {"id": "rick", "name": "Rick", "role": "R&D / Dev", "host": "Mac local"},
    {"id": "tony-local", "name": "Tony (local)", "role": "Management — 2e instance", "host": "Mac local"},
]


def _rail_status(timer_unit: str) -> Dict[str, Any]:
    """Last-run + status for a cross-cutting rail, from its systemd timer.

    The console runs on the box, so we can read the timer's LastTriggerUSec.
    Best-effort: if systemctl isn't available (tests / off-box), return a
    neutral 'unknown' with telemetry False — the rail still renders, honestly
    flagged as un-instrumented (same pattern as the Mac-local agents)."""
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "show", timer_unit,
             "-p", "LastTriggerUSec", "-p", "Result"],
            capture_output=True, text=True, timeout=4,
        )
        props = dict(
            line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
        )
        last = props.get("LastTriggerUSec", "").strip()
        if not last or last in ("0", "n/a"):
            return {"status": "warn", "telemetry": True, "last_human": "jamais déclenché"}
        return {"status": "ok", "telemetry": True, "last_human": last}
    except Exception:
        return {"status": "unknown", "telemetry": False, "last_human": ""}


def _level(slug: str) -> str:
    """Hierarchy level of a dept from its dept.yaml (management|ops)."""
    y = github_reader.load_dept_yaml(slug)
    if isinstance(y, dict):
        lvl = (y.get("hierarchy", {}) or {}).get("level") \
            or (y.get("department", {}) or {}).get("level")
        if lvl == "management":
            return "management"
    return "ops"


def build() -> Dict[str, Any]:
    """Return {management, ops, concierges, layers} for the framework chart.

    Department boxes are filled live from the registry so the chart always
    reflects who actually exists; the structure itself is the fixed framework.
    """
    live = dept_registry.live_departments()
    management = [d for d in live if _level(d.slug) == "management"]
    ops = [d for d in live if _level(d.slug) != "management"]
    concierges = [concierge_reader.get_concierge(n) for n in concierge_reader.CONCIERGES]
    concierges = [c for c in concierges if c is not None]
    return {
        "management": management,
        "ops": ops,
        "concierges": concierges,
        "layers": LAYERS,
    }


# Pulse-age thresholds for tiered status. Loop ticks every ~20 min; silent
# under WARN_SEC is amber (might just be between ticks / briefly parked),
# silent over WARN_SEC is red (genuinely dead).
_PULSE_WARN_SEC = 90 * 60        # 90 min — matches morty_reader._STALE_PULSE_SEC
_PULSE_ALERT_SEC = 24 * 3600     # 24 h — clearly dead, not just a gap


def _dept_status(pulse_alive: bool, pulse_age_sec, layer_rows: List[Any]) -> str:
    """Roll a dept's live signals into ONE tiered status for the node.

    Grading (so red keeps meaning "look now", not "everything"):
    - "alert" : loop silent > 24 h, OR a layer that HAS run before is now
                stale (regressed). These are real problems.
    - "warn"  : loop silent but recently (< 24 h) — amber, keep an eye.
    - "ok"    : loop alive (ticked within the warn window) and no regressed layer.

    A layer that has simply NEVER run (never_run=True) is NOT counted as a
    problem here — in Phase 1 (human-approved) Layer 3 legitimately never
    fires until a gate is approved. Never-run renders as a neutral idle badge,
    not red. (Bug surfaced by the flowchart 2026-06-04: every dept showed red
    L3 forever because never-run was conflated with stale.)
    """
    # A "regressed" layer ran at least once and has since gone stale.
    regressed = any((not r.never_run) and r.is_stale for r in layer_rows)
    if regressed:
        return "alert"
    if pulse_alive:
        return "ok"
    # loop silent: grade by how long
    if pulse_age_sec is not None and pulse_age_sec > _PULSE_ALERT_SEC:
        return "alert"
    return "warn"


def build_graph() -> Dict[str, Any]:
    """Build a semantic node/edge graph of the whole org for the cockpit.

    Returns plain JSON-able dicts (NOT React Flow internals): the client
    template owns layout + rendering. Each node carries `kind`, `status`,
    and a `href` (when clickable); each edge carries `kind` + `label`.

    Live status comes from the same source as the Carnet table: per-dept
    loop pulse + per-(dept×layer) freshness via morty_reader. The structure
    (Principal → Management → Ops, concierges aside, Mac-local tier, the
    4-layer loop, the two rails) is the fixed framework from the Notion
    "bubble-ops-loop — Architecture finale simplifiée" page.
    """
    live = dept_registry.live_departments()
    management = [d for d in live if _level(d.slug) == "management"]
    ops = [d for d in live if _level(d.slug) != "management"]
    slugs = [d.slug for d in live]

    pulse = morty_reader.loop_pulse(slugs)
    rows = morty_reader.per_dept_layer_heartbeats(slugs)
    rows_by_dept: Dict[str, List[Any]] = {}
    for r in rows:
        rows_by_dept.setdefault(r.dept, []).append(r)

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    # ── Principal ──────────────────────────────────────────────────────
    nodes.append({
        "id": "principal", "kind": "principal", "tier": 0,
        "title": "{{OPERATOR}} · {{OPERATOR_2}}", "role": "Principal",
        "note": "gates critiques · mandats · capital", "status": "ok",
    })

    def _dept_node(d, kind: str, tier: int) -> Dict[str, Any]:
        p = pulse.get(d.slug)
        alive = bool(p and p.alive)
        age_sec = p.age_sec if p else None
        lrows = rows_by_dept.get(d.slug, [])
        return {
            "id": f"dept:{d.slug}", "kind": kind, "tier": tier,
            "title": d.display_name, "role": "Département management" if kind == "mgmt" else None,
            "slug": d.slug, "href": f"/dept/{d.slug}",
            # "vps" | "local" — hybrid Mac-local depts (e.g. Miranda/content on
            # {{OPERATOR_2}}'s Mac) carry host:"local" so the frontend can badge them.
            "host": getattr(d, "host", "vps"),
            "status": _dept_status(alive, age_sec, lrows),
            "pulse": {
                "alive": alive,
                "age_human": (p.age_human if p else "") or "",
            },
            "layers": [
                {"num": r.layer,
                 # three states: ok | idle (never run) | stale (ran, now overdue)
                 "state": ("idle" if r.never_run
                           else "stale" if r.is_stale else "ok"),
                 "stale": r.is_stale, "never_run": r.never_run,
                 "age_human": r.age_human, "last": r.last_success_iso}
                for r in sorted(lrows, key=lambda x: x.layer)
            ],
        }

    # ── Management tier ────────────────────────────────────────────────
    # ONE bidirectional edge per parent↔child relationship (directives DOWN +
    # KPIs UP collapsed into a single line, no permanent label — the two-way
    # relation + which-file/where-read shows in the click panel). This halves
    # the edge count and removes ~16 floating labels that cluttered the chart.
    mgmt_ids: List[str] = []

    def _link_edge(parent, child, child_slug):
        return {"id": f"e:{parent}-{child}", "source": parent, "target": child,
                "kind": "link", "label": "",
                "relation": {
                    "summary": "directives ↓ (PR) · KPIs ↑ (Layer 4)",
                    "down": {
                        "what": "directives (PR prioritaire)",
                        "writes": f"bubble-ops-{child_slug}/queues/management/directive-*.yaml",
                        "read_at": "enfant — Layer 1 (Data Refresh)",
                        "note": "l'enfant reste propriétaire de son exécution ; "
                                "le parent ne peut ni exécuter ni contourner les gates.",
                    },
                    "up": {
                        "what": "KPIs + export management (Layer 4)",
                        "writes": f"bubble-ops-{child_slug}/outputs/<date>/4/risk-kpis.yaml "
                                  f"+ management-export.yaml",
                        "read_at": "parent (lecture seule)",
                        "note": "le parent voit les outputs de l'enfant, sans y écrire.",
                    },
                }}

    for d in management:
        n = _dept_node(d, "mgmt", 1)
        nodes.append(n)
        mgmt_ids.append(n["id"])
        edges.append(_link_edge("principal", n["id"], d.slug))

    # ── Ops tier ───────────────────────────────────────────────────────
    parent_ids = mgmt_ids or ["principal"]
    dept_ids: List[str] = list(mgmt_ids)   # every dept a concierge can act on
    for d in ops:
        n = _dept_node(d, "ops", 2)
        nodes.append(n)
        dept_ids.append(n["id"])
        # link to the (single) management parent — avoids a fan of duplicate
        # edges when there are multiple parents (there is one manager today).
        edges.append(_link_edge(parent_ids[0], n["id"], d.slug))

    # ── Concierges — CROSS-CUTTING, above the org (not ops peers). ─────────
    # Grounded in real code/permissions observed on the VPS (2026-06-05):
    #   • run as the `claude` user → passwordless sudo to start/stop/restart
    #     EVERY agent's loop (ops-loop-*, claude-agent-*, telegram-watchdog-*,
    #     bubble-*, cloud-wiki-*) + journalctl. (/etc/sudoers.d)
    #   • all dept repos live under /home/claude/agents/bubble-ops-* owned by
    #     the same `claude` user → read/write on every dept's working tree
    #     (the "sandbox commune"); share the git credential helper.
    #   • no per-agent restriction file (claudette settings.json is empty) →
    #     scope is the broad claude-user set; narrower than Rick-local only by
    #     convention, NOT a hard OS sandbox.
    # So they get ONE node above Tony with an INBOUND link to every dept,
    # representing "can act on any dept" (lifecycle control + shared FS).
    CONCIERGE_AUTHZ = {
        "run_as": "utilisateur `claude` (sandbox commune Morty + Claudette)",
        "lifecycle": "sudo NOPASSWD : start/stop/restart de TOUT loop d'agent "
                     "(ops-loop-*, claude-agent-*, telegram-watchdog-*, bubble-*, "
                     "cloud-wiki-*) + journalctl",
        "filesystem": "lecture/écriture sur tous les repos /home/claude/agents/"
                      "bubble-ops-* (mêmes droits `claude`)",
        "limits": "pas de boucle /loop autonome (réactif) ; restriction vs "
                  "Rick-local par convention, pas par sandbox OS ; secrets en "
                  "RAM jamais lus.",
        "evidence": "observé sur cx33 : /etc/sudoers.d + ownership des repos "
                    "(2026-06-05).",
    }
    for c in (concierge_reader.get_concierge(name) for name in concierge_reader.CONCIERGES):
        if c is None:
            continue
        svc = (getattr(c, "metadata", {}) or {}).get("service_status", "")
        last_used = getattr(c, "last_activity_iso", None) or ""
        cid = f"concierge:{c.name}"
        nodes.append({
            "id": cid, "kind": "concierge", "tier": 0,  # cross-cutting, above Tony
            "title": c.name.capitalize(), "role": "Concierge",
            "note": "transversal · peut agir sur tout dept",
            "href": f"/concierge/{c.name}",
            "status": "ok" if svc in ("", "active", "running") else "warn",
            "last_human": last_used,
            "authz": CONCIERGE_AUTHZ,
            "last_used": last_used,
        })
        # INBOUND link to EVERY dept — the concierge can act on any of them.
        for did in dept_ids:
            edges.append({
                "id": f"e:{cid}-{did}", "source": cid, "target": did,
                "kind": "concierge_io", "label": "",
                "relation": {
                    "direction": f"{c.name.capitalize()} → {did} (transversal, à la demande)",
                    "writes": "contrôle du loop (start/stop/restart) + repo en lecture/écriture",
                    "read_at": "journalctl + working tree du dept",
                    "note": f"dernière activité : {last_used or 'inconnue'}. "
                            "Concierge transversal : agit sur n'importe quel dept "
                            "(sudo lifecycle + FS commun), réactif, pas de /loop.",
                }})

    # ── Mac-local agents (static — no telemetry yet) ───────────────────
    for a in LOCAL_AGENTS:
        nodes.append({
            "id": f"local:{a['id']}", "kind": "local", "tier": 3,
            "title": a["name"], "role": a["role"], "note": a["host"],
            "status": "unknown", "telemetry": False,
        })

    # ── GitHub repos (only) on the main graph, with links. ────────────────
    # Grounded in each dept's real remotes (via dataflow.dept_dataflow): the
    # dept's own bubble-ops-<slug> repo (+ its vault when it has one), plus a
    # single shared-wiki node every dept reads. Each dept → its repo(s).
    from console.services import dataflow as _dataflow
    wiki_id = "repo:shared-wiki"
    wiki_seen = False
    for d in management + ops:
        did = f"dept:{d.slug}"
        try:
            df = _dataflow.dept_dataflow(d.slug)
        except Exception:
            df = {"repos": []}
        for rp in df.get("repos", []):
            if rp["kind"] == "wiki":
                wiki_seen = True
                # link this dept to the shared wiki (read); node added once below
                edges.append({
                    "id": f"e:{did}-{wiki_id}", "source": did, "target": wiki_id,
                    "kind": "repo_link", "label": "", "access": "read",
                    "relation": {"direction": f"{d.display_name} lit le wiki partagé",
                                 "writes": "shared-wiki (lecture)", "read_at": "tous les layers",
                                 "note": rp.get("role", "")}})
                continue
            rid = f"repo:{rp['id']}"
            nodes.append({
                "id": rid, "kind": "repo", "repo_kind": rp["kind"],
                "title": rp["id"], "role": "Repo GitHub" if rp["kind"] == "repo" else "Vault GitHub",
                "note": rp.get("role", ""), "status": "ok",
                "href": f"https://github.com/Bubble-invest/{rp['id']}",
                "owner_dept": did,
            })
            edges.append({
                "id": f"e:{did}-{rid}", "source": did, "target": rid,
                "kind": "repo_link", "label": "", "access": "rw",
                "relation": {"direction": f"{d.display_name} ↔ {rp['id']} (lecture/écriture)",
                             "writes": "queues/ · outputs/ · layers/ · vault",
                             "read_at": "à chaque layer", "note": rp.get("role", "")}})
    if wiki_seen:
        nodes.append({
            "id": wiki_id, "kind": "repo", "repo_kind": "wiki",
            "title": "shared-wiki", "role": "Wiki partagé (GitHub)",
            "note": "mémoire partagée — lue par tous les agents",
            "status": "ok", "href": "https://github.com/vdk888/bubble-shared-wiki",
        })

    # ── Two cross-cutting rails (belt + suspenders) that ENCLOSE the whole
    # org: Sécurité and Wiki-compile (meeting wishlist "deux grandes flèches").
    # They run org-wide (not per-dept), so they frame the graph rather than
    # connect to one node. Last-run comes from their systemd timers.
    # Both ENCIRCLE the org (concentric frames) — they genuinely sweep every
    # agent (grounded on cx33 2026-06-05): security = morty-agentic-audit +
    # security-audit transcript-leak scan over all agents (root-owned logs);
    # wiki-compile = reads ALL agents' sessions (-home-claude-agents-* + the
    # two Macs) into the shared-wiki vault, daily, pushed to GitHub.
    nodes.append({
        "id": "rail:security", "kind": "rail", "rail_key": "security",
        "title": "Sécurité", "role": "Ceinture transversale",
        "note": "audit agentique + scan fuite-transcript sur TOUS les agents (quotidien)",
        "scope": "morty-agentic-audit.service + security-audit.sh ; logs root-owned",
        **_rail_status("morty-agentic-audit.timer"),
    })
    nodes.append({
        "id": "rail:wiki", "kind": "rail", "rail_key": "wiki",
        "title": "Wiki-compile", "role": "Bretelle transversale",
        "note": "compile les sessions de TOUS les agents → vault partagé (quotidien)",
        "scope": "cloud-wiki-compile.sh lit -home-claude-agents-* + Mac {{OPERATOR}}/{{OPERATOR_2}} → shared-wiki, push GitHub",
        **_rail_status("cloud-wiki-compile-compile.timer"),
    })

    return {
        "nodes": nodes,
        "edges": edges,
        "layers": LAYERS,
        "rails": [
            {"id": "engine", "kind": "engine", "title": "Moteur principal",
             "body": "/loop en continu — un par département. À chaque tick : "
                     "regarde les files, agit, ou bat silencieusement du cœur."},
            {"id": "net", "kind": "net", "title": "Filet de sécurité",
             "body": "Sauvegarde planifiée (08:00 + 14:00) + routines cloud "
                     "quotidiennes. Si une boucle meurt, le filet exécute un "
                     "tick à sa place."},
        ],
    }
