"""GitHub App installation-token broker.

Notion v4 §"Token broker Morty" (lines 592-614). Mints installation access
tokens on demand from a GitHub App's private key, caches them in memory only,
refreshes before expiry, and down-scopes permissions per action class.

Design invariants (enforced by tests):
  - Tokens are NEVER persisted to disk (the cache is a dict in-process).
  - PEM bytes are obtained via an injected `pem_provider` callable that
    sources them in memory (e.g. `sops --decrypt` stdout). The broker never
    writes the PEM anywhere.
  - JWT is RS256, claims iat/exp/iss per GitHub spec
    (https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app).
  - Installation token call uses POST /app/installations/{id}/access_tokens
    with `permissions` and `repositories` down-scoping (defense-in-depth).
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


# --- Permission classes per Notion v4 §"Classes de tokens éphémères" -----

# Notion v4 lines 619-622. Each action class maps to a minimal permissions set
# requested at mint time (GitHub down-scopes the issued token accordingly).
PERMISSION_CLASSES: dict[str, dict[str, str]] = {
    "runtime_read": {"contents": "read", "metadata": "read"},
    "runtime_write_own": {"contents": "write", "metadata": "read"},
    "open_priority_pr": {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
    },
    "settings_pr": {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
    },
}

# Hard cap per Notion v4 ("token_ttl_minutes: 60" in the audit example).
MAX_TTL_MINUTES = 60

# Refresh cached tokens this many seconds before their expires_at, so we never
# hand out a token that GitHub is about to reject.
DEFAULT_REFRESH_BUFFER_SECONDS = 60

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class Token:
    """A short-lived installation access token + its metadata.

    Note: `value` is the raw token. It must never be logged, never written to
    disk by the broker. Consumers may inject it into a child process's
    environment (e.g. GITHUB_TOKEN for a single `git push`) but must not echo it.
    """

    value: str
    expires_at: datetime
    permissions: Mapping[str, str]
    repositories: list[str] = field(default_factory=list)
    repository_selection: str = "selected"

    def is_expired(self, buffer_seconds: int = DEFAULT_REFRESH_BUFFER_SECONDS) -> bool:
        """True when within `buffer_seconds` of expiry (refresh required)."""
        now = datetime.now(timezone.utc)
        return now + timedelta(seconds=buffer_seconds) >= self.expires_at


class Broker:
    """Mints installation access tokens for a single GitHub App installation.

    Construction:
        b = Broker(app_id=..., installation_id=..., pem_provider=lambda: <bytes>)

    The `pem_provider` is invoked on each JWT mint. In production it shells out
    to `sops --decrypt` and captures stdout, never writing the plaintext PEM to
    disk. In tests it returns an in-memory PEM directly.
    """

    def __init__(
        self,
        app_id: int,
        installation_id: int,
        pem_provider: Callable[[], bytes],
        *,
        jwt_ttl_seconds: int = 540,  # 9 min; GitHub max is 10 min
        refresh_buffer_seconds: int = DEFAULT_REFRESH_BUFFER_SECONDS,
    ) -> None:
        self.app_id = int(app_id)
        # installation_id at __init__ is a DEFAULT used when mint() is called
        # without explicit installation_id. Per Q2 refactor: mint(installation_id=...)
        # overrides this, enabling one Broker process to mint for N installations
        # (Tony, Maya, Ben, Miranda, Eliot ... each with their own App installation).
        self.installation_id = int(installation_id)
        self._pem_provider = pem_provider
        self._jwt_ttl_seconds = int(jwt_ttl_seconds)
        self._refresh_buffer_seconds = int(refresh_buffer_seconds)
        # IMPORTANT: in-memory cache only. Keyed by (installation_id, dept, action, repo).
        # The installation_id dimension was added in Q2 refactor (2026-05-20) to support
        # multi-installation processes. Existing callers that don't pass installation_id
        # to mint() implicitly use self.installation_id, preserving v1 behavior.
        # Never persisted, never serialized to disk by this class.
        self._cache: dict[tuple[int, str, str, str | None], Token] = {}

    # --- JWT mint ---------------------------------------------------------

    def _build_jwt(self) -> str:
        """Build an RS256-signed GitHub App JWT.

        Spec:
          - header: {"alg": "RS256", "typ": "JWT"}
          - payload: {"iat": now-30s, "exp": iat + ttl, "iss": app_id}
          - signature: RSA-PKCS1v15(SHA-256)

        The 30-second past-shift on `iat` absorbs GitHub's clock-skew window.
        """
        pem_bytes = self._pem_provider()
        try:
            private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        finally:
            # We can't fully wipe Python bytes, but we drop our reference promptly.
            del pem_bytes
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("GitHub App private key must be RSA")

        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iat": now - 30,
            "exp": now - 30 + self._jwt_ttl_seconds,
            "iss": self.app_id,
        }
        header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        signature_b64 = _b64url(signature)
        return f"{header_b64}.{payload_b64}.{signature_b64}"

    # --- Installation token mint -----------------------------------------

    def mint(
        self,
        dept: str,
        action: str,
        repo: str | None = None,
        installation_id: int | None = None,
    ) -> Token:
        """Return a cached or freshly minted Token.

        Cache key = (installation_id, dept, action, repo). On miss/near-expiry:
          1. Builds a JWT,
          2. POSTs to /app/installations/{id}/access_tokens with the action's
             minimal `permissions` and a `repositories` allow-list,
          3. Parses the response into a Token,
          4. Stores it in the in-memory cache,
          5. Returns it.

        Q2 refactor (2026-05-20): `installation_id` is now a per-mint parameter
        (defaults to `self.installation_id` for backward-compat). This lets one
        Broker process serve multiple GitHub App installations — e.g. one daemon
        minting for `bubble-ops-fixture`, `bubble-ops-maya`, `bubble-ops-ben`,
        etc. — without re-instantiation. Cache isolation between installations
        is enforced by the cache key.

        Notion v4 line 715: token-level path scoping does NOT exist in GitHub.
        Path enforcement is the caller's responsibility (via `policy.py` or
        the Morty git guard / wrapper layer). This method only mints scoped
        tokens — it does NOT take a `paths` argument.
        """
        if action not in PERMISSION_CLASSES:
            raise ValueError(
                f"unknown action class {action!r}. "
                f"Known: {sorted(PERMISSION_CLASSES)}"
            )
        # Resolve effective installation_id (Q2: per-mint override falls back to __init__)
        effective_installation_id = (
            int(installation_id) if installation_id is not None else self.installation_id
        )
        key = (effective_installation_id, dept, action, repo)
        cached = self._cache.get(key)
        if cached is not None and not cached.is_expired(self._refresh_buffer_seconds):
            return cached

        jwt = self._build_jwt()
        permissions = dict(PERMISSION_CLASSES[action])
        body: dict[str, Any] = {"permissions": permissions}
        if repo is not None:
            body["repositories"] = [repo]
        url = f"{GITHUB_API_BASE}/app/installations/{effective_installation_id}/access_tokens"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "bubble-token-broker/0.1",
        }
        response = requests.post(url, json=body, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        token = _parse_token_response(data)
        self._cache[key] = token
        return token


# --- Helpers --------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    """Standard JWT base64url encoding (no padding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _parse_token_response(data: Mapping[str, Any]) -> Token:
    """Convert GitHub's JSON response into a Token dataclass."""
    expires_at_raw = data["expires_at"]
    # GitHub returns RFC 3339 with trailing 'Z'; Python <3.11 needs +00:00.
    if expires_at_raw.endswith("Z"):
        expires_at_iso = expires_at_raw[:-1] + "+00:00"
    else:
        expires_at_iso = expires_at_raw
    expires_at = datetime.fromisoformat(expires_at_iso)
    repos: list[str] = []
    for r in data.get("repositories") or []:
        if isinstance(r, str):
            repos.append(r)
        elif isinstance(r, Mapping):
            name = r.get("name") or r.get("full_name") or ""
            if name:
                repos.append(name)
    return Token(
        value=data["token"],
        expires_at=expires_at,
        permissions=dict(data.get("permissions") or {}),
        repositories=repos,
        repository_selection=str(data.get("repository_selection", "selected")),
    )
