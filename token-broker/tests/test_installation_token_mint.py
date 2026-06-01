"""Installation token mint tests — proves the broker calls the right endpoint
with the right shape and parses the response into a typed Token.
"""

from __future__ import annotations

from datetime import datetime, timezone


def test_mint_calls_correct_endpoint(
    mocked_requests_post, mock_pem_provider, mock_app_id, mock_installation_id
):
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    b.mint(dept="fixture", action="runtime_write_own", repo="bubble-ops-fixture")

    assert mocked_requests_post.called
    call = mocked_requests_post.call_args
    url = call.args[0] if call.args else call.kwargs.get("url")
    expected = f"https://api.github.com/app/installations/{mock_installation_id}/access_tokens"
    assert url == expected


def test_mint_uses_jwt_bearer_header(
    mocked_requests_post, mock_pem_provider, mock_app_id, mock_installation_id
):
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    b.mint(dept="fixture", action="runtime_read", repo="bubble-ops-fixture")

    headers = mocked_requests_post.call_args.kwargs["headers"]
    assert headers["Accept"] == "application/vnd.github+json"
    assert "X-GitHub-Api-Version" in headers
    auth = headers["Authorization"]
    assert auth.startswith("Bearer ")
    jwt = auth.split(" ", 1)[1]
    assert jwt.count(".") == 2, "Bearer must be a JWT (3 dot-separated segments)"


def test_mint_passes_permissions_body(
    mocked_requests_post, mock_pem_provider, mock_app_id, mock_installation_id
):
    """Per Notion v4 'Classes de tokens éphémères', tokens must be down-scoped to the
    minimum permissions needed for the action class. The broker sends them as the
    POST body's `permissions` field (GitHub down-scopes the token accordingly)."""
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    b.mint(dept="fixture", action="runtime_read", repo="bubble-ops-fixture")
    body = mocked_requests_post.call_args.kwargs["json"]
    assert "permissions" in body
    # runtime_read => contents:read only (Notion v4 line 619)
    assert body["permissions"].get("contents") == "read"


def test_mint_runtime_write_own_requests_contents_write(
    mocked_requests_post, mock_pem_provider, mock_app_id, mock_installation_id
):
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    b.mint(dept="fixture", action="runtime_write_own", repo="bubble-ops-fixture")
    body = mocked_requests_post.call_args.kwargs["json"]
    # runtime_write_own => contents:write (Notion v4 line 620)
    assert body["permissions"].get("contents") == "write"


def test_mint_passes_repositories_scope(
    mocked_requests_post, mock_pem_provider, mock_app_id, mock_installation_id
):
    """Per Notion v4 doctrine: defense-in-depth, down-scope to the specific repo."""
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    b.mint(dept="fixture", action="runtime_write_own", repo="bubble-ops-fixture")
    body = mocked_requests_post.call_args.kwargs["json"]
    assert body.get("repositories") == ["bubble-ops-fixture"]


def test_mint_returns_token_with_expiry_and_metadata(
    mocked_requests_post, mock_pem_provider, mock_app_id, mock_installation_id, mock_github_token_response
):
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    token = b.mint(dept="fixture", action="runtime_write_own", repo="bubble-ops-fixture")

    assert token.value == mock_github_token_response["token"]
    assert isinstance(token.expires_at, datetime)
    assert token.expires_at.tzinfo is not None  # must be tz-aware
    assert token.expires_at > datetime.now(timezone.utc)
    assert token.permissions.get("contents") == "write"
    assert isinstance(token.repositories, list)


def test_mint_open_priority_pr_requests_pr_write(
    mocked_requests_post, mock_pem_provider, mock_app_id, mock_installation_id
):
    """Notion v4 line 621: open_priority_pr => contents:write + pull_requests:write."""
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    b.mint(dept="tony", action="open_priority_pr", repo="bubble-ops-ben")
    body = mocked_requests_post.call_args.kwargs["json"]
    assert body["permissions"].get("contents") == "write"
    assert body["permissions"].get("pull_requests") == "write"


def test_mint_settings_pr_requests_pr_write(
    mocked_requests_post, mock_pem_provider, mock_app_id, mock_installation_id
):
    """Notion v4 line 622: settings_pr => contents:write + pull_requests:write."""
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    b.mint(dept="fixture", action="settings_pr", repo="bubble-ops-fixture")
    body = mocked_requests_post.call_args.kwargs["json"]
    assert body["permissions"].get("contents") == "write"
    assert body["permissions"].get("pull_requests") == "write"
