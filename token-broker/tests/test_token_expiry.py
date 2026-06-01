"""Cache + expiry tests — TTL ≤60min per Notion v4, refresh before expiry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock


def test_cache_hit_when_not_expired(
    mocked_requests_post, mock_pem_provider, mock_app_id, mock_installation_id
):
    """Two mint calls with the same (dept, action, repo) within TTL must reuse cache."""
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    t1 = b.mint(dept="fixture", action="runtime_read", repo="bubble-ops-fixture")
    t2 = b.mint(dept="fixture", action="runtime_read", repo="bubble-ops-fixture")
    assert t1.value == t2.value
    # GitHub API should have been hit exactly once (second was cache hit)
    assert mocked_requests_post.call_count == 1


def test_cache_miss_when_expired(
    monkeypatch, mock_pem_provider, mock_app_id, mock_installation_id, mock_github_token_response
):
    """When cached token is within the safety buffer of expires_at, mint must refresh."""
    from src import broker as broker_module
    from src.broker import Broker

    # Build a response that expires in 30s (well inside the 60s buffer)
    near_expiry = datetime.now(timezone.utc) + timedelta(seconds=30)
    near_expiry_response = dict(mock_github_token_response)
    near_expiry_response["expires_at"] = near_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    near_expiry_response["token"] = "ghs_FIRSTtoken"

    second_response_data = dict(mock_github_token_response)
    second_response_data["token"] = "ghs_SECONDtoken"

    call_count = {"n": 0}

    def fake_post(*args, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        resp.status_code = 201
        if call_count["n"] == 1:
            resp.json.return_value = near_expiry_response
        else:
            resp.json.return_value = second_response_data
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(broker_module.requests, "post", fake_post)

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    t1 = b.mint(dept="fixture", action="runtime_read", repo="bubble-ops-fixture")
    t2 = b.mint(dept="fixture", action="runtime_read", repo="bubble-ops-fixture")

    assert t1.value == "ghs_FIRSTtoken"
    assert t2.value == "ghs_SECONDtoken"
    assert call_count["n"] == 2, "Expected a refresh when cached token was inside expiry buffer"


def test_cache_keyed_by_dept_action_repo(
    monkeypatch, mock_pem_provider, mock_app_id, mock_installation_id, mock_github_token_response
):
    """Different cache keys must mint separate tokens."""
    from src import broker as broker_module
    from src.broker import Broker

    call_count = {"n": 0}

    def fake_post(*args, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        resp.status_code = 201
        body = dict(mock_github_token_response)
        body["token"] = f"ghs_TOKEN{call_count['n']}"
        resp.json.return_value = body
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(broker_module.requests, "post", fake_post)

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    t1 = b.mint(dept="fixture", action="runtime_read", repo="bubble-ops-fixture")
    t2 = b.mint(dept="fixture", action="runtime_write_own", repo="bubble-ops-fixture")  # diff action
    t3 = b.mint(dept="tony", action="runtime_read", repo="bubble-ops-tony")  # diff dept+repo

    assert t1.value != t2.value
    assert t2.value != t3.value
    assert call_count["n"] == 3
