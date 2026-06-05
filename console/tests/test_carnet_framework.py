"""
test_carnet_framework.py — the org-framework flowchart on the Carnet de bord.

Joris msg 1183 → 1188 (2026-06-01): a simple flowchart of how the org works
(concierges, departments, layers) — shown INSIDE the Carnet de bord (/health)
page, not on a separate page.

2026-06-04: the static HTML chart was replaced by an interactive React Flow
graph. The structure (hierarchy, 4 moments, two rails, concierges, local
agents) now lives in the GET /health/graph.json payload that the client
renders; the /health HTML carries the graph *container* + the live activity
table. Tests assert against the right surface accordingly.
"""
from __future__ import annotations

from console.services import org_framework


# ─── Service: build() (legacy shape, still used elsewhere) ───────────────

def test_build_returns_four_keys(client):
    fw = org_framework.build()
    assert set(fw) == {"management", "ops", "concierges", "layers"}
    assert len(fw["layers"]) == 4


# ─── build_graph() via the JSON endpoint ─────────────────────────────────
# NOTE: assert through GET /health/graph.json (the `client` fixture), not a
# direct org_framework.build_graph() call. The `app` fixture re-imports the
# console package with READ_FROM_DISK set; a module imported at test top-level
# would read the wrong disk root. The endpoint uses the correctly-configured
# module instance, so it reflects the fixture depts.

def test_graph_endpoint_serves_json(client):
    r = client.get("/health/graph.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert set(data) >= {"nodes", "edges", "layers", "rails"}
    assert len(data["layers"]) == 4


def test_graph_endpoint_requires_auth(client_noauth):
    r = client_noauth.get("/health/graph.json")
    assert r.status_code in (401, 403)


def test_graph_has_principal_and_no_dangling_edges(client):
    g = client.get("/health/graph.json").json()
    ids = {n["id"] for n in g["nodes"]}
    assert "principal" in ids
    for e in g["edges"]:
        assert e["source"] in ids, f"dangling source {e['source']}"
        assert e["target"] in ids, f"dangling target {e['target']}"


def test_graph_includes_live_fixture_dept(client):
    g = client.get("/health/graph.json").json()
    node = next((n for n in g["nodes"] if n["id"] == "dept:fixture"), None)
    assert node is not None, "live fixture dept should be a graph node"
    assert node["href"] == "/dept/fixture"
    assert len(node["layers"]) == 4
    assert node["status"] in {"ok", "warn", "alert", "unknown"}


def test_layers_have_tristate(client):
    """Each layer reports state ok|idle|stale — never-run is idle, not stale."""
    g = client.get("/health/graph.json").json()
    dept_nodes = [n for n in g["nodes"] if n["kind"] in ("ops", "mgmt")]
    assert dept_nodes, "expected at least one dept node"
    for n in dept_nodes:
        for L in n["layers"]:
            assert L["state"] in {"ok", "idle", "stale"}
            # idle == never_run; stale implies it ran before
            if L["state"] == "idle":
                assert L["never_run"] is True
            if L["state"] == "stale":
                assert L["never_run"] is False


def test_never_run_layer_does_not_force_alert(client):
    """A dept whose only 'problem' is a never-run layer must NOT be red.

    Regression guard for the 2026-06-04 bug where every dept showed alert
    forever because Phase-1 Layer 3 never runs."""
    g = client.get("/health/graph.json").json()
    for n in [x for x in g["nodes"] if x["kind"] in ("ops", "mgmt")]:
        only_idle_problems = all(
            (L["state"] != "stale") for L in n["layers"]
        )
        if only_idle_problems and n.get("pulse", {}).get("alive"):
            assert n["status"] == "ok", (
                f"{n['id']} has only idle layers + live loop but is {n['status']}"
            )


def test_graph_includes_local_agents_without_telemetry(client):
    g = client.get("/health/graph.json").json()
    locals_ = [n for n in g["nodes"] if n["kind"] == "local"]
    assert locals_, "Mac-local agents must be drawn (Notion wishlist)"
    assert all(n.get("telemetry") is False for n in locals_)
    assert {"rick", "miranda"} <= {n["id"].split(":", 1)[1] for n in locals_}


def test_graph_two_rails(client):
    g = client.get("/health/graph.json").json()
    assert {"engine", "net"} <= {r["id"] for r in g["rails"]}


def test_edges_carry_relation_metadata(client):
    """Clickable edges expose the 'log visuel' relation for the panel.

    Parent↔child links are bidirectional (one edge, no permanent label):
    relation carries down (directives) + up (KPIs) blocks. Concierge I/O
    edges carry a flat relation. No edge carries a permanent label."""
    g = client.get("/health/graph.json").json()
    rel_edges = [e for e in g["edges"] if e.get("relation")]
    assert rel_edges, "edges must carry relation metadata for the click panel"
    # declutter: no permanent text labels on any edge
    assert all(not e.get("label") for e in g["edges"]), "edges must have no labels"

    links = [e for e in rel_edges if e["kind"] == "link"]
    assert links, "expected bidirectional parent↔child link edges"
    for e in links:
        r = e["relation"]
        assert {"summary", "down", "up"} <= set(r)
        assert "queues/management" in r["down"]["writes"]
        assert "outputs/" in r["up"]["writes"]


# ─── Layer-detail click-through endpoint ─────────────────────────────────

def test_layer_detail_endpoint_shape(client):
    r = client.get("/health/layer/fixture/1.json")
    assert r.status_code == 200
    d = r.json()
    assert set(d) >= {"dept", "layer", "never_run", "last_iso",
                      "age_human", "summary", "artifacts"}
    assert d["dept"] == "fixture" and d["layer"] == 1
    assert isinstance(d["artifacts"], list)


def test_layer_detail_rejects_bad_layer(client):
    assert client.get("/health/layer/fixture/9.json").status_code == 400


def test_layer_detail_requires_auth(client_noauth):
    r = client_noauth.get("/health/layer/fixture/1.json")
    assert r.status_code in (401, 403)


# ─── Cross-cutting rails (security + wiki-compile) ───────────────────────

def test_graph_has_security_and_wiki_rails(client):
    g = client.get("/health/graph.json").json()
    rails = [n for n in g["nodes"] if n["kind"] == "rail"]
    ids = {n["id"] for n in rails}
    assert {"rail:security", "rail:wiki"} <= ids
    for n in rails:
        assert n["tier"] == -1
        assert n["rail"] in ("left", "right")
        assert n["status"] in {"ok", "warn", "unknown"}


# ─── Concierge I/O + authorisations ──────────────────────────────────────

def test_concierge_nodes_carry_authz_when_present(client):
    """If concierges exist, they expose authz + an I/O edge to principal."""
    g = client.get("/health/graph.json").json()
    concierges = [n for n in g["nodes"] if n["kind"] == "concierge"]
    if not concierges:
        return  # no /home/claude/agents in the test env — acceptable
    for c in concierges:
        assert "authz" in c and {"sandbox", "powers", "repos", "loop"} <= set(c["authz"])
    io_edges = [e for e in g["edges"] if e["kind"] == "concierge_io"]
    assert io_edges, "each concierge should have an I/O edge to principal"


# ─── On the Carnet de bord page ──────────────────────────────────────────

def test_carnet_hosts_the_graph_container(client):
    """The page carries the React Flow mount + fetches the graph JSON."""
    r = client.get("/health")
    body = r.text
    assert r.status_code == 200
    assert 'id="org-flow"' in body
    assert "/health/graph.json" in body
    # CDN React Flow bundle is referenced.
    assert "reactflow" in body


def test_carnet_graph_degrades_without_js(client):
    """A <noscript> fallback points readers to the live table."""
    r = client.get("/health")
    assert "<noscript>" in r.text


def test_no_separate_framework_page(client):
    """The standalone /framework page was removed — it lives on /health now."""
    r = client.get("/framework")
    assert r.status_code == 404
    # And no nav tab points to it.
    home = client.get("/")
    assert 'href="/framework"' not in home.text


def test_carnet_still_shows_live_activity(client):
    """Regression: the live-activity section + footer stay on the page."""
    r = client.get("/health")
    body = r.text
    assert "Activité en direct" in body
    assert "en direct" in body.lower() or "source vivante" in body.lower()
