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


# ---------------------------------------------------------------------------
# Independent security review (2026-09-24, PR #492) — Finding 2:
# selected_option / selected_option_label must be signed (choose decisions)
# ---------------------------------------------------------------------------

def _choose_decision() -> dict:
    d = _base_decision()
    d["action"] = "choose"
    d["selected_option"] = "opt-a"
    d["selected_option_label"] = "Roll forward the hedge"
    return d


def test_choose_decision_signs_and_verifies_selected_option(keypair):
    priv, pub, _, _ = keypair
    signed = ds.sign_decision(_choose_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    ok, reason = ds.verify_signature(signed, pub)
    assert ok is True
    assert reason == "ok"


@pytest.mark.parametrize("field,new_value", [
    ("selected_option", "opt-b"),
    ("selected_option_label", "Close the whole position"),
])
def test_tampered_selected_option_fails_verification(keypair, field, new_value):
    """The exact gap the independent review flagged: a validly-signed
    `choose` decision must NOT still verify after its actual chosen option
    (or label) is changed post-write."""
    priv, pub, _, _ = keypair
    signed = ds.sign_decision(_choose_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    tampered = dict(signed)
    tampered[field] = new_value
    ok, reason = ds.verify_signature(tampered, pub)
    assert ok is False


def test_stripped_selected_option_fails_verification(keypair):
    """Deleting the key entirely (not just changing its value) must also
    invalidate the signature — otherwise an attacker could dodge the value
    check by removing the field instead of editing it."""
    priv, pub, _, _ = keypair
    signed = ds.sign_decision(_choose_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    tampered = dict(signed)
    del tampered["selected_option"]
    ok, reason = ds.verify_signature(tampered, pub)
    assert ok is False


def test_approve_decision_without_selected_option_still_signs(keypair):
    """Regression guard: approve/reject/defer decisions never carry
    selected_option — making it part of SIGNED_FIELDS canonicalization must
    not require it to be present."""
    priv, pub, _, _ = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    assert "selected_option" not in signed
    ok, reason = ds.verify_signature(signed, pub)
    assert ok is True


def test_verify_decision_tampered_selected_option_end_to_end(tmp_path, keypair):
    priv, pub, _, pub_path = keypair
    signed = ds.sign_decision(_choose_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    signed["selected_option"] = "opt-b"  # tampered post-signing
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    result = ds.verify_decision(decision_path, gate_path, public_key_path=pub_path, mode="enforce")
    assert result.verified is False
    assert result.allowed is False


# ---------------------------------------------------------------------------
# Independent security review — Finding 3: expected_gate_id, symmetric with
# expected_dept
# ---------------------------------------------------------------------------

def test_verify_decision_expected_gate_id_mismatch_fails(tmp_path, keypair):
    priv, pub, _, pub_path = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    result = ds.verify_decision(
        decision_path, gate_path, public_key_path=pub_path, mode="enforce",
        expected_gate_id="some-other-gate-id",
    )
    assert result.allowed is False
    assert "gate_id mismatch" in result.reason


def test_verify_decision_expected_gate_id_match_passes(tmp_path, keypair):
    priv, pub, _, pub_path = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    result = ds.verify_decision(
        decision_path, gate_path, public_key_path=pub_path, mode="enforce",
        expected_gate_id=signed["gate_id"],
    )
    assert result.verified is True
    assert result.allowed is True


# ---------------------------------------------------------------------------
# Independent security review — Finding 1: public key path must NOT resolve
# relative to __file__, plus a runtime ownership check on the resolved
# default/env path. Every scenario below uses an injectable stat_fn so it
# needs no real root privileges on the test runner.
# ---------------------------------------------------------------------------

class _FakeStatResult:
    def __init__(self, st_uid: int, st_mode: int):
        self.st_uid = st_uid
        self.st_mode = st_mode


def _stat_map(overrides: dict, default=(0, 0o755)):
    """Return a stat_fn: paths in `overrides` (Path -> (uid, mode)) get that
    result; anything else defaults to root-owned, mode 0o755 (i.e. "safe"),
    so tests only need to describe the ancestor(s) they actually care about
    instead of the whole real filesystem chain up to `/`."""
    overrides = {Path(k): v for k, v in overrides.items()}

    def fn(path):
        p = Path(path)
        uid, mode = overrides.get(p, default)
        return _FakeStatResult(uid, mode)

    return fn


def _write_pem(path: Path, pub) -> None:
    path.write_bytes(pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))


def test_default_public_key_path_is_not_file_relative():
    """The exact regression the review caught: the default must be a fixed
    string, never computed from this module's own __file__ location (which
    is vendored into agent-writable dept checkouts)."""
    assert isinstance(ds.DEFAULT_PUBLIC_KEY_PATH, str)
    assert ds.DEFAULT_PUBLIC_KEY_PATH.startswith("/opt/bubble-ops-loop/")


def test_check_ownership_safe_all_root_owned_passes(tmp_path):
    target = tmp_path / "a" / "b" / "pub.pem"
    stat_fn = _stat_map({})  # everything defaults to root:0755 == safe
    ok, reason = ds._check_ownership_safe(target, stat_fn=stat_fn)
    assert ok is True


def test_check_ownership_safe_non_root_file_owner_fails(tmp_path):
    target = tmp_path / "keys" / "pub.pem"
    stat_fn = _stat_map({target: (1500, 0o644)})  # file itself owned by a non-root uid
    ok, reason = ds._check_ownership_safe(target, stat_fn=stat_fn)
    assert ok is False
    assert "uid 1500" in reason


def test_check_ownership_safe_group_writable_parent_dir_fails(tmp_path):
    """The exact vendoring hole from the review: the FILE can be
    root-owned while its containing directory is agent-owned and
    group/other-writable (0775) — a uid owning that directory can
    unlink()+recreate the file regardless of the file's own ownership."""
    parent = tmp_path / "scripts" / "keys"
    target = parent / "pub.pem"
    stat_fn = _stat_map({parent: (1500, 0o775)})
    ok, reason = ds._check_ownership_safe(target, stat_fn=stat_fn)
    assert ok is False
    assert str(parent) in reason


def test_check_ownership_safe_missing_ancestor_not_treated_as_unsafe(tmp_path):
    target = tmp_path / "does" / "not" / "exist" / "pub.pem"

    def stat_fn(path):
        raise FileNotFoundError(path)

    ok, reason = ds._check_ownership_safe(target, stat_fn=stat_fn)
    assert ok is True


def test_load_public_key_explicit_path_skips_ownership_check(tmp_path, keypair):
    """An explicitly-passed path (a caller's own code-level decision) is
    trusted as-is — same behavior all 19 pre-existing tests already rely
    on, using tmp_path files owned by the test-runner uid, not root."""
    _, pub, _, pub_path = keypair
    loaded = ds.load_public_key(pub_path)  # real os.stat, no stat_fn override
    assert loaded.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ) == pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_load_public_key_default_path_refuses_unsafe_ownership(tmp_path, keypair, monkeypatch):
    _, pub, _, _ = keypair
    fake_dir = tmp_path / "scripts" / "keys"
    fake_dir.mkdir(parents=True)
    fake_path = fake_dir / "decision-signing-ed25519.pub.pem"
    _write_pem(fake_path, pub)

    monkeypatch.setattr(ds, "DEFAULT_PUBLIC_KEY_PATH", str(fake_path))
    monkeypatch.delenv(ds.TEST_MODE_ENV, raising=False)
    monkeypatch.delenv(ds.PUBLIC_KEY_PATH_ENV, raising=False)

    # Simulate the exact vendoring hole: the containing dir is agent-owned.
    stat_fn = _stat_map({fake_dir: (1500, 0o775)})
    with pytest.raises(ds.DecisionSigningError):
        ds.load_public_key(stat_fn=stat_fn)


def test_load_public_key_default_path_accepts_safe_ownership(tmp_path, keypair, monkeypatch):
    _, pub, _, _ = keypair
    fake_dir = tmp_path / "opt" / "bubble-ops-loop" / "scripts" / "keys"
    fake_dir.mkdir(parents=True)
    fake_path = fake_dir / "decision-signing-ed25519.pub.pem"
    _write_pem(fake_path, pub)

    monkeypatch.setattr(ds, "DEFAULT_PUBLIC_KEY_PATH", str(fake_path))
    monkeypatch.delenv(ds.TEST_MODE_ENV, raising=False)
    monkeypatch.delenv(ds.PUBLIC_KEY_PATH_ENV, raising=False)

    stat_fn = _stat_map({})  # everything defaults to root:0755
    loaded = ds.load_public_key(stat_fn=stat_fn)
    assert loaded.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ) == pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_public_key_env_override_ignored_without_test_mode(tmp_path, keypair, monkeypatch):
    """Independent review finding 1: an agent-settable env var must NOT be
    able to redirect trust to a key it controls — unless test mode is
    explicitly on. Without DECISION_SIGNING_TEST_MODE=1, the env var is
    ignored and resolution falls through to DEFAULT_PUBLIC_KEY_PATH."""
    _, _, _, pub_path = keypair
    monkeypatch.delenv(ds.TEST_MODE_ENV, raising=False)
    monkeypatch.setenv(ds.PUBLIC_KEY_PATH_ENV, str(pub_path))
    monkeypatch.setattr(ds, "DEFAULT_PUBLIC_KEY_PATH", "/definitely/not/the/env/path.pem")

    resolved = ds._resolve_public_key_path(None)
    assert resolved == Path("/definitely/not/the/env/path.pem")


def test_public_key_env_override_honored_with_test_mode(tmp_path, keypair, monkeypatch):
    _, _, _, pub_path = keypair
    monkeypatch.setenv(ds.TEST_MODE_ENV, "1")
    monkeypatch.setenv(ds.PUBLIC_KEY_PATH_ENV, str(pub_path))

    resolved = ds._resolve_public_key_path(None)
    assert resolved == Path(pub_path)


def test_verify_decision_unsafe_default_pubkey_ownership_warn_vs_enforce(tmp_path, keypair, monkeypatch):
    """End-to-end: verify_decision() threads stat_fn through to
    load_public_key() and degrades an unsafe default key path through the
    SAME warn/enforce gate as every other failure mode."""
    priv, pub, _, _ = keypair
    signed = ds.sign_decision(_base_decision(), gate_content=GATE_CONTENT, dept="ben", private_key=priv)
    decision_path = _write(tmp_path, "decision.yaml", yaml.safe_dump(signed))
    gate_path = _write(tmp_path, "gate.yaml", GATE_CONTENT)

    fake_dir = tmp_path / "scripts" / "keys"
    fake_dir.mkdir(parents=True)
    fake_pub_path = fake_dir / "decision-signing-ed25519.pub.pem"
    _write_pem(fake_pub_path, pub)

    monkeypatch.setattr(ds, "DEFAULT_PUBLIC_KEY_PATH", str(fake_pub_path))
    monkeypatch.delenv(ds.TEST_MODE_ENV, raising=False)
    monkeypatch.delenv(ds.PUBLIC_KEY_PATH_ENV, raising=False)
    unsafe_stat_fn = _stat_map({fake_dir: (1500, 0o775)})

    warn_result = ds.verify_decision(decision_path, gate_path, mode="warn", stat_fn=unsafe_stat_fn)
    assert warn_result.verified is False
    assert warn_result.allowed is True

    enforce_result = ds.verify_decision(decision_path, gate_path, mode="enforce", stat_fn=unsafe_stat_fn)
    assert enforce_result.verified is False
    assert enforce_result.allowed is False


# ---------------------------------------------------------------------------
# Independent security review — Finding 4: warn mode alerts loudly, not just
# a log line
# ---------------------------------------------------------------------------

def test_loud_alert_never_raises_without_notify_config(caplog):
    """No notify_config, and no real dept accounts configured anywhere —
    notify_alert() itself raises (no recipient), which must be swallowed.
    The always-on ERROR log line is the floor guarantee."""
    import logging
    with caplog.at_level(logging.ERROR, logger="decision_signing"):
        ds._loud_alert(
            "signature does not verify (tampered field or forged decision)",
            gate_id="trade-proposal-ACGL", dept="ben", mode="warn", notify_config=None,
        )
    assert any("SIGNATURE CHECK FAILED" in r.message for r in caplog.records)


def test_finish_warn_mode_invokes_loud_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(ds, "_loud_alert", lambda reason, **kw: calls.append((reason, kw)))
    result = ds._finish(False, "boom", "warn", gate_id="g1", dept="ben", notify_config={"x": 1})
    assert result.allowed is True
    assert len(calls) == 1
    reason, kw = calls[0]
    assert reason == "boom"
    assert kw["gate_id"] == "g1"
    assert kw["dept"] == "ben"
    assert kw["notify_config"] == {"x": 1}


def test_finish_enforce_mode_does_not_invoke_loud_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(ds, "_loud_alert", lambda reason, **kw: calls.append((reason, kw)))
    result = ds._finish(False, "boom", "enforce", gate_id="g1", dept="ben")
    assert result.allowed is False
    assert calls == []


def test_finish_verified_true_does_not_invoke_loud_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(ds, "_loud_alert", lambda reason, **kw: calls.append((reason, kw)))
    result = ds._finish(True, "ok", "warn")
    assert result.allowed is True
    assert calls == []
