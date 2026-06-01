"""JWT mint tests — proves the broker builds a spec-compliant GitHub App JWT.

GitHub spec (authenticating-as-a-github-app):
  - alg = RS256
  - claims: iat (≤60s past), exp (≤10min future), iss (app_id)
"""

from __future__ import annotations

import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def _b64url_decode(data: str) -> bytes:
    """Decode JWT base64url segment (handles missing padding)."""
    padding_needed = 4 - len(data) % 4
    if padding_needed and padding_needed < 4:
        data += "=" * padding_needed
    return base64.urlsafe_b64decode(data)


def _split_jwt(jwt: str) -> tuple[dict, dict, bytes, bytes]:
    """Return (header, payload, signing_input, signature)."""
    header_b64, payload_b64, signature_b64 = jwt.split(".")
    header = json.loads(_b64url_decode(header_b64))
    payload = json.loads(_b64url_decode(payload_b64))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = _b64url_decode(signature_b64)
    return header, payload, signing_input, signature


def test_jwt_has_three_segments(mock_pem_provider, mock_app_id, mock_installation_id):
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    jwt = b._build_jwt()
    assert jwt.count(".") == 2, "JWT must have exactly header.payload.signature"


def test_jwt_header_is_rs256(mock_pem_provider, mock_app_id, mock_installation_id):
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    jwt = b._build_jwt()
    header, _, _, _ = _split_jwt(jwt)
    assert header["alg"] == "RS256"
    assert header["typ"] == "JWT"


def test_jwt_has_correct_claims(mock_pem_provider, mock_app_id, mock_installation_id):
    from src.broker import Broker

    before = int(time.time())
    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    jwt = b._build_jwt()
    after = int(time.time())

    _, payload, _, _ = _split_jwt(jwt)

    # iss must be the app id (GitHub accepts string-encoded ints)
    assert int(payload["iss"]) == mock_app_id
    # iat must be in the recent past or now (allow 60s clock-skew window per spec)
    assert before - 60 <= payload["iat"] <= after
    # exp ≤ 10 min future per GitHub spec
    assert payload["exp"] - payload["iat"] <= 600
    # exp must be in the future
    assert payload["exp"] > before


def test_jwt_signature_verifies_with_public_key(mock_pem, mock_pem_provider, mock_app_id, mock_installation_id):
    """The JWT signature must verify cryptographically with the matching public key."""
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    jwt = b._build_jwt()

    _, _, signing_input, signature = _split_jwt(jwt)
    private_key = serialization.load_pem_private_key(mock_pem, password=None)
    public_key = private_key.public_key()
    # Should not raise: signature must verify with PKCS1v15 + SHA-256 (RS256)
    public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())


def test_jwt_exp_minus_iat_is_at_most_600s(mock_pem_provider, mock_app_id, mock_installation_id):
    """GitHub rejects JWTs with exp > iat + 10min."""
    from src.broker import Broker

    b = Broker(app_id=mock_app_id, installation_id=mock_installation_id, pem_provider=mock_pem_provider)
    jwt = b._build_jwt()
    _, payload, _, _ = _split_jwt(jwt)
    assert payload["exp"] - payload["iat"] <= 600
    # And > 0 (sanity)
    assert payload["exp"] - payload["iat"] > 0
