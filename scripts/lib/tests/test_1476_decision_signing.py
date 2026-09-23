"""#1476 — Ed25519 signing/verification of inbox/decisions/*.yaml.

Every human-gated action (ben trade proposals, directive_emit, tony's
tools/gcal_create.py, ...) trusts a decision file's `action: approve`. This
was forgeable by the dept agent that's supposed to be gated by it (the
agent's own uid can push to its own repo). Joris's call (2026-09-23, option
(a)): the cockpit signs, executors verify — asymmetric (Ed25519), because
verification runs INSIDE the gated agent's uid, where a symmetric (HMAC) key
would let it forge its own approval.

These tests generate their OWN throwaway Ed25519 keypair per test (via
`cryptography`, not the repo's real key material — none exists in this repo;
see scripts/keys/README.md) and never touch DEFAULT_PRIVATE_KEY_PATH or a
real gate/decision file.

Run: python3 -m pytest scripts/lib/tests/test_1476_decision_signing.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib import decision_signing as ds  # noqa: E402


GATE_CONTENT = (
    "id: trade-proposal-ACGL-2026-09-23\n"
    "kind: trade_proposal\n"
    "summary: Buy 10 ACGL @ market\n"
)

OTHER_GATE_CONTENT = (
    "id: trade-proposal-ACGL-2026-09-23\n"
    "kind: trade_proposal\n"
    "summary: Buy 10000 ACGL @ market\n"  # a materially different order
)


def _base_decision() -> dict:
    return {
        "gate_id": "trade-proposal-ACGL-2026-09-23",
        "action": "approve",
        "comment": "looks good",
        "decided_at": "2026-09-23T09:00:00Z",
        "decided_by": "joris",
    }


@pytest.fixture()
def keypair(tmp_path):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_path = tmp_path / "private.pem"
    pub_path = tmp_path / "public.pem"
    priv_path.write_bytes(priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    pub_path.write_bytes(pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return priv, pub, priv_path, pub_path


def _write(tmp_path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# sign_decision / verify_signature — the crypto primitive
# ---------------------------------------------------------------------------

def test_valid_signature_verifies(keypair):
    priv, pub, _, _ = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    assert signed["key_id"] == ds.DEFAULT_KEY_ID
    assert "signature" in signed and signed["signature"]
    ok, reason = ds.verify_signature(signed, pub)
    assert ok is True
    assert reason == "ok"


def test_sign_decision_does_not_mutate_input(keypair):
    priv, _, _, _ = keypair
    original = _base_decision()
    snapshot = dict(original)
    ds.sign_decision(original, gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    assert original == snapshot


@pytest.mark.parametrize("field,new_value", [
    ("action", "reject"),
    ("gate_id", "trade-proposal-OTHER-2026-09-23"),
    ("dept", "tony"),
    ("decided_by", "the-agent-itself"),
    ("decided_at", "2026-09-23T23:59:59Z"),
])
def test_tampered_field_fails_verification(keypair, field, new_value):
    priv, pub, _, _ = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    tampered = dict(signed)
    tampered[field] = new_value
    ok, reason = ds.verify_signature(tampered, pub)
    assert ok is False
    assert "tampered" in reason or "forged" in reason


def test_forged_decision_with_wrong_key_fails(keypair):
    _, _, _, _ = keypair
    other_priv = Ed25519PrivateKey.generate()  # the dept agent's own forged key
    forged = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=other_priv)
    _, real_pub, _, _ = keypair
    ok, reason = ds.verify_signature(forged, real_pub)
    assert ok is False


def test_unsigned_decision_fails_verification(keypair):
    _, pub, _, _ = keypair
    ok, reason = ds.verify_signature(_base_decision(), pub)
    assert ok is False
    assert "no signature" in reason


def test_replay_onto_different_gate_content_fails(keypair):
    """A validly-signed decision for one gate body must not verify against a
    DIFFERENT gate body at the same gate_id (edited-after-approval / replay)."""
    priv, pub, _, _ = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    # Attacker (or an innocent re-edit) swaps in a different gate_hash without
    # re-signing — verify_signature alone catches this because gate_hash is
    # itself a signed field.
    replayed = dict(signed)
    replayed["gate_hash"] = ds.gate_hash(OTHER_GATE_CONTENT)
    ok, reason = ds.verify_signature(replayed, pub)
    assert ok is False


# ---------------------------------------------------------------------------
# verify_decision — the executor-facing helper (files + warn/enforce)
# ---------------------------------------------------------------------------

def test_verify_decision_valid_signature_passes_both_modes(tmp_path, keypair):
    priv, pub, _, pub_path = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    for mode in ("warn", "enforce"):
        result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path, mode=mode)
        assert result.verified is True
        assert result.allowed is True
        assert result.mode == mode


def test_verify_decision_tampered_field_warn_allows_but_flags_unverified(tmp_path, keypair):
    priv, pub, _, pub_path = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    signed["action"] = "reject_no_wait_approve"  # tampered post-signing
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path, mode="warn")
    assert result.verified is False
    assert result.allowed is True  # warn mode: logged, not blocked


def test_verify_decision_tampered_field_enforce_blocks(tmp_path, keypair):
    priv, pub, _, pub_path = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    signed["action"] = "reject_no_wait_approve"
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path, mode="enforce")
    assert result.verified is False
    assert result.allowed is False


def test_verify_decision_forged_unsigned_warn_vs_enforce(tmp_path, keypair):
    _, _, _, pub_path = keypair
    unsigned = _base_decision()  # never signed at all — e.g. hand-edited by the agent
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(unsigned))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    warn_result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path, mode="warn")
    assert warn_result.verified is False
    assert warn_result.allowed is True

    enforce_result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path, mode="enforce")
    assert enforce_result.verified is False
    assert enforce_result.allowed is False


def test_verify_decision_replay_onto_modified_gate_fails(tmp_path, keypair):
    """The gate file on disk changed AFTER the operator approved it — the
    stored gate_hash from signing time no longer matches. Must fail even
    though the signature itself is otherwise perfectly valid."""
    priv, pub, _, pub_path = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    # Gate file edited after approval (or: decision replayed onto a
    # same-gate_id file with different content).
    gate_path = _write(tmp_path, "gate.yaml", OTHER_GATE_CONTENT)

    enforce_result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path, mode="enforce")
    assert enforce_result.verified is False
    assert enforce_result.allowed is False
    assert "gate_hash mismatch" in enforce_result.reason

    warn_result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path, mode="warn")
    assert warn_result.verified is False
    assert warn_result.allowed is True


def test_verify_decision_dept_mismatch_fails(tmp_path, keypair):
    """A validly-signed decision for dept 'ben' must not verify when an
    executor in a DIFFERENT dept checks it against its own expected_dept
    (e.g. a same-named gate_id copied across repos)."""
    priv, pub, _, pub_path = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    result = ds.verify_decision(
        decision_path, gate_path, public_key_path=pub_path, mode="enforce", expected_dept="tony",
    )
    assert result.allowed is False


def test_verify_decision_missing_public_key_degrades_by_mode(tmp_path, keypair):
    priv, _, _, _ = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)
    missing_pub = tmp_path / "does-not-exist.pem"

    warn_result = ds.verify_decision(decision_path, gate_path, public_key_path=missing_pub, mode="warn")
    assert warn_result.allowed is True
    enforce_result = ds.verify_decision(decision_path, gate_path, public_key_path=missing_pub, mode="enforce")
    assert enforce_result.allowed is False


def test_verify_decision_default_mode_is_warn(tmp_path, keypair, monkeypatch):
    monkeypatch.delenv(ds.MODE_ENV, raising=False)
    _, _, _, pub_path = keypair
    unsigned = _base_decision()
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(unsigned))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path)
    assert result.mode == "warn"
    assert result.allowed is True


def test_verify_decision_mode_env_var(tmp_path, keypair, monkeypatch):
    monkeypatch.setenv(ds.MODE_ENV, "enforce")
    _, _, _, pub_path = keypair
    unsigned = _base_decision()
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(unsigned))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path)
    assert result.mode == "enforce"
    assert result.allowed is False
