"""Static assets (CSS, fonts, images) must NOT require bearer auth.

Why: the bearer middleware was added to protect routes. But /static is loaded
by the browser AFTER the operator's authenticated GET on /, and the browser
does NOT carry the bearer header on follow-up resource requests (only the
first navigation does). So /static returning 401 means CSS never loads in a
real operator browser, and the page renders unstyled.

This bug was caught during the Bureau-de-Cadre UX redesign smoke (msg 2700,
2026-05-21): screenshot showed paper-creme background loading but no
typography/cards/colors because style.css 401'd silently.

The fix: exempt /static/* from the bearer middleware, same as /health-noauth.
The static directory contains ONLY public design assets — no operator data
ever lives there.
"""
from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from console import main, settings


@pytest.fixture
def client_with_static(tmp_path):
    """Build a fresh app with bearer set + a real /static dir on disk."""
    # Use the actual project static dir so style.css exists
    project_static = pathlib.Path(__file__).resolve().parent.parent / "static"
    assert project_static.exists(), "console/static/ must exist (CSS lives there)"
    with patch.object(settings, "BEARER_TOKEN", "test-token-xyz"), \
         patch.object(settings, "STATIC_DIR", project_static):
        app = main.create_app()
        with TestClient(app) as c:
            yield c


def test_static_css_does_not_require_bearer(client_with_static):
    """GET /static/style.css with NO Authorization header must return 200, not 401."""
    r = client_with_static.get("/static/style.css")
    assert r.status_code == 200, (
        f"/static/style.css returned {r.status_code} without bearer — operator "
        f"browsers can't load CSS this way. Expected 200 (static must be public)."
    )
    # And it's actually CSS
    assert "text/css" in r.headers.get("content-type", "").lower()
    # And it's not empty
    assert len(r.content) > 100, "style.css should be substantial (>100 bytes)"


def test_static_css_also_works_with_bearer(client_with_static):
    """Backward compat: if a request DOES carry the bearer, /static still serves."""
    r = client_with_static.get(
        "/static/style.css",
        headers={"Authorization": "Bearer test-token-xyz"},
    )
    assert r.status_code == 200


def test_protected_route_still_requires_bearer(client_with_static):
    """Regression guard: the bearer middleware still works on real routes."""
    r = client_with_static.get("/")
    assert r.status_code == 401, (
        "Regression: / no longer requires bearer. The static-exemption "
        "must be path-specific, not global."
    )


def test_static_path_traversal_blocked(client_with_static):
    """Defense in depth: the static mount must not allow path traversal escape."""
    # FastAPI's StaticFiles blocks ../ traversal by default; verify behavior
    r = client_with_static.get("/static/../main.py")
    # Either 404 (StaticFiles refuses) or 401 (bearer applied to non-/static path)
    # Both are acceptable defense outcomes; the bad outcome is 200 with main.py content.
    assert r.status_code in (401, 404), (
        f"/static/../main.py returned {r.status_code} — must be 401 or 404, "
        f"not 200 (would leak source code)."
    )
