"""Multi-installation per broker process — Q2 refactor.

Per Joris's Step 3b Q2 review: when N depts run on Morty (Tony, Maya, Ben,
Miranda, Eliot...), one broker process should be able to mint tokens for
multiple GitHub App installations without re-instantiation. The previous
design locked installation_id in __init__, which forced N broker processes.

Refactor:
  - `installation_id` moves from `Broker.__init__` to `Broker.mint(installation_id=...)`
  - Cache key extended from (dept, action, repo) to (installation_id, dept, action, repo)
  - Backward-compat: __init__ still accepts installation_id as a DEFAULT used when
    mint() is called without explicit installation_id (so existing single-installation
    callers don't break)

Notion v4 §"Token broker Morty" (lines 590-602) describes the broker as a
local mint-on-demand component scoped per-(dept, action). The (installation_id)
dimension was implicit in v1 because we had 1 installation. v2 makes it explicit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


def _ok_response(token_value: str = "ghs_TESTtoken12345678") -> dict:
    return {
        "token": token_value,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "permissions": {"contents": "write"},
        "repository_selection": "selected",
        "repositories": [{"name": "bubble-ops-fixture"}],
    }


def test_mint_accepts_installation_id_kwarg(mock_pem):
    """mint(installation_id=...) overrides the __init__ default."""
    from src.broker import Broker

    b = Broker(
        app_id=3782718,
        installation_id=111,  # default
        pem_provider=lambda: mock_pem,
    )

    with patch("src.broker.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            ok=True, json=lambda: _ok_response("ghs_INST222"), raise_for_status=lambda: None
        )
        token = b.mint(
            dept="maya",
            action="runtime_read",
            repo="bubble-ops-maya",
            installation_id=222,  # override
        )
    # Verify the POST URL contains 222, not 111
    posted_url = mock_post.call_args[0][0]
    assert "/app/installations/222/access_tokens" in posted_url
    assert "/app/installations/111/" not in posted_url


def test_mint_falls_back_to_init_installation_id(mock_pem):
    """When mint() called without installation_id, uses __init__'s value."""
    from src.broker import Broker

    b = Broker(
        app_id=3782718,
        installation_id=111,
        pem_provider=lambda: mock_pem,
    )

    with patch("src.broker.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            ok=True, json=lambda: _ok_response(), raise_for_status=lambda: None
        )
        b.mint(dept="fixture", action="runtime_read", repo="bubble-ops-fixture")
    posted_url = mock_post.call_args[0][0]
    assert "/app/installations/111/access_tokens" in posted_url


def test_cache_separates_different_installations(mock_pem):
    """Tokens for installation 111 and installation 222 are cached independently."""
    from src.broker import Broker

    b = Broker(
        app_id=3782718,
        installation_id=111,
        pem_provider=lambda: mock_pem,
    )

    with patch("src.broker.requests.post") as mock_post:
        # Each installation gets its own token value
        responses = [
            MagicMock(ok=True, json=lambda: _ok_response("ghs_FOR111"), raise_for_status=lambda: None),
            MagicMock(ok=True, json=lambda: _ok_response("ghs_FOR222"), raise_for_status=lambda: None),
        ]
        mock_post.side_effect = responses

        t111 = b.mint(dept="fixture", action="runtime_read", repo="bubble-ops-fixture", installation_id=111)
        t222 = b.mint(dept="maya", action="runtime_read", repo="bubble-ops-maya", installation_id=222)

    assert t111.value == "ghs_FOR111"
    assert t222.value == "ghs_FOR222"
    # Both API calls happened (no cache pollution across installations)
    assert mock_post.call_count == 2


def test_cache_hit_keyed_by_installation_id(mock_pem):
    """Second call with SAME (installation_id, dept, action, repo) hits cache."""
    from src.broker import Broker

    b = Broker(
        app_id=3782718,
        installation_id=111,
        pem_provider=lambda: mock_pem,
    )

    with patch("src.broker.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            ok=True, json=lambda: _ok_response("ghs_CACHED"), raise_for_status=lambda: None
        )
        t1 = b.mint(dept="maya", action="runtime_read", repo="bubble-ops-maya", installation_id=222)
        t2 = b.mint(dept="maya", action="runtime_read", repo="bubble-ops-maya", installation_id=222)

    assert t1 is t2  # same Token object from cache
    assert mock_post.call_count == 1  # only ONE API call (second was cache hit)


def test_cache_miss_when_only_installation_id_differs(mock_pem):
    """Same dept+action+repo but different installation_id = different cache entries."""
    from src.broker import Broker

    b = Broker(
        app_id=3782718,
        installation_id=111,
        pem_provider=lambda: mock_pem,
    )

    with patch("src.broker.requests.post") as mock_post:
        mock_post.side_effect = [
            MagicMock(ok=True, json=lambda: _ok_response("ghs_INST111"), raise_for_status=lambda: None),
            MagicMock(ok=True, json=lambda: _ok_response("ghs_INST222"), raise_for_status=lambda: None),
        ]
        # Same dept/action/repo, but installation_id differs
        b.mint(dept="x", action="runtime_read", repo="repo-x", installation_id=111)
        b.mint(dept="x", action="runtime_read", repo="repo-x", installation_id=222)

    assert mock_post.call_count == 2
