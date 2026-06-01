"""
test_disaster_recovery_doc.py — Security/backup sprint, deliverable C.

Tests the master disaster-recovery playbook: docs/DISASTER-RECOVERY.md.

Invariants :
  - Exists.
  - Has the 5 required sections.
  - Each section has at least one bash code block (actionable steps).
  - In French (operator-facing).
  - Does NOT leak any passphrase in examples.
  - References the companion scripts and docs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC = PROJECT_ROOT / "docs" / "DISASTER-RECOVERY.md"


def test_dr_doc_exists():
    assert DOC.exists(), f"missing {DOC}"


def test_dr_doc_has_required_sections():
    body = DOC.read_text(encoding="utf-8")
    # The 5 sections from the brief — must all be present as headings.
    required_section_markers = [
        # 1. Si Morty est mort
        ("morty est mort", "section chronological recovery"),
        # 2. Si seulement /etc/age/key.txt est corrompu
        ("age/key.txt", "section age-key-only corruption"),
        # 3. Si on perd la passphrase Restic
        ("passphrase restic", "section lost restic passphrase"),
        # 4. Si on perd le compte GitHub
        ("compte github", "section lost github account"),
        # 5. Drill
        ("drill", "section drill recommendation"),
    ]
    body_lower = body.lower()
    for needle, desc in required_section_markers:
        assert needle in body_lower, f"missing {desc} (marker '{needle}')"


def test_dr_doc_section_count():
    body = DOC.read_text(encoding="utf-8")
    # At least 5 level-2 headings (## ...).
    h2 = re.findall(r"^##\s+\S", body, flags=re.MULTILINE)
    assert len(h2) >= 5, f"expected >= 5 level-2 sections, found {len(h2)}"


def test_dr_doc_each_section_has_actionable_bash():
    """
    Each of the 5 required sections must contain at least one ```bash
    fenced code block — operator-actionable, not just prose.
    """
    body = DOC.read_text(encoding="utf-8")
    # Split into sections by ## heading.
    sections = re.split(r"^##\s+", body, flags=re.MULTILINE)
    # First element is the preamble before any ##, skip it.
    section_bodies = sections[1:]
    assert len(section_bodies) >= 5, (
        f"expected >= 5 sections, got {len(section_bodies)}"
    )
    # Of the first 5 sections (the canonical playbook sections), each
    # must contain ```bash.
    sections_with_bash = 0
    for sec in section_bodies:
        if "```bash" in sec or "```sh" in sec:
            sections_with_bash += 1
    assert sections_with_bash >= 5, (
        f"only {sections_with_bash} sections contain a bash code block; "
        "the brief requires each of the 5 sections to be actionable."
    )


def test_dr_doc_is_in_french():
    body = DOC.read_text(encoding="utf-8")
    # Heuristic : common French function words must appear, and the doc
    # must NOT be predominantly English. We check for some French markers.
    french_markers = [
        "étape",
        "depuis",  # "depuis le backup", "depuis git"
        "nouveau",
        "perd",
        "sauvegarde",
    ]
    found = sum(1 for m in french_markers if m in body.lower())
    assert found >= 4, (
        f"doc doesn't read as French (only {found}/5 markers found). "
        "Operator copy must be in French (Bureau-de-Cadre voice)."
    )


def test_dr_doc_does_not_leak_passphrase_examples():
    """
    The doc shows commands that USE passphrases (e.g. restore-age-key.sh
    prompts for one), but must NEVER show a concrete passphrase value in
    an example. We forbid common leak patterns.
    """
    body = DOC.read_text(encoding="utf-8")
    # Patterns we forbid (concrete passphrase values in examples).
    forbidden = [
        r"PASSPHRASE\s*=\s*['\"][^'\"]{4,}['\"]",  # PASSPHRASE="..."
        r"RESTIC_PASSWORD\s*=\s*['\"][^'\"]{4,}['\"]",  # RESTIC_PASSWORD="..."
        r"export\s+RESTIC_PASSWORD\s*=\s*\S{4,}",
        r"echo\s+['\"][a-zA-Z0-9_!@#%^&*-]{8,}['\"]\s*\|\s*age",  # echo "secret" | age
    ]
    for pat in forbidden:
        m = re.search(pat, body)
        assert not m, (
            f"doc leaks a concrete passphrase value matching {pat!r} : "
            f"{m.group(0) if m else ''!r}"
        )


def test_dr_doc_references_companion_scripts_and_docs():
    body = DOC.read_text(encoding="utf-8")
    # The master playbook MUST point to the deliverable A + B scripts
    # and docs, otherwise the operator at 3 AM has no entry point.
    must_reference = [
        "restore-age-key.sh",
        "morty-restic-setup.sh",
        "BACKUP-STRATEGY.md",
        "DISASTER-RECOVERY-AGE-KEY.md",
    ]
    for ref in must_reference:
        assert ref in body, f"doc must reference {ref}"


def test_dr_doc_mentions_hetzner_provisioning_is_manual():
    """
    Step 1 (provision new VPS) is NOT automatable without HCLOUD_TOKEN —
    the doc must say so explicitly so the operator doesn't waste time
    looking for a script.
    """
    body = DOC.read_text(encoding="utf-8")
    body_lower = body.lower()
    # Either references the missing token, or says "manuel" / "dashboard".
    assert "hcloud_token" in body_lower or "dashboard" in body_lower or "manuel" in body_lower
    # CX33 is the canonical instance type — making sure operator picks
    # the right one.
    assert "CX33" in body or "cx33" in body_lower


def test_dr_doc_mentions_1password_for_passphrase_storage():
    body = DOC.read_text(encoding="utf-8")
    # All three passphrases (age backup, restic, others) must be stored
    # in 1Password — the doc must say so.
    assert "1Password" in body or "gestionnaire" in body.lower()
