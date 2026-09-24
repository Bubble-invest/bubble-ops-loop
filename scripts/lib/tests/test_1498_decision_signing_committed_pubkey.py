"""#1498 — the COMMITTED decision-signing public key actually parses.

Board #1476 / PR #492 shipped `scripts/lib/decision_signing.py` and the
runbook to commit its public key; board #1498 does that commit:
`scripts/keys/decision-signing-ed25519.pub.pem`. This module never touches
the matching PRIVATE key (SOPS-encrypted, VPS-only, out of this repo) — it
only asserts that `load_public_key()` (the same function every executor's
`verify_decision()` call ultimately uses) can load THIS EXACT committed
file as a valid Ed25519 public key, and that its content matches what board
#1498's card specifies byte-for-byte.

Uses `path=` explicit (not the fixed `/opt/bubble-ops-loop/...` default),
which per `load_public_key()`'s own docstring skips the root-ownership walk
— appropriate here since a CI/dev checkout of this repo is not root-owned,
and this test isn't exercising that ownership check (see
test_1476_decision_signing.py for that).

Run: python3 -m pytest scripts/lib/tests/test_1498_decision_signing_committed_pubkey.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib import decision_signing as ds  # noqa: E402

PUBKEY_PATH = PROJECT_ROOT / "scripts" / "keys" / "decision-signing-ed25519.pub.pem"

# Exact content specified by board #1498's card — pinned here so a future
# accidental edit/re-wrap of the committed file is caught as a test failure,
# not silently shipped.
EXPECTED_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAwKEC4YKFDchTHlD7taUY62AXv4NTU/r6Ivu8ehbnlgI=\n"
    "-----END PUBLIC KEY-----\n"
)


def test_committed_pubkey_file_exists():
    assert PUBKEY_PATH.is_file(), f"missing committed public key at {PUBKEY_PATH}"


def test_committed_pubkey_content_matches_card_exactly():
    assert PUBKEY_PATH.read_text(encoding="utf-8") == EXPECTED_PEM


def test_committed_pubkey_is_not_vendored_in_the_map():
    """scripts/keys/README.md and decision_signing.py's own module docstring
    are explicit: this file must NOT be added to
    scripts/vendor-dept-libs.sh's MAP (a dept-writable vendored copy would
    be a trust-anchor the gated uid could overwrite). Guard that here so a
    future PR adding it there fails CI instead of silently reopening the
    hole PR #492's security review closed."""
    vendor_script = PROJECT_ROOT / "scripts" / "vendor-dept-libs.sh"
    content = vendor_script.read_text(encoding="utf-8")
    map_start = content.index("\nMAP=(")
    map_end = content.index(")", map_start)
    map_block = content[map_start:map_end]
    assert "decision-signing-ed25519.pub.pem" not in map_block
    assert "decision-signing" not in map_block


def test_load_public_key_parses_the_committed_file():
    """The exact function every executor's verify_decision() call resolves
    to (in production, via DEFAULT_PUBLIC_KEY_PATH) can load this exact
    committed file."""
    key = ds.load_public_key(PUBKEY_PATH)
    assert isinstance(key, Ed25519PublicKey)


def test_committed_pubkey_matches_expected_raw_bytes():
    """Cross-check via the cryptography lib directly (independent of
    load_public_key()'s own parsing), and pin the raw 32-byte Ed25519 public
    key value so any accidental key-swap is caught."""
    key = serialization.load_pem_public_key(PUBKEY_PATH.read_bytes())
    assert isinstance(key, Ed25519PublicKey)
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    assert len(raw) == 32
    assert raw.hex() == "c0a102e182850dc8531e50fbb5a518eb6017bf835353fafa22fbbc7a16e79602"
