"""
test_gate_chart.py — gate-card chart embed feature.

Ben (the fund agent) writes 90-day price-comparison PNGs and points a gate
card's optional `chart_path` field at one. The cockpit must render that chart
inline in the gate detail view AND serve the PNG over an authenticated,
path-traversal-proof route.

Data contract (fixed, owned by Ben):
  chart_path: outputs/<YYYY-MM-DD>/charts/<TICKER>-vs-<P1>-<P2>-90d.png
  — relative to the dept repo root, always a .png, dark-theme matplotlib.

The route is /gate/<slug>/chart?path=<relative>. It MUST:
  1. require the same bearer auth as every other console route,
  2. strictly validate the path is inside <repo_root>/outputs/*/charts/ and
     ends in .png (reject .., absolute paths, anything outside charts/, non-png),
  3. serve only from the requested dept's own repo root (dept-scoped),
  4. return the PNG bytes with image/png content-type.

The detail template must render <img> when chart_path is set + the file
exists, and be a graceful no-op (no img, no broken image, no error) when
chart_path is absent OR the referenced file is missing.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# A minimal valid 1x1 PNG (the smallest legal PNG). Enough to assert bytes +
# content-type without pulling in Pillow/matplotlib in the test env.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000100ffff03000006000557bfabd40000"
    "000049454e44ae426082"
)


def _write_chart(repo_root: Path, date: str, fname: str, data: bytes = _PNG_BYTES) -> Path:
    """Drop a PNG into outputs/<date>/charts/<fname> under a dept repo root."""
    charts = repo_root / "outputs" / date / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    p = charts / fname
    p.write_bytes(data)
    return p


def _add_chart_gate(repo_root: Path, gate_id: str, chart_path: str | None) -> None:
    """Write a trade_proposal gate card, optionally with a chart_path field."""
    gates = repo_root / "queues" / "gates"
    gates.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": gate_id,
        "kind": "trade_proposal",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "ticker": "THK",
        "side": "buy",
        "proposed_qty": 10,
        "summary": "Thesis: machine-vision tailwind.",
        "actions": ["approve", "reject", "modify", "defer"],
    }
    if chart_path is not None:
        doc["chart_path"] = chart_path
    (gates / f"{gate_id}.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False), encoding="utf-8"
    )


# fixture_root from conftest builds bubble-ops-fixture (Live). We add chart
# data + a chart gate to it for these tests.
@pytest.fixture
def ben_root(fixture_root: Path) -> Path:
    return fixture_root / "bubble-ops-fixture"


# ── 1. chart route serves a valid PNG when path is within charts dir ──────

def test_chart_route_serves_valid_png(client, ben_root):
    _write_chart(ben_root, "2026-06-19", "THK-vs-6301-ROBO-90d.png")
    rel = "outputs/2026-06-19/charts/THK-vs-6301-ROBO-90d.png"
    r = client.get(f"/gate/fixture/chart?path={rel}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == _PNG_BYTES


# ── 2. path-traversal REJECTED in every form ──────────────────────────────

@pytest.mark.parametrize("bad", [
    "../../../../etc/passwd",
    "outputs/2026-06-19/charts/../../../../etc/passwd",
    "/etc/passwd",
    "/home/claude/agents/bubble-ops-fixture/dept.yaml",
    "outputs/2026-06-19/charts/../../dept.yaml",        # escapes charts/
    "dept.yaml",                                         # outside outputs/
    "outputs/2026-06-19/notcharts/x.png",                # not a charts/ dir
    "outputs/2026-06-19/charts/evil.txt",                # not a .png
    "outputs/2026-06-19/charts/evil.png.txt",            # not a .png
    "outputs/2026-06-19/charts/passwd",                  # no extension
])
def test_chart_route_rejects_traversal(client, ben_root, bad):
    # Make a legit chart exist so a bypass would actually serve *something*.
    _write_chart(ben_root, "2026-06-19", "THK-vs-6301-ROBO-90d.png")
    r = client.get("/gate/fixture/chart", params={"path": bad})
    assert r.status_code in (400, 403, 404), (
        f"traversal {bad!r} returned {r.status_code} — must be rejected"
    )
    # Never leak file contents
    assert "root:" not in r.text  # /etc/passwd marker
    assert "department:" not in r.text  # dept.yaml marker


def test_chart_route_rejects_symlink_escape(client, ben_root, tmp_path):
    """A symlink inside charts/ pointing outside the repo must not be served."""
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"SECRET-OUTSIDE-REPO")
    charts = ben_root / "outputs" / "2026-06-19" / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    link = charts / "escape.png"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    r = client.get(
        "/gate/fixture/chart",
        params={"path": "outputs/2026-06-19/charts/escape.png"},
    )
    assert r.status_code in (400, 403, 404)
    assert b"SECRET-OUTSIDE-REPO" not in r.content


# ── 3. auth required ──────────────────────────────────────────────────────

def test_chart_route_requires_auth(client_noauth, ben_root):
    _write_chart(ben_root, "2026-06-19", "THK-vs-6301-ROBO-90d.png")
    rel = "outputs/2026-06-19/charts/THK-vs-6301-ROBO-90d.png"
    r = client_noauth.get(f"/gate/fixture/chart?path={rel}")
    assert r.status_code in (401, 403), (
        f"unauthenticated chart fetch returned {r.status_code} — must be 401/403"
    )
    assert r.content != _PNG_BYTES


# ── 4. template renders <img> with chart_path, graceful no-op without ─────

def test_gate_detail_renders_img_when_chart_present(client, ben_root):
    rel = "outputs/2026-06-19/charts/THK-vs-6301-ROBO-90d.png"
    _write_chart(ben_root, "2026-06-19", "THK-vs-6301-ROBO-90d.png")
    _add_chart_gate(ben_root, "trade-chart-1", rel)
    r = client.get("/gate/fixture/trade-chart-1")
    assert r.status_code == 200
    assert "/gate/fixture/chart?path=" in r.text
    assert rel in r.text
    assert "<img" in r.text.lower()


def test_gate_detail_no_img_when_chart_absent(client, ben_root):
    _add_chart_gate(ben_root, "trade-nochart-1", None)
    r = client.get("/gate/fixture/trade-chart-absent" if False else "/gate/fixture/trade-nochart-1")
    assert r.status_code == 200
    # No chart-serving link, no broken-image markup, no error
    assert "/gate/fixture/chart?path=" not in r.text
    assert "chart-embed" not in r.text or "chart_path" not in r.text


def test_gate_detail_graceful_when_chart_file_missing(client, ben_root):
    """chart_path set but the PNG does not exist on disk → detail still renders,
    no error. (The <img> may be emitted; the route 404s, browser shows nothing.
    What must NOT happen is a 500 on the detail page.)"""
    rel = "outputs/2026-06-19/charts/DOES-NOT-EXIST-90d.png"
    _add_chart_gate(ben_root, "trade-missing-1", rel)
    r = client.get("/gate/fixture/trade-missing-1")
    assert r.status_code == 200  # detail view must not error


# ── 5. dept-scoping: ben's chart route serves only ben's repo ─────────────

def test_chart_route_is_dept_scoped(client, fixture_root: Path):
    """A path valid in dept A must not resolve through dept B's route.
    Put the chart only under fixture's repo; request it via miranda's slug."""
    fixture_repo = fixture_root / "bubble-ops-fixture"
    _write_chart(fixture_repo, "2026-06-19", "THK-vs-6301-ROBO-90d.png")
    rel = "outputs/2026-06-19/charts/THK-vs-6301-ROBO-90d.png"
    # miranda has no such file → must not reach across into fixture's repo
    r = client.get(f"/gate/miranda/chart?path={rel}")
    assert r.status_code in (400, 403, 404)
    assert r.content != _PNG_BYTES


def test_chart_route_unknown_dept_404(client):
    r = client.get(
        "/gate/nonexistent-dept/chart",
        params={"path": "outputs/2026-06-19/charts/x.png"},
    )
    assert r.status_code == 404
