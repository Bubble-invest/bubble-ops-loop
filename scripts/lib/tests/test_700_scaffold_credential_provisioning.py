"""
test_700_scaffold_credential_provisioning.py — TDD tests for board card #700.

Real incident (#698, 2026-07-17): Miranda's cutover moved her workspace to
bubble-ops-content but never carried IMAP_PASSWORD_FIRM across. Her
config/newsletter_sources.yaml referenced it, but her secrets.sops.env only
had SOPS_PROVISIONED + NETLIFY_AUTH_TOKEN. Result: AUTHENTICATIONFAILED on
every newsletter run for a day, silently -- a MISSING credential and a
ROTATED one produce the same server error, costing time chasing "who has
the good password" when the key was simply ABSENT.

Fix: scaffold.py gets a provisioning check --
`collect_referenced_credentials(root)` + `check_credential_provisioning(root)`
-- that:
  1. Collects credential-looking env-var NAMES referenced by the workspace's
     own config/*.yaml (and *.yml) files and skills/**/*.md, matching a
     TIGHT allowlist of suffixes/prefixes (*_PASSWORD*, *_TOKEN*, *_KEY*,
     *_SECRET*, IMAP_*, SMTP_*) -- see scaffold.CREDENTIAL_NAME_RE. This is
     deliberately narrow: an ordinary uppercase config value (e.g. a status
     enum like READY or a currency code like USD) must NOT trip the check.
  2. Cross-references those names against the KEY NAMES present in the
     workspace's secrets.sops.env (top-level `KEY=` lines read as text --
     never decrypted, never a VALUE inspected).
  3. On a gap, raises CredentialProvisioningError naming the missing KEYS
     ONLY (never a value).

Tests cover:
  - test_missing_credential_is_caught_red_then_green
      The #698 repro: IMAP_PASSWORD_FIRM referenced in config but absent
      from secrets.sops.env -> check_credential_provisioning raises, naming
      IMAP_PASSWORD_FIRM.
  - test_fully_provisioned_workspace_passes
      Every referenced credential IS present -> no exception (no false
      alarm).
  - test_ordinary_uppercase_value_does_not_trip_check
      A config value like `status: READY` or `region: US_EAST` is NOT a
      credential name and must not appear in collected names / must not
      raise.
  - test_no_secret_value_ever_appears_in_error
      The raised error text contains the missing KEY NAME but never a
      secret VALUE that was present in secrets.sops.env for OTHER keys.
  - test_scaffold_wires_check_into_migration_path
      scaffold() calls the provisioning check when secrets.sops.env
      already exists in root (the cutover/migration case) and propagates
      the failure loudly.

All tests are file-system-only. No network. No GitHub. No SOPS decryption.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path surgery so scaffold.py and its deps are importable (mirrors the other
# scripts/lib/tests/test_scaffold_*.py files in this directory).
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent               # scripts/lib/
SCRIPTS_DIR = SCRIPTS_LIB.parent        # scripts/
PROJECT_ROOT = SCRIPTS_DIR.parent       # bubble-ops-loop/
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"

for p in (str(SKILL_ROOT), str(SCRIPTS_LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

import scaffold  # noqa: E402  (after path surgery)


# ---------------------------------------------------------------------------
# Helpers -- build a minimal tmp workspace shaped like the #698 repro.
# ---------------------------------------------------------------------------

MIRANDA_NEWSLETTER_SOURCES_YAML = """\
sources:
  - name: firm_imap
    type: imap
    host: imap.firm.example.com
    username: newsletter@firm.example.com
    password_env: IMAP_PASSWORD_FIRM
  - name: netlify
    type: webhook
    auth_token_env: NETLIFY_AUTH_TOKEN
status: READY
region: US_EAST
"""


def _make_workspace(tmp_path: Path, *, secrets_keys: list[str]) -> Path:
    """Build a tmp workspace with a config referencing IMAP_PASSWORD_FIRM +
    NETLIFY_AUTH_TOKEN, and a secrets.sops.env containing only `secrets_keys`.
    """
    root = tmp_path / "bubble-ops-content"
    (root / "config").mkdir(parents=True)
    (root / "config" / "newsletter_sources.yaml").write_text(
        MIRANDA_NEWSLETTER_SOURCES_YAML, encoding="utf-8"
    )

    # secrets.sops.env in the real (SOPS-encrypted) shape: `KEY=value` lines,
    # value replaced with an ENC[...] blob to mirror how sops actually
    # encrypts an env-style file. We only ever read the KEY side.
    lines = [f"{k}=ENC[AES256_GCM,data:xxxxx,type:str]" for k in secrets_keys]
    (root / "secrets.sops.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_missing_credential_is_caught_red_then_green(tmp_path: Path):
    """#698 repro: IMAP_PASSWORD_FIRM is referenced but absent from
    secrets.sops.env (only SOPS_PROVISIONED + NETLIFY_AUTH_TOKEN present).
    check_credential_provisioning must raise, naming IMAP_PASSWORD_FIRM.
    """
    root = _make_workspace(
        tmp_path, secrets_keys=["SOPS_PROVISIONED", "NETLIFY_AUTH_TOKEN"]
    )

    with pytest.raises(scaffold.CredentialProvisioningError) as exc_info:
        scaffold.check_credential_provisioning(root)

    message = str(exc_info.value)
    assert "IMAP_PASSWORD_FIRM" in message, (
        f"Expected missing key IMAP_PASSWORD_FIRM to be named in the error, got: {message!r}"
    )
    # NETLIFY_AUTH_TOKEN is present -- must NOT be reported as missing.
    assert "NETLIFY_AUTH_TOKEN" not in exc_info.value.missing_keys


def test_fully_provisioned_workspace_passes(tmp_path: Path):
    """Every referenced credential IS present -> no exception, no false alarm."""
    root = _make_workspace(
        tmp_path,
        secrets_keys=["SOPS_PROVISIONED", "NETLIFY_AUTH_TOKEN", "IMAP_PASSWORD_FIRM"],
    )

    # Must not raise.
    scaffold.check_credential_provisioning(root)


def test_ordinary_uppercase_value_does_not_trip_check(tmp_path: Path):
    """`status: READY` and `region: US_EAST` are ordinary uppercase config
    values, not credential names -- they must not be collected as
    referenced credentials and must not cause a false-positive failure.
    """
    root = _make_workspace(
        tmp_path,
        secrets_keys=["SOPS_PROVISIONED", "NETLIFY_AUTH_TOKEN", "IMAP_PASSWORD_FIRM"],
    )

    referenced = scaffold.collect_referenced_credentials(root)
    assert "READY" not in referenced
    assert "US_EAST" not in referenced
    # Sanity: the real credential names ARE collected.
    assert "IMAP_PASSWORD_FIRM" in referenced
    assert "NETLIFY_AUTH_TOKEN" in referenced

    # And the full check passes cleanly (no false alarm raised).
    scaffold.check_credential_provisioning(root)


def test_no_secret_value_ever_appears_in_error(tmp_path: Path):
    """The raised error must contain only KEY NAMES, never a VALUE -- not
    even a value that belongs to a DIFFERENT (present) key.
    """
    root = _make_workspace(
        tmp_path, secrets_keys=["SOPS_PROVISIONED", "NETLIFY_AUTH_TOKEN"]
    )
    # Plant a distinctive "value" in the present key's line to prove it
    # never leaks even incidentally.
    secrets_path = root / "secrets.sops.env"
    secrets_path.write_text(
        "SOPS_PROVISIONED=ENC[AES256_GCM,data:zzzz,type:str]\n"
        "NETLIFY_AUTH_TOKEN=ENC[AES256_GCM,data:TOTALLY_SECRET_VALUE_XYZ,type:str]\n",
        encoding="utf-8",
    )

    with pytest.raises(scaffold.CredentialProvisioningError) as exc_info:
        scaffold.check_credential_provisioning(root)

    message = str(exc_info.value)
    assert "TOTALLY_SECRET_VALUE_XYZ" not in message
    assert "ENC[" not in message


def test_scaffold_wires_check_into_migration_path(tmp_path: Path):
    """scaffold() must run the provisioning check when secrets.sops.env
    already exists in root (the cutover/migration re-scaffold case), and
    propagate the failure loudly rather than silently completing.
    """
    root = tmp_path / "bubble-ops-content"
    root.mkdir()
    (root / "config").mkdir()
    (root / "config" / "newsletter_sources.yaml").write_text(
        MIRANDA_NEWSLETTER_SOURCES_YAML, encoding="utf-8"
    )
    (root / "secrets.sops.env").write_text(
        "SOPS_PROVISIONED=ENC[AES256_GCM,data:xxxx,type:str]\n"
        "NETLIFY_AUTH_TOKEN=ENC[AES256_GCM,data:xxxx,type:str]\n",
        encoding="utf-8",
    )

    with pytest.raises(scaffold.CredentialProvisioningError) as exc_info:
        scaffold.scaffold(
            root=root,
            slug="content",
            display_name="Content",
            owner="operator",
            level="ops",
            children=[],
        )
    assert "IMAP_PASSWORD_FIRM" in str(exc_info.value)


def test_scaffold_skips_check_when_no_secrets_file_yet(tmp_path: Path):
    """A brand-new (never-provisioned) scaffold has no secrets.sops.env yet
    -- that's the normal bootstrap order (onboarding happens before secrets
    are provisioned). The check must not fire in that case; scaffold()
    must complete normally.
    """
    root = tmp_path / "bubble-ops-smoke"
    root.mkdir()
    scaffold.scaffold(
        root=root,
        slug="smoke",
        display_name="Smoke",
        owner="operator",
        level="ops",
        children=[],
    )
    assert (root / "CLAUDE.md").exists()
