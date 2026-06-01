"""Shared pytest fixtures for bubble-token-broker tests.

Notion v4 doctrine (lines ~563-720):
  - PEM stays in memory only (no disk path stored, no temp file written)
  - Tokens are NEVER persisted to disk
  - GitHub API is mocked end-to-end in tests; no live network in test runs.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Make src/ importable for tests
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --- PEM fixture -----------------------------------------------------------


@pytest.fixture(scope="session")
def mock_pem() -> bytes:
    """A valid in-memory RSA-2048 PEM, never touches disk.

    Per Notion v4 §"Secrets": private keys live in SOPS-encrypted form on disk
    and are decrypted to memory only. In tests we skip SOPS and synthesize a
    real RSA-2048 key in process memory.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def mock_pem_provider(mock_pem):
    """A callable[[], bytes] that returns the in-memory PEM.

    Mirrors the production injection shape: in prod, the provider is a thin
    wrapper around `sops --decrypt --input-type binary --output-type binary
    /srv/bubble-secrets/...sops.pem` that captures stdout and returns the
    bytes without ever writing to disk.
    """
    return lambda: mock_pem


# --- App / installation identifiers ---------------------------------------


@pytest.fixture
def mock_app_id() -> int:
    """Stub GitHub App ID (production = 3782718 per /etc/bubble/secrets.sops.env)."""
    return 3782718


@pytest.fixture
def mock_installation_id() -> int:
    """Stub installation ID for bubble-ops-fixture (production = 134075326)."""
    return 134075326


@pytest.fixture
def mock_client_id() -> str:
    """Stub GitHub App client ID."""
    return "Iv23cteHxkkWlFqSloaa"


# --- GitHub API response shapes -------------------------------------------


@pytest.fixture
def mock_github_token_response() -> dict[str, Any]:
    """Canonical installation-token response shape per GitHub REST API docs.

    https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
    """
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=60)
    return {
        "token": "ghs_FAKETOKENVALUE0123456789abcdef",
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "permissions": {"contents": "write", "metadata": "read"},
        "repository_selection": "selected",
        "repositories": [
            {"id": 1, "name": "bubble-ops-fixture", "full_name": "vdk888/bubble-ops-fixture"}
        ],
    }


@pytest.fixture
def mocked_requests_post(monkeypatch, mock_github_token_response):
    """Monkeypatch requests.post so the broker never hits the real API.

    Returns a MagicMock with `.call_args_list` for assertion.
    """
    from src import broker as broker_module  # late import (Phase A: may not exist)

    mock = MagicMock()
    response = MagicMock()
    response.status_code = 201
    response.json.return_value = mock_github_token_response
    response.text = json.dumps(mock_github_token_response)
    response.raise_for_status = MagicMock()
    mock.return_value = response
    monkeypatch.setattr(broker_module.requests, "post", mock)
    return mock


# --- Policy YAML fixtures -------------------------------------------------


@pytest.fixture
def ops_policy_yaml(tmp_path) -> Path:
    """Notion v4 §'Ops department standard' policy shape."""
    data = {
        "github_access": {
            "actor": "ops-loop-fixture",
            "own_repo": "bubble-ops-fixture",
            "read": ["bubble-ops-fixture", "bubble-shared-wiki"],
            "write": [
                {
                    "repo": "bubble-ops-fixture",
                    "allowed_paths": ["outputs/**", "queues/**", "inbox/**"],
                    "mode": "direct_runtime_commit",
                }
            ],
            "pull_requests": {"can_open_to": []},
        }
    }
    path = tmp_path / "fixture-policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def tony_policy_yaml(tmp_path) -> Path:
    """Notion v4 §'Management / Tony' policy shape."""
    data = {
        "github_access": {
            "actor": "ops-loop-tony",
            "own_repo": "bubble-ops-tony",
            "read": ["bubble-ops-tony", "bubble-ops-ben", "bubble-ops-maya"],
            "write": [
                {
                    "repo": "bubble-ops-tony",
                    "allowed_paths": ["outputs/**", "queues/**", "inbox/**"],
                    "mode": "direct_runtime_commit",
                }
            ],
            "pull_requests": {
                "can_open_to": [
                    "bubble-ops-ben",
                    "bubble-ops-maya",
                    "bubble-ops-miranda",
                    "bubble-ops-eliot",
                ],
                "target_paths": ["queues/management/**"],
            },
        }
    }
    path = tmp_path / "tony-policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def console_policy_yaml(tmp_path) -> Path:
    """Notion v4 §'Console' policy shape (can open settings PRs)."""
    data = {
        "github_access": {
            "actor": "bubble-ops-console",
            "write": [
                {
                    "repo": "bubble-ops-*",
                    "allowed_paths": ["inbox/decisions/**", "queues/gates/**"],
                }
            ],
            "pull_requests": {"can_open_settings_pr": True},
        }
    }
    path = tmp_path / "console-policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return path
