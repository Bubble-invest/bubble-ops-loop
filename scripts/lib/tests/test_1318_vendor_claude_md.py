"""#1318 — single-source vendoring of fleet CLAUDE.md doctrine snippets.

Verifies the anti-drift contract: one canonical snippet -> every CLAUDE.md, and a
reverted/hand-edited copy is caught by `check` (the #917/#1314 failure mode).
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MOD_PATH = REPO / "scripts" / "vendor_claude_md.py"
SNIPPETS = REPO / "shared" / "snippets"
MANIFEST = REPO / "fleet" / "claude-md-targets.yaml"


def _load():
    spec = importlib.util.spec_from_file_location("vendor_claude_md", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_canonical_snippets_exist():
    assert (SNIPPETS / "operator-alignment.md").is_file()
    assert (SNIPPETS / "session-start-reads.md").is_file()


def test_operator_alignment_carries_all_four_directives():
    txt = (SNIPPETS / "operator-alignment.md").read_text().lower()
    # the 4 approved directives + intent-consult, and the north-star linkage
    assert "operator-intent" in txt
    assert "trace the decision to the intent" in txt
    assert "reuse existing fleet access" in txt
    assert "simplification" in txt
    assert "fleet standard for nearly everything" in txt
    assert "north-star" in txt


def test_apply_is_idempotent_and_check_passes(tmp_path):
    mod = _load()
    f = tmp_path / "CLAUDE.md"
    f.write_text("# Some agent\n\nExisting doctrine.\n")
    assert mod.apply_file(f, ["operator-alignment"]) is True
    once = f.read_text()
    # applying again changes nothing (idempotent)
    assert mod.apply_file(f, ["operator-alignment"]) is False
    assert f.read_text() == once
    # exactly one block
    assert once.count("BEGIN VENDORED:operator-alignment") == 1
    assert mod.check_file(f, ["operator-alignment"]) == []


def test_drift_is_caught(tmp_path):
    mod = _load()
    f = tmp_path / "CLAUDE.md"
    f.write_text("# Agent\n")
    mod.apply_file(f, ["operator-alignment"])
    # tamper inside the block (a reverted/hand-edited copy)
    tampered = f.read_text().replace("Reuse existing fleet access", "IGNORE fleet access")
    f.write_text(tampered)
    probs = mod.check_file(f, ["operator-alignment"])
    assert any("DRIFTED" in p for p in probs)


def test_absent_block_is_reported(tmp_path):
    mod = _load()
    f = tmp_path / "CLAUDE.md"
    f.write_text("# Agent with no vendored block\n")
    probs = mod.check_file(f, ["operator-alignment"])
    assert any("ABSENT" in p for p in probs)


def test_manifest_only_references_existing_snippets():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(MANIFEST.read_text())
    for t in data["targets"]:
        for name in t["snippets"]:
            assert (SNIPPETS / f"{name}.md").is_file(), f"{t['agent']} -> missing snippet {name}"


def test_cli_apply_and_check_roundtrip(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("# Agent\n")
    r = subprocess.run(
        [sys.executable, str(MOD_PATH), "apply", "--snippet", "operator-alignment", str(f)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run(
        [sys.executable, str(MOD_PATH), "check", "--snippet", "operator-alignment", str(f)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
