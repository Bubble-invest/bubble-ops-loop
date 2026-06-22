"""
test_auth.py — bearer-token auth middleware.

Notion v5 line 1011: "Auth: bearer token + port Tailscale-only".
{{OPERATOR}} is the sole operator; one CONSOLE_BEARER_TOKEN env var.
"""


def test_protected_route_rejects_request_without_bearer(client_noauth):
    """GET / without Authorization header must return 401."""
    r = client_noauth.get("/")
    assert r.status_code == 401


def test_protected_route_accepts_valid_bearer(client):
    """GET / with valid bearer returns 200."""
    r = client.get("/")
    assert r.status_code == 200


def test_protected_route_rejects_wrong_token(client_noauth):
    """A bogus bearer must return 401, not silently fall through."""
    r = client_noauth.get("/", headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401
