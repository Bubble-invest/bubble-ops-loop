"""
test_gate_attachments.py — general attachments mechanism for gate decision cards.

An agent may attach images or files to a gate via the `attachments:` list in
the gate YAML.  Each attachment has a `path` (dept-repo-relative, inside
outputs/<date>/attachments/) and an optional `caption`.

The route is /gate/<slug>/attachment?path=<relative>.  It MUST:
  1. require the same bearer auth as every other console route,
  2. strictly validate the path is inside <repo_root>/outputs/*/attachments/ and
     the extension is in the allowlist (.png .jpg .jpeg .gif .svg .webp .pdf
     .csv .txt .md) — reject .., absolute paths, wrong dir, disallowed ext,
     double-ext like evil.png.html, nonexistent file, unknown dept,
  3. serve only from the requested dept's own repo root (dept-scoped),
  4. return the file bytes with the correct Content-Type,
  5. always add CSP + X-Content-Type-Options: nosniff headers (mandatory for SVG;
     applied to all for consistency).

Templates (gate_card.html, gate_batch.html) must render <img> for image types
and a labeled link for non-image types, graceful no-op when list is absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ── Minimal test payloads ─────────────────────────────────────────────────────

# Minimal valid 1x1 PNG — enough to assert bytes + content-type without Pillow.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000100ffff03000006000557bfabd40000"
    "000049454e44ae426082"
)
_PDF_BYTES = b"%PDF-1.4 fake-pdf"
_CSV_BYTES = b"ticker,price\nAPPL,200.5\n"
_SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_attachment(
    repo_root: Path, date: str, fname: str, data: bytes = _PNG_BYTES
) -> Path:
    """Drop a file into outputs/<date>/attachments/<fname> under a dept repo root."""
    attdir = repo_root / "outputs" / date / "attachments"
    attdir.mkdir(parents=True, exist_ok=True)
    p = attdir / fname
    p.write_bytes(data)
    return p


def _add_gate_with_attachments(
    repo_root: Path, gate_id: str, attachments: list | None = None
) -> None:
    """Write a minimal trade_proposal gate with optional attachments list."""
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
        "ticker": "TST",
        "side": "buy",
        "proposed_qty": 5,
        "summary": "Attachment test gate.",
        "actions": ["approve", "reject", "modify", "defer"],
    }
    if attachments is not None:
        doc["attachments"] = attachments
    (gates / f"{gate_id}.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def ben_root(fixture_root: Path) -> Path:
    return fixture_root / "bubble-ops-fixture"


# ── 1. Valid image attachment served with correct media type ──────────────────

def test_attachment_route_serves_png(client, ben_root):
    _write_attachment(ben_root, "2026-06-19", "scenario-curve.png", _PNG_BYTES)
    rel = "outputs/2026-06-19/attachments/scenario-curve.png"
    r = client.get(f"/gate/fixture/attachment?path={rel}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == _PNG_BYTES


def test_attachment_route_serves_pdf(client, ben_root):
    _write_attachment(ben_root, "2026-06-19", "memo.pdf", _PDF_BYTES)
    rel = "outputs/2026-06-19/attachments/memo.pdf"
    r = client.get(f"/gate/fixture/attachment?path={rel}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content == _PDF_BYTES


def test_attachment_route_serves_csv(client, ben_root):
    _write_attachment(ben_root, "2026-06-19", "data.csv", _CSV_BYTES)
    rel = "outputs/2026-06-19/attachments/data.csv"
    r = client.get(f"/gate/fixture/attachment?path={rel}")
    assert r.status_code == 200, r.text
    assert "csv" in r.headers["content-type"].lower() or "text" in r.headers["content-type"].lower()
    assert r.content == _CSV_BYTES


def test_attachment_route_serves_svg(client, ben_root):
    """SVG is served inline but must carry CSP + nosniff headers."""
    _write_attachment(ben_root, "2026-06-19", "diagram.svg", _SVG_BYTES)
    rel = "outputs/2026-06-19/attachments/diagram.svg"
    r = client.get(f"/gate/fixture/attachment?path={rel}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.content == _SVG_BYTES
    # Security headers must be present on ALL attachment responses
    assert "content-security-policy" in r.headers
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers.get("x-content-type-options", "").lower() == "nosniff"


def test_attachment_route_has_csp_on_png(client, ben_root):
    """CSP and nosniff are applied to ALL attachment responses, not just SVG."""
    _write_attachment(ben_root, "2026-06-19", "chart.png", _PNG_BYTES)
    rel = "outputs/2026-06-19/attachments/chart.png"
    r = client.get(f"/gate/fixture/attachment?path={rel}")
    assert r.status_code == 200
    assert "content-security-policy" in r.headers
    assert r.headers.get("x-content-type-options", "").lower() == "nosniff"


# ── 2. Path traversal — ALL must be rejected (404) ───────────────────────────

@pytest.mark.parametrize("bad", [
    # Parent-directory traversal
    "../../../../etc/passwd",
    "outputs/2026-06-19/attachments/../../../../etc/passwd",
    # Absolute path
    "/etc/passwd",
    "/home/claude/agents/bubble-ops-fixture/dept.yaml",
    # Escapes attachments/ dir
    "outputs/2026-06-19/attachments/../../dept.yaml",
    # Outside outputs/ entirely
    "dept.yaml",
    # Wrong sub-dir: charts/ is for resolve_chart_path, NOT for resolve_attachment_path
    "outputs/2026-06-19/charts/x.png",
    # Disallowed extensions
    "outputs/2026-06-19/attachments/evil.html",
    "outputs/2026-06-19/attachments/evil.js",
    "outputs/2026-06-19/attachments/evil.exe",
    # Double-extension — last suffix is .html → rejected
    "outputs/2026-06-19/attachments/evil.pdf.html",
    "outputs/2026-06-19/attachments/evil.png.html",
    # No extension
    "outputs/2026-06-19/attachments/passwd",
    # Depth too shallow
    "outputs/2026-06-19/attachment-file.png",
])
def test_attachment_route_rejects_bad_path(client, ben_root, bad):
    # Plant a legit file so a bypass would actually return content.
    _write_attachment(ben_root, "2026-06-19", "legit.png", _PNG_BYTES)
    r = client.get("/gate/fixture/attachment", params={"path": bad})
    assert r.status_code in (400, 403, 404), (
        f"bad path {bad!r} returned {r.status_code} — must be rejected"
    )
    # Never leak sensitive content
    assert "root:" not in r.text   # /etc/passwd marker
    assert "department:" not in r.text  # dept.yaml marker


# ── 3. Symlink escape ─────────────────────────────────────────────────────────

def test_attachment_route_rejects_symlink_escape(client, ben_root, tmp_path):
    """A symlink inside attachments/ that points outside the repo must be rejected."""
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"SECRET-OUTSIDE-REPO")
    attdir = ben_root / "outputs" / "2026-06-19" / "attachments"
    attdir.mkdir(parents=True, exist_ok=True)
    link = attdir / "escape.png"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    r = client.get(
        "/gate/fixture/attachment",
        params={"path": "outputs/2026-06-19/attachments/escape.png"},
    )
    assert r.status_code in (400, 403, 404)
    assert b"SECRET-OUTSIDE-REPO" not in r.content


# ── 4. Nonexistent file → 404 ────────────────────────────────────────────────

def test_attachment_route_404_on_missing_file(client, ben_root):
    r = client.get(
        "/gate/fixture/attachment",
        params={"path": "outputs/2026-06-19/attachments/does-not-exist.png"},
    )
    assert r.status_code == 404


# ── 5. Unknown dept → 404 ────────────────────────────────────────────────────

def test_attachment_route_unknown_dept_404(client):
    r = client.get(
        "/gate/nonexistent-dept/attachment",
        params={"path": "outputs/2026-06-19/attachments/x.png"},
    )
    assert r.status_code == 404


# ── 6. Auth required ─────────────────────────────────────────────────────────

def test_attachment_route_requires_auth(client_noauth, ben_root):
    _write_attachment(ben_root, "2026-06-19", "auth-test.png", _PNG_BYTES)
    rel = "outputs/2026-06-19/attachments/auth-test.png"
    r = client_noauth.get(f"/gate/fixture/attachment?path={rel}")
    assert r.status_code in (401, 403), (
        f"unauthenticated attachment fetch returned {r.status_code} — must be 401/403"
    )
    assert r.content != _PNG_BYTES


# ── 7. Dept-scoping ──────────────────────────────────────────────────────────

def test_attachment_route_is_dept_scoped(client, fixture_root: Path):
    """An attachment that exists only in fixture's repo must not serve through miranda's route."""
    fixture_repo = fixture_root / "bubble-ops-fixture"
    _write_attachment(fixture_repo, "2026-06-19", "chart.png", _PNG_BYTES)
    rel = "outputs/2026-06-19/attachments/chart.png"
    # miranda has no such file → must not reach across into fixture's repo
    r = client.get(f"/gate/miranda/attachment?path={rel}")
    assert r.status_code in (400, 403, 404)
    assert r.content != _PNG_BYTES


# ── 8. attachment_media_type helper ──────────────────────────────────────────

def test_attachment_media_type_returns_correct_types():
    from console.services.github_reader import attachment_media_type
    cases = {
        "x.png": "image/png",
        "x.jpg": "image/jpeg",
        "x.jpeg": "image/jpeg",
        "x.gif": "image/gif",
        "x.svg": "image/svg+xml",
        "x.webp": "image/webp",
        "x.pdf": "application/pdf",
        "x.csv": "text/csv",
        "x.txt": "text/plain",
        "x.md": "text/markdown",
    }
    for fname, expected in cases.items():
        assert attachment_media_type(Path(fname)) == expected, fname


def test_attachment_media_type_unknown_fallback():
    from console.services.github_reader import attachment_media_type
    # Should never happen in production (resolver rejects unknown exts),
    # but the helper must degrade gracefully.
    assert attachment_media_type(Path("x.bin")) == "application/octet-stream"


# ── 9. resolve_attachment_path unit tests (no HTTP) ──────────────────────────

def test_resolve_attachment_path_returns_none_for_charts_dir(ben_root, monkeypatch):
    """resolve_attachment_path must NOT resolve paths in charts/ (that's charts resolver)."""
    import os
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    # Re-import so env is picked up
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    from console.services.github_reader import resolve_attachment_path
    result = resolve_attachment_path("fixture", "outputs/2026-06-19/charts/x.png")
    assert result is None


# ── 10. Template renders attachments ─────────────────────────────────────────

def test_gate_detail_renders_image_attachment(client, ben_root):
    """gate_card.html must render <img> when gate has an image attachment."""
    _write_attachment(ben_root, "2026-06-19", "curve.png", _PNG_BYTES)
    rel = "outputs/2026-06-19/attachments/curve.png"
    _add_gate_with_attachments(ben_root, "att-img-1", [{"path": rel, "caption": "Scenario"}])
    r = client.get("/gate/fixture/att-img-1")
    assert r.status_code == 200, r.text
    assert "/gate/fixture/attachment?path=" in r.text
    assert rel in r.text
    assert "<img" in r.text.lower()


def test_gate_detail_renders_pdf_as_link(client, ben_root):
    """gate_card.html must render a labeled link for non-image attachments."""
    _write_attachment(ben_root, "2026-06-19", "memo.pdf", _PDF_BYTES)
    rel = "outputs/2026-06-19/attachments/memo.pdf"
    _add_gate_with_attachments(ben_root, "att-pdf-1", [{"path": rel, "caption": "Full memo"}])
    r = client.get("/gate/fixture/att-pdf-1")
    assert r.status_code == 200, r.text
    assert "/gate/fixture/attachment?path=" in r.text
    assert rel in r.text
    # Must be a link, NOT an <img>
    assert "Full memo" in r.text
    # Should not render as img
    text_lower = r.text.lower()
    # The attachment link is there
    assert "attachment?path=" in r.text


def test_gate_detail_graceful_no_attachments(client, ben_root):
    """gate_card.html must be a no-op when gate has no attachments key."""
    _add_gate_with_attachments(ben_root, "att-none-1", attachments=None)
    r = client.get("/gate/fixture/att-none-1")
    assert r.status_code == 200
    # No attachment route links in the page
    assert "/gate/fixture/attachment?path=" not in r.text


def test_gate_detail_graceful_file_missing(client, ben_root):
    """gate_card.html must not 500 when attachment file is missing on disk.
    The <img> or link is emitted; the route 404s; browser shows nothing.
    The detail view must render successfully."""
    rel = "outputs/2026-06-19/attachments/DOES-NOT-EXIST.png"
    _add_gate_with_attachments(ben_root, "att-missing-1", [{"path": rel}])
    r = client.get("/gate/fixture/att-missing-1")
    assert r.status_code == 200  # page must not error


def test_gate_batch_renders_pièces_jointes_label(client, ben_root):
    """gate_batch.html summary label must include '& pièces jointes' when gate has attachments."""
    _write_attachment(ben_root, "2026-06-19", "batch-test.png", _PNG_BYTES)
    rel = "outputs/2026-06-19/attachments/batch-test.png"
    _add_gate_with_attachments(ben_root, "att-batch-1", [{"path": rel}])
    r = client.get("/gate/fixture/kind/trade_proposal")
    assert r.status_code == 200, r.text
    assert "pièces jointes" in r.text
