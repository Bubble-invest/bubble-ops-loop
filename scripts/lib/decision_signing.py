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
    + selected_option, selected_option_label (OPTIONAL — only `choose`/#730
      question-gate decisions carry these; see OPTIONAL_SIGNED_FIELDS below)

`gate_hash` is sha256(the exact gate YAML text the decision was made
against) — WITHOUT it, a still-valid signature over {gate_id, action, ...}
could be replayed onto a gate file that was edited AFTER the operator
approved a different version of it (board #1476 explicitly calls this out:
"a hash of the gate file content so an approval can't be replayed onto a
modified gate"). `dept` is included so a decision can't be lifted from one
dept's repo and replayed against a same-named gate_id in another dept's.

`selected_option`/`selected_option_label` (independent security review,
2026-09-24): `console/routes/gate.py::gate_decide()` writes these two on a
`choose`-action decision — they ARE the actionable payload for a question
gate (which of the 2-3 options the operator picked). Deliberately NOT
signed: `comment` (free-text operator note, never drives executor
behaviour).

Public key trust (independent security review finding 1, 2026-09-24):
`DEFAULT_PUBLIC_KEY_PATH` is a FIXED, root-owned framework path
(`/opt/bubble-ops-loop/...` on the VPS — confirmed root:root 0755, not
writable by any `agent-<slug>` uid), never resolved relative to this
module's own `__file__` — that tree gets vendored (copied, not symlinked)
into each dept's agent-writable checkout by
`scripts/vendor-dept-libs.sh`, so a `__file__`-relative default would let
the very uid this module gates overwrite its own trust anchor.
`load_public_key()` additionally refuses (raises) to load from the default/
env-resolved path if the file or ANY parent directory is owned by a
non-root uid or is group/other-writable — defense in depth in case that
fixed path, or a dept's local override of it, is ever misconfigured. See
`load_public_key()`'s docstring for the full resolution order.

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
#
# REQUIRED — every decision must carry these or sign/verify raises.
SIGNED_FIELDS = ("gate_id", "action", "dept", "decided_by", "decided_at", "gate_hash")

# OPTIONAL — signed WHEN PRESENT (see _canonical_payload). Only `choose`
# (#730 question-gate) decisions carry these today; `console/routes/
# gate.py::gate_decide()` is the sole writer. Independent security review
# (2026-09-24, PR #492): these are the actionable payload of a `choose`
# decision — which option the operator picked — and were previously
# unsigned, so a decision could pass a valid signature while its actual
# chosen option had been tampered with post-write. Presence itself is also
# covered: a decision signed WITH these keys can't be "fixed" by an
# attacker simply deleting them, because the canonical payload's key set
# changes too (see _canonical_payload).
OPTIONAL_SIGNED_FIELDS = ("selected_option", "selected_option_label")

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

# Where the PUBLIC key lives. Content is safe to read from any uid (it's
# public) — the risk this module guards against is WRITE access to the
# path a verifier trusts by default. Independent security review finding 1
# (2026-09-24, PR #492): the OLD default (`Path(__file__).resolve().parent
# .parent / "keys" / ...`) resolved relative to wherever decision_signing.py
# itself was loaded from — but that tree gets vendored (copied, chowned
# best-effort, NOT symlinked, no sticky bit on the containing directory)
# into each dept's own agent-writable checkout by
# scripts/vendor-dept-libs.sh. A uid that owns a 0775 directory can
# unlink()+recreate any file inside it regardless of that file's own
# ownership — so the gated uid could overwrite its own vendored public key
# with one whose private half it controls, defeating the entire point of
# asymmetric signing.
#
# Fixed instead: a root-owned framework path OUTSIDE any dept's vendored/
# writable tree. Confirmed on the VPS: /opt/bubble-ops-loop is root:root
# 0755, not writable by any agent-<slug> uid (`ssh joris-cx33 'ls -ld
# /opt/bubble-ops-loop'`). `load_public_key()` also runtime-checks
# ownership of this path (and its parents) before trusting it — see there.
DEFAULT_PUBLIC_KEY_PATH = "/opt/bubble-ops-loop/scripts/keys/decision-signing-ed25519.pub.pem"

# Env override for the public key path — ONLY honored when
# DECISION_SIGNING_TEST_MODE=1 is ALSO set (see _resolve_public_key_path).
# Independent security review finding 1: an unconditional env override is
# itself a hole — an agent that can set its own process/systemd-unit
# environment (which the gated uid can, same threat model as the vendored
# file) could point verification at a key it controls without touching any
# code. A caller that legitimately needs a non-default path passes
# `public_key_path=` explicitly (a code-level decision, reviewed like any
# other), not an env var a runtime process can set for itself.
PUBLIC_KEY_PATH_ENV = "DECISION_SIGNING_PUBLIC_KEY_PATH"
TEST_MODE_ENV = "DECISION_SIGNING_TEST_MODE"

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
    larger attack surface to canonicalize correctly).

    SIGNED_FIELDS (required) always go in. OPTIONAL_SIGNED_FIELDS go in
    WHEN PRESENT in `fields` — this is what makes them tamper-evident even
    against deletion: a `choose` decision's canonical payload has a
    different key set than an approve/reject/defer one, so an attacker
    can't strip `selected_option` back out post-signing to dodge the value
    check; the resulting bytes (and thus the signature) simply no longer
    match either way.
    """
    missing = [f for f in SIGNED_FIELDS if f not in fields or fields[f] in (None, "")]
    if missing:
        raise DecisionSigningError(f"cannot sign/verify: missing field(s) {missing}")
    subset = {k: fields[k] for k in SIGNED_FIELDS}
    for f in OPTIONAL_SIGNED_FIELDS:
        if f in fields:
            subset[f] = fields[f]
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


def _resolve_public_key_path(explicit: Optional[Union[str, Path]]) -> Path:
    """Resolution order for the trusted public key path:

    1. `explicit` — the CALLER passed a path. Trusted as given (no ownership
       walk below): a caller choosing its own path is a code-level decision,
       reviewed like any other, not something a runtime env var can flip.
    2. `DECISION_SIGNING_PUBLIC_KEY_PATH` env var — ONLY when
       `DECISION_SIGNING_TEST_MODE=1` is ALSO set in the environment. See
       PUBLIC_KEY_PATH_ENV's module-level comment for why this is gated.
    3. `DEFAULT_PUBLIC_KEY_PATH` — the fixed, root-owned framework path.
    """
    if explicit is not None:
        return Path(explicit)
    if os.environ.get(TEST_MODE_ENV) == "1":
        env_path = os.environ.get(PUBLIC_KEY_PATH_ENV)
        if env_path:
            return Path(env_path)
    return Path(DEFAULT_PUBLIC_KEY_PATH)


def _walk_up(path: Path):
    """Yield `path` and every parent directory up to and including the
    filesystem root."""
    current = path
    while True:
        yield current
        parent = current.parent
        if parent == current:
            return
        current = parent


def _check_ownership_safe(path: Path, *, stat_fn: Any) -> tuple[bool, str]:
    """Walk `path` and every ancestor directory, refusing (returning
    ok=False) if any of them is owned by a non-root uid or is group/other-
    writable. `stat_fn` is injectable (production default: `os.stat`) so
    tests can simulate arbitrary ownership on a plain tmp_path without
    needing real root privileges on the test runner — see
    scripts/lib/tests/test_1476_decision_signing.py.

    A missing ancestor (FileNotFoundError) is not itself treated as unsafe
    — a not-yet-created directory has no writable content to tamper with —
    but any OTHER stat failure is (fail closed on the unexpected)."""
    resolved = path if path.is_absolute() else path.resolve()
    for candidate in _walk_up(resolved):
        try:
            st = stat_fn(candidate)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 — an unexpected stat failure is unsafe, not ignorable
            return False, f"{candidate}: could not stat ({type(exc).__name__}: {exc})"
        if st.st_uid != 0:
            return False, (
                f"{candidate} is owned by uid {st.st_uid}, not root — a "
                f"non-root uid that owns this path could replace the "
                f"public key file it contains"
            )
        if st.st_mode & 0o022:
            return False, (
                f"{candidate} is writable by group or other "
                f"(mode {oct(st.st_mode & 0o777)})"
            )
    return True, "ok"


def load_public_key(
    path: Optional[Union[str, Path]] = None,
    *,
    stat_fn: Any = os.stat,
) -> Ed25519PublicKey:
    """Load the Ed25519 PUBLIC key used to verify gate decisions (PEM,
    SubjectPublicKeyInfo). Raises FileNotFoundError / ValueError /
    DecisionSigningError with a clear message on any failure.

    Path resolution: see `_resolve_public_key_path()`.

    Ownership check (independent security review finding 1, 2026-09-24):
    whenever the path was NOT given explicitly by the caller (i.e. resolved
    via the test-mode env var or DEFAULT_PUBLIC_KEY_PATH), this refuses to
    load if the resolved file or ANY parent directory is owned by a
    non-root uid or is group/other-writable. Without this, a fixed path
    string is only as trustworthy as every directory above it — e.g. if
    `scripts/keys/*.pub.pem` is ever added to `vendor-dept-libs.sh`'s MAP
    for an offline/local dept, a dept-writable ancestor directory would let
    the gated uid replace the file wholesale even though the path itself
    looks fixed. An explicit `path=` is trusted without this walk (the
    caller already made an informed choice)."""
    explicit = path is not None
    resolved = _resolve_public_key_path(path)
    if not explicit:
        ok, reason = _check_ownership_safe(resolved, stat_fn=stat_fn)
        if not ok:
            raise DecisionSigningError(
                f"refusing to trust public key path {resolved}: {reason}"
            )
    data = resolved.read_bytes()
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{resolved}: not an Ed25519 public key")
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
    expected_gate_id: Optional[str] = None,
    stat_fn: Any = os.stat,
    notify_config: Optional[Dict[str, Any]] = None,
) -> VerifyResult:
    """The call an executor makes before trusting `action: approve`.

        result = verify_decision(
            "inbox/decisions/trade-proposal-ACGL.yaml",
            "queues/gates/trade-proposal-ACGL.yaml",
            expected_dept="ben",
            expected_gate_id="trade-proposal-ACGL",
        )
        if not result.allowed:
            raise NotApprovedError(...)   # or equivalent refusal
        if not result.verified:
            log.warning("proceeding on an unverified decision (warn mode): %s", result.reason)

    `mode` defaults to the DECISION_SIGNATURES env var, default "warn" — see
    the module docstring. Every failure mode (missing decision file, missing
    gate file, gate_hash mismatch/replay, missing/forged signature, missing
    public key, unsafe public-key-path ownership, gate_id/dept mismatch)
    goes through the SAME warn/enforce gate, so a dept that hasn't
    provisioned the public key yet degrades exactly like one that has an
    unsigned decision — never a silent, unlogged bypass.

    `expected_gate_id` (independent security review finding 3, 2026-09-24):
    symmetric with `expected_dept` — a caller that mismatches
    `decision_path`/`gate_path` against the wrong gate (e.g. a copy/paste
    bug wiring the executor) is itself caught here, rather than relying
    solely on the file-naming convention `write_gate_decision` happens to
    follow.

    `stat_fn` is forwarded to `load_public_key()`'s ownership check (see
    there) — production callers never need to pass this; it exists for
    tests.

    `notify_config` (independent security review finding 4, 2026-09-24):
    optional dept config dict forwarded to the loud alert this emits in
    "warn" mode on ANY verification failure (see `_loud_alert`) — pass your
    dept's own `config.yaml`-derived dict (the same one you'd pass to
    `scripts/lib/notify.py::notify_alert`) to get a real Telegram/email
    alert, not just the always-on ERROR-level log line. No config is
    required for the log line; it degrades gracefully without one.
    """
    resolved_mode = _mode(mode)

    try:
        decision = yaml.safe_load(Path(decision_path).read_text(encoding="utf-8")) or {}
        if not isinstance(decision, dict):
            raise ValueError("decision YAML did not parse to a mapping")
    except Exception as exc:
        return _finish(
            False, f"could not read/parse decision file: {exc}", resolved_mode,
            notify_config=notify_config,
        )

    gate_id = decision.get("gate_id")
    dept = decision.get("dept")

    try:
        gate_content = Path(gate_path).read_text(encoding="utf-8")
    except Exception as exc:
        return _finish(
            False, f"could not read gate file: {exc}", resolved_mode,
            gate_id=gate_id, dept=dept, notify_config=notify_config,
        )

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
            resolved_mode, gate_id=gate_id, dept=dept, notify_config=notify_config,
        )

    if expected_gate_id is not None and gate_id != expected_gate_id:
        return _finish(
            False,
            f"gate_id mismatch: decision signed for {gate_id!r}, "
            f"expected {expected_gate_id!r}",
            resolved_mode, gate_id=gate_id, dept=dept, notify_config=notify_config,
        )

    if expected_dept is not None and dept != expected_dept:
        return _finish(
            False,
            f"dept mismatch: decision signed for {dept!r}, "
            f"expected {expected_dept!r}",
            resolved_mode, gate_id=gate_id, dept=dept, notify_config=notify_config,
        )

    try:
        public_key = load_public_key(public_key_path, stat_fn=stat_fn)
    except Exception as exc:
        return _finish(
            False, f"could not load public key: {exc}", resolved_mode,
            gate_id=gate_id, dept=dept, notify_config=notify_config,
        )

    ok, reason = verify_signature(decision, public_key)
    return _finish(ok, reason, resolved_mode, gate_id=gate_id, dept=dept, notify_config=notify_config)


def _loud_alert(
    reason: str,
    *,
    gate_id: Optional[str],
    dept: Optional[str],
    mode: str,
    notify_config: Optional[Dict[str, Any]],
) -> None:
    """Best-effort LOUD alert for a decision that failed verification but
    was still ALLOWED through (mode="warn"). Independent security review
    finding 4 (2026-09-24): a single WARNING-level log line is easy to
    miss, and this is exactly the "unsigned/forged/tampered/replayed
    decision let through" case the migration period depends on someone
    actually noticing.

    Always logs at ERROR first — this alone never depends on anything
    else succeeding. Then best-effort reuses the fleet's existing
    `scripts/lib/notify.py::notify_alert()` (the framework's shared
    email/Telegram alert path, already vendored to every dept via
    `scripts/vendor-dept-libs.sh`'s MAP) when it's importable, either from
    the console's own tree or a dept's vendored `lib/` package. Without a
    `notify_config` carrying real `accounts`/`notifications` config,
    `notify_alert()` itself raises (no recipient configured) — caught here
    and swallowed, same as any other delivery failure. This function must
    NEVER raise into a `verify_decision()` caller: a broken or unconfigured
    alert channel must not be allowed to break gate execution.
    """
    _log.error(
        "GATE DECISION SIGNATURE CHECK FAILED (mode=%s — ALLOWED THROUGH): "
        "%s [gate_id=%s dept=%s]", mode, reason, gate_id, dept,
    )
    notify_alert = None
    try:
        from scripts.lib import notify as _notify  # framework tree (e.g. the console)
        notify_alert = _notify.notify_alert
    except Exception:
        try:
            from lib import notify as _notify  # vendored dept tree
            notify_alert = _notify.notify_alert
        except Exception:
            return
    try:
        notify_alert(
            reason=f"unverified gate decision ALLOWED (warn mode): {reason}",
            severity="high",
            metadata={"gate_id": gate_id, "dept": dept, "mode": mode},
            config=notify_config or {"dept_label": dept or "decision-signing"},
        )
    except Exception as exc:  # noqa: BLE001 — alert delivery failure must never propagate
        _log.error(
            "decision_signing: loud alert delivery itself failed (%s: %s) — "
            "see the log line above for the original signature failure",
            type(exc).__name__, exc,
        )


def _finish(
    verified: bool,
    reason: str,
    mode: str,
    *,
    gate_id: Optional[str] = None,
    dept: Optional[str] = None,
    notify_config: Optional[Dict[str, Any]] = None,
) -> VerifyResult:
    if verified:
        return VerifyResult(verified=True, allowed=True, reason=reason, mode=mode)
    if mode == "enforce":
        return VerifyResult(verified=False, allowed=False, reason=reason, mode=mode)
    _loud_alert(reason, gate_id=gate_id, dept=dept, mode=mode, notify_config=notify_config)
    return VerifyResult(verified=False, allowed=True, reason=reason, mode=mode)
