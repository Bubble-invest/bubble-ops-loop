"""decision_signing.py — Ed25519 signing/verification for cockpit gate decisions.

Board #1476. Every human-gated action in the fleet (ben trade proposals,
directive_emit, tony's tools/gcal_create.py, and others) trusts
`inbox/decisions/<gate_id>.yaml` with `action: approve`. In production the
cockpit (bubble-console, since #1463) writes those files, but the dept
agent's own uid also has git push to its repo and main isn't branch-
protected — so the decision file, being content-only and unsigned, was
forgeable by the very agent it's supposed to gate (parity with #1462).

Joris's call (2026-09-23, option (a)): the cockpit SIGNS every decision it
writes; executors VERIFY it before trusting `action: approve`.

Why Ed25519, not HMAC: verification happens INSIDE the dept agent's uid
(the executor scripts run as agent-<slug>, e.g. Ben's execution path). A
shared HMAC secret readable there would let the very agent we're gating
forge its own approval — the exact hole this closes. Ed25519 is asymmetric:
only the cockpit (uid bubble-console, since #1463) holds the PRIVATE key
(SOPS-injected, root:bubble-console 0640 — mirrors
`console/deploy/cockpit-approver/`'s App-key injection); every dept only
ever needs the PUBLIC key, which is safe to commit to this repo and vendor
to every dept checkout (see scripts/keys/README.md).

What's signed (the "security-relevant fields", card #1476's list):
    gate_id, action, dept, decided_by, decided_at, gate_hash

`gate_hash` is sha256(the exact gate YAML text the decision was made
against) — WITHOUT it, a still-valid signature over {gate_id, action, ...}
could be replayed onto a gate file that was edited AFTER the operator
approved a different version of it (board #1476 explicitly calls this out:
"a hash of the gate file content so an approval can't be replayed onto a
modified gate"). `dept` is included so a decision can't be lifted from one
dept's repo and replayed against a same-named gate_id in another dept's.

Two layers, on purpose:
  - `verify_signature()` is the STRICT cryptographic primitive: valid
    Ed25519 signature over the canonical payload, or it isn't. No opinion
    on what the caller should DO about a bad signature.
  - `verify_decision()` is the executor-facing helper that also applies the
    DECISION_SIGNATURES=warn|enforce migration knob (default warn) — so
    existing unsigned decisions in flight the day this ships don't hard-
    break every dept's gate execution. Flip to `enforce` (per dept, once
    each has adopted this) once the cockpit has been signing for a while
    and no unsigned/forged decision should ever legitimately appear.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_log = logging.getLogger("decision_signing")

# The exact fields a signature covers, in this fixed order (order doesn't
# matter for the JSON encoding below since we sort_keys, but keeping an
# explicit tuple makes "what's signed" a single, greppable source of truth
# instead of scattered dict-comprehension logic).
SIGNED_FIELDS = ("gate_id", "action", "dept", "decided_by", "decided_at", "gate_hash")

# Default key id embedded in every signature this module produces. Bump this
# (e.g. "cockpit-decision-key-v2") if/when the key is ever rotated, so a
# verifier with multiple trusted public keys can pick the right one —
# `verify_decision()` doesn't do multi-key lookup today (single key, single
# id), but stamping the id from day one avoids a schema change at rotation
# time.
DEFAULT_KEY_ID = "cockpit-decision-key-v1"

# Where the cockpit (uid bubble-console) reads the SOPS-injected Ed25519
# PRIVATE key from. Mirrors console/deploy/cockpit-approver's
# /run/bubble-cockpit-approver/token layout: tmpfs, root:bubble-console,
# 0640 — root writes it (via a boot-time decrypt, NOT a 45-min timer like
# the App-token mint, since this key is static and never expires/rotates on
# a schedule), the console process only ever reads it.
DEFAULT_PRIVATE_KEY_PATH = "/run/bubble-cockpit-decision-key/key"
PRIVATE_KEY_PATH_ENV = "DECISION_SIGNING_PRIVATE_KEY_PATH"

# Where the PUBLIC key lives — committed to this repo (scripts/keys/), and
# vendored to every dept checkout the same way scripts/lib/*.py already is
# (see scripts/keys/README.md). Safe to read from any uid; it's public.
DEFAULT_PUBLIC_KEY_PATH = Path(__file__).resolve().parent.parent / "keys" / "decision-signing-ed25519.pub.pem"
PUBLIC_KEY_PATH_ENV = "DECISION_SIGNING_PUBLIC_KEY_PATH"

# Migration knob (card #1476 item 3). "warn" (default): a missing/invalid/
# forged/replayed signature is logged and STILL ALLOWED — so depts that
# haven't adopted verify_decision()'s caller-side enforcement yet, or whose
# in-flight decisions predate this feature, don't suddenly stop executing
# approved actions. "enforce": the same conditions are REFUSED. Flip per
# dept once each executor has adopted verify_decision() (see the PR body's
# follow-up list) and the cockpit has been signing in production for a
# while.
MODE_ENV = "DECISION_SIGNATURES"
_VALID_MODES = ("warn", "enforce")
_DEFAULT_MODE = "warn"


class DecisionSigningError(Exception):
    """Raised for programmer-error style misuse (e.g. signing a decision
    that's missing a required field). NOT raised for untrusted input —
    verify_signature()/verify_decision() always return, never raise, on bad
    input, so a malicious/malformed decision file can't crash an executor."""


def _canonical_payload(fields: Dict[str, Any]) -> bytes:
    """Deterministic byte serialization of the signed fields, for both
    sign() and verify(). Plain JSON with sorted keys and no incidental
    whitespace — stable across Python versions/platforms (unlike
    yaml.safe_dump, which is not guaranteed byte-stable and is a much
    larger attack surface to canonicalize correctly)."""
    missing = [f for f in SIGNED_FIELDS if f not in fields or fields[f] in (None, "")]
    if missing:
        raise DecisionSigningError(f"cannot sign/verify: missing field(s) {missing}")
    subset = {k: fields[k] for k in SIGNED_FIELDS}
    return json.dumps(subset, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def gate_hash(gate_content: str) -> str:
    """sha256 hex digest of the EXACT gate YAML text a decision was made
    against. Binding the signature to this (not just the gate_id) is what
    stops an old, validly-signed approval from being replayed onto a gate
    file that was edited after the operator saw it."""
    return hashlib.sha256(gate_content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

def load_private_key(path: Optional[Union[str, Path]] = None) -> Ed25519PrivateKey:
    """Load the cockpit's Ed25519 PRIVATE key (PEM, PKCS8, unencrypted — the
    file's own permissions, not a passphrase, are the access control, same
    as the cockpit-approver App .pem post-decrypt). Raises FileNotFoundError
    / ValueError with a clear message on any failure — callers decide how to
    degrade (see github_reader._sign_decision's fail-open-to-unsigned
    behaviour: a cockpit that can't sign must never simply hang or crash the
    whole gate-decide request)."""
    p = Path(path or os.environ.get(PRIVATE_KEY_PATH_ENV) or DEFAULT_PRIVATE_KEY_PATH)
    data = p.read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{p}: not an Ed25519 private key")
    return key


def load_public_key(path: Optional[Union[str, Path]] = None) -> Ed25519PublicKey:
    """Load the committed/vendored Ed25519 PUBLIC key (PEM, SubjectPublicKeyInfo).
    Raises FileNotFoundError / ValueError with a clear message on any failure."""
    p = Path(path or os.environ.get(PUBLIC_KEY_PATH_ENV) or DEFAULT_PUBLIC_KEY_PATH)
    data = p.read_bytes()
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{p}: not an Ed25519 public key")
    return key


# ---------------------------------------------------------------------------
# Sign
# ---------------------------------------------------------------------------

def sign_decision(
    decision: Dict[str, Any],
    *,
    gate_content: str,
    dept: str,
    private_key: Ed25519PrivateKey,
    key_id: str = DEFAULT_KEY_ID,
) -> Dict[str, Any]:
    """Return a NEW dict — `decision` plus `dept`, `gate_hash`, `key_id`,
    `signature` — signed with `private_key`. Does not mutate `decision`.

    Required in `decision` before calling: gate_id, action, decided_by,
    decided_at (exactly what console/routes/gate.py::gate_decide already
    builds — see github_reader._sign_decision).
    """
    enriched = dict(decision)
    enriched["dept"] = dept
    enriched["gate_hash"] = gate_hash(gate_content)
    payload = _canonical_payload(enriched)
    signature = private_key.sign(payload)
    enriched["key_id"] = key_id
    enriched["signature"] = base64.b64encode(signature).decode("ascii")
    return enriched


# ---------------------------------------------------------------------------
# Verify — strict cryptographic primitive
# ---------------------------------------------------------------------------

def verify_signature(decision: Dict[str, Any], public_key: Ed25519PublicKey) -> tuple[bool, str]:
    """Strict Ed25519 verification of `decision` against `public_key`.
    Returns (ok, reason) — NEVER raises on malformed/malicious input (a
    forged decision file is untrusted data, not a bug). `ok=True` means the
    signature is cryptographically valid over EXACTLY the fields currently
    in `decision` (including `gate_hash` — the caller is responsible for
    having set `decision["gate_hash"]` to the hash of the gate file it
    actually wants checked; see verify_decision(), which does this for you).
    """
    sig_b64 = decision.get("signature")
    if not sig_b64 or not isinstance(sig_b64, str):
        return False, "no signature field (unsigned decision)"
    try:
        signature = base64.b64decode(sig_b64, validate=True)
    except Exception:
        return False, "signature field is not valid base64"
    try:
        payload = _canonical_payload(decision)
    except DecisionSigningError as exc:
        return False, str(exc)
    try:
        public_key.verify(signature, payload)
    except InvalidSignature:
        return False, "signature does not verify (tampered field or forged decision)"
    except Exception as exc:  # noqa: BLE001 — never let a crypto-lib surprise raise into an executor
        return False, f"signature verification error: {exc}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Verify — executor-facing helper (reads files, applies warn/enforce)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifyResult:
    verified: bool  # True ONLY if the signature cryptographically checked out
    allowed: bool   # True if the executor should proceed (verified, OR mode == "warn")
    reason: str
    mode: str


def _mode(explicit: Optional[str]) -> str:
    mode = (explicit or os.environ.get(MODE_ENV) or _DEFAULT_MODE).strip().lower()
    return mode if mode in _VALID_MODES else _DEFAULT_MODE


def verify_decision(
    decision_path: Union[str, Path],
    gate_path: Union[str, Path],
    *,
    public_key_path: Optional[Union[str, Path]] = None,
    mode: Optional[str] = None,
    expected_dept: Optional[str] = None,
) -> VerifyResult:
    """The call an executor makes before trusting `action: approve`.

        result = verify_decision(
            "inbox/decisions/trade-proposal-ACGL.yaml",
            "queues/gates/trade-proposal-ACGL.yaml",
            expected_dept="ben",
        )
        if not result.allowed:
            raise NotApprovedError(...)   # or equivalent refusal
        if not result.verified:
            log.warning("proceeding on an unverified decision (warn mode): %s", result.reason)

    `mode` defaults to the DECISION_SIGNATURES env var, default "warn" — see
    the module docstring. Every failure mode (missing decision file, missing
    gate file, gate_hash mismatch/replay, missing/forged signature, missing
    public key) goes through the SAME warn/enforce gate, so a dept that
    hasn't provisioned the public key yet degrades exactly like one that has
    an unsigned decision — never a silent, unlogged bypass.
    """
    resolved_mode = _mode(mode)

    try:
        decision = yaml.safe_load(Path(decision_path).read_text(encoding="utf-8")) or {}
        if not isinstance(decision, dict):
            raise ValueError("decision YAML did not parse to a mapping")
    except Exception as exc:
        return _finish(False, f"could not read/parse decision file: {exc}", resolved_mode)

    try:
        gate_content = Path(gate_path).read_text(encoding="utf-8")
    except Exception as exc:
        return _finish(False, f"could not read gate file: {exc}", resolved_mode)

    # Recompute the gate hash BEFORE the crypto check — gives a much clearer
    # reason ("gate changed / replay") than a generic signature failure when
    # that's specifically what happened, even though a hash mismatch WOULD
    # also just fail verify_signature() below (gate_hash is a signed field).
    actual_hash = gate_hash(gate_content)
    claimed_hash = decision.get("gate_hash")
    if claimed_hash != actual_hash:
        return _finish(
            False,
            "gate_hash mismatch — the decision was signed against different gate "
            "content than the gate file currently on disk (edited-after-approval, "
            "or a replay onto a different gate)",
            resolved_mode,
        )

    if expected_dept is not None and decision.get("dept") != expected_dept:
        return _finish(
            False,
            f"dept mismatch: decision signed for {decision.get('dept')!r}, "
            f"expected {expected_dept!r}",
            resolved_mode,
        )

    try:
        public_key = load_public_key(public_key_path)
    except Exception as exc:
        return _finish(False, f"could not load public key: {exc}", resolved_mode)

    ok, reason = verify_signature(decision, public_key)
    return _finish(ok, reason, resolved_mode)


def _finish(verified: bool, reason: str, mode: str) -> VerifyResult:
    if verified:
        return VerifyResult(verified=True, allowed=True, reason=reason, mode=mode)
    if mode == "enforce":
        return VerifyResult(verified=False, allowed=False, reason=reason, mode=mode)
    _log.warning("decision signature check failed, ALLOWING (DECISION_SIGNATURES=warn): %s", reason)
    return VerifyResult(verified=False, allowed=True, reason=reason, mode=mode)
