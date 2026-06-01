"""
Verify the scripts/lib/state_yaml.py helper appends validated steps + commit
SHA correctly, without losing prior history.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from lib import state_yaml


def test_record_step_appends_to_validated_steps(tmp_path: Path, sample_state_yaml: dict) -> None:
    p = tmp_path / "STATE.yaml"
    p.write_text(yaml.safe_dump(sample_state_yaml, sort_keys=False), encoding="utf-8")
    # `mandate` already validated in fixture. Add `missions`.
    state_yaml.record_validated_step(
        path=p,
        step="missions",
        commit_sha="def5678",
        validated_at="2026-05-20T20:00:00Z",
        validated_by="joris",
    )
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "missions" in doc["validated_steps"]
    assert any(c["step"] == "missions" and c["commit_sha"] == "def5678"
               for c in doc["commits"])
    # Prior step must still be there.
    assert "mandate" in doc["validated_steps"]


def test_record_step_advances_status(tmp_path: Path, sample_state_yaml: dict) -> None:
    """Recording step N should advance status per the 7-state SM."""
    p = tmp_path / "STATE.yaml"
    p.write_text(yaml.safe_dump(sample_state_yaml, sort_keys=False), encoding="utf-8")
    # mandate -> step1 -> status Configuring; recording missions should advance to Drafting.
    state_yaml.record_validated_step(
        path=p,
        step="missions",
        commit_sha="def5678",
        validated_at="2026-05-20T20:00:00Z",
        validated_by="joris",
    )
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc["status"] == "Drafting", \
        f"status should advance Configuring -> Drafting after missions, got {doc['status']!r}"


def test_record_step_is_idempotent(tmp_path: Path, sample_state_yaml: dict) -> None:
    """Recording the same step twice should be a no-op (or at least not duplicate)."""
    p = tmp_path / "STATE.yaml"
    p.write_text(yaml.safe_dump(sample_state_yaml, sort_keys=False), encoding="utf-8")
    state_yaml.record_validated_step(
        path=p,
        step="mandate",
        commit_sha="abc1234",
        validated_at="2026-05-20T19:33:00Z",
        validated_by="joris",
    )
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    # mandate should appear exactly once.
    assert doc["validated_steps"].count("mandate") == 1


def test_init_state_creates_valid_file(tmp_path: Path, schemas_dir: Path) -> None:
    """state_yaml.init_state writes a schema-valid empty STATE.yaml."""
    import jsonschema

    p = tmp_path / "STATE.yaml"
    state_yaml.init_state(
        path=p, slug="alpha", display_name="Alpha", owner="joris",
        created_at="2026-05-20T19:00:00Z",
    )
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    schema = yaml.safe_load((schemas_dir / "state.schema.yaml").read_text(encoding="utf-8"))
    v = jsonschema.Draft7Validator(schema)
    errors = list(v.iter_errors(doc))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]
    assert doc["status"] == "Idea"
    assert doc["validated_steps"] == []
