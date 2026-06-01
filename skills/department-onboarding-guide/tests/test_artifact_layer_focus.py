"""
test_artifact_layer_focus.py — Refonte #2 of 3, Deliverable D.

Pins the behavior of the per-layer PROMPT.md tester
(`test_layer_focus`) used by the Step 3 runner to gate every
APPROVE_SUBSTEP before declaring a layer ready.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skill_lib.artifact_tests import test_artifact
from skill_lib.artifact_tests.layer_focus import test_layer_focus


def _write_prompt(tmp_path: Path, layer: int, body: str) -> Path:
    d = tmp_path / "layers" / str(layer)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "PROMPT.md"
    p.write_text(body, encoding="utf-8")
    return p


def _good_prompt_md(layer: int) -> str:
    return (
        f"# Layer {layer} — La recherche\n\n"
        "## Mission générique\n\n"
        "C'est le moment où je transforme les signaux en idées exploitables "
        "(drafts, plans, variantes).\n\n"
        "## Focalisation pour ce département\n\n"
        "Pour Miranda, à ce moment-là, je vais transformer chaque "
        "content_idea_task en 2-3 drafts de post, générer des angles "
        "alternatifs et créer un gate avant publication.\n\n"
        "## Outputs\n\n"
        "- `outputs/<date>/2/research/*.md`\n"
        "- `queues/gates/<id>.yaml`\n"
    )


def test_passes_on_canonical_prompt_md(tmp_path):
    p = _write_prompt(tmp_path, 2, _good_prompt_md(2))
    payload = {
        "layer": 2,
        "focus_md": "Pour Miranda, à ce moment-là...",
        "prompt_md_path": p,
    }
    r = test_layer_focus(payload, {"dept_root": tmp_path})
    assert r.passed, r.summary_md


def _good_prompt_md_layer1(tmp_path: Path) -> Path:
    body = (
        "# Layer 1 — Data Update\n\n"
        "## Mission générique\n\n"
        "Refresh data externe + interne, lire les recurring missions dues, "
        "puis matérialiser les besoins du jour en queue items. "
        "Cadence par défaut : 06:00 UTC daily.\n\n"
        "## Focalisation pour ce département\n\n"
        "Pour Miranda, à 06:00 je scanne LinkedIn + wiki, je matérialise "
        "les content_idea_task dans queues/research/.\n\n"
        "## Outputs\n\n- `outputs/<date>/1/plan.md`\n- `queues/research/`\n"
    )
    d = tmp_path / "layers" / "1"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "PROMPT.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_dispatcher_routes_to_layer_focus(tmp_path):
    p = _good_prompt_md_layer1(tmp_path)
    payload = {"layer": 1, "focus_md": "Focalisation Layer 1.", "prompt_md_path": p}
    r = test_artifact("layer_focus", payload, {"dept_root": tmp_path})
    assert r.passed, r.summary_md


def test_fails_when_file_missing(tmp_path):
    payload = {
        "layer": 1,
        "focus_md": "anything",
        "prompt_md_path": tmp_path / "layers" / "1" / "PROMPT.md",
    }
    r = test_layer_focus(payload, {"dept_root": tmp_path})
    assert r.passed is False
    assert any("existe" in i.lower() or "missing" in i.lower() or "trouv" in i.lower()
               for i in r.issues)


def test_fails_on_empty_file(tmp_path):
    p = _write_prompt(tmp_path, 1, "")
    payload = {"layer": 1, "focus_md": "x", "prompt_md_path": p}
    r = test_layer_focus(payload, {"dept_root": tmp_path})
    assert r.passed is False
    assert any("vide" in i.lower() or "court" in i.lower() or "200" in i for i in r.issues)


def test_fails_when_no_outputs_section(tmp_path):
    body = "# Layer 1\n\n" + ("a " * 200)  # long enough, but no Outputs
    p = _write_prompt(tmp_path, 1, body)
    payload = {"layer": 1, "focus_md": "x", "prompt_md_path": p}
    r = test_layer_focus(payload, {"dept_root": tmp_path})
    assert r.passed is False
    assert any("output" in i.lower() for i in r.issues)


def test_fails_when_generic_description_missing(tmp_path):
    # Body contains Outputs but lacks any Layer-N doctrinal one-liner.
    body = (
        "# Random title\n\n"
        "Some focalisation block here, no doctrinal one-liner whatsoever.\n\n"
        "## Outputs\n\n- foo\n" + ("a " * 100)
    )
    p = _write_prompt(tmp_path, 1, body)
    payload = {"layer": 1, "focus_md": "x", "prompt_md_path": p}
    r = test_layer_focus(payload, {"dept_root": tmp_path})
    assert r.passed is False
    assert any("générique" in i.lower() or "doctrin" in i.lower() or "layer" in i.lower()
               for i in r.issues)


def test_summary_md_uses_bureau_de_cadre_french(tmp_path):
    p = _write_prompt(tmp_path, 2, _good_prompt_md(2))
    payload = {"layer": 2, "focus_md": "x", "prompt_md_path": p}
    r = test_layer_focus(payload, {"dept_root": tmp_path})
    # FR — must mention "Layer 2" and use French verb forms.
    assert "Layer 2" in r.summary_md
    assert "validé" in r.summary_md.lower() or "ok" in r.summary_md.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
