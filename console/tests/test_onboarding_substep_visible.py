"""
test_onboarding_substep_visible.py — Sprint Maya-blocker Fix 2.

Pins the console's reflection of `STATE.yaml::step_progress.<step>.current_substep`
in the operator-facing onboarding view (Notion v5 lines 894-924 for Step 5,
762-781 for the 3-pane layout).

Before this fix: if Joris opened /agents/<slug>/onboarding while Maya was
mid-Step-5 asking about kpi naming, he saw a generic "Step 5 — Les
décisions qu'elle pourra prendre · en cours" with no indication of the
sub-phase. The sub-phase IS persisted in STATE.yaml — the template just
didn't read it.

After: a short Bureau-de-Cadre French sentence is rendered below the
current-step bullet, e.g.:
  - "En ce moment : elle te demande comment nommer le jeu de garde-fous
     KPI pour `social_post`."
  - "En ce moment : elle te demande comment nommer la bande d'autorisation
     pour `social_post`."
  - "En ce moment : elle te propose la gate policy pour `social_post`."
  - "En ce moment : elle te propose la mission `signal_scan_task`." (step 2)
  - "En ce moment : étape en cours." (generic fallback)

Tested via direct unit tests on `humanize_substep` AND via the HTMX
timeline fragment route to verify end-to-end render.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ----- helpers -----


def _seed_state_at_step5_kpi_naming(repo: Path, class_id: str = "social_post"):
    """Mutate the repo's STATE.yaml so Step 5 is mid-`kpi_naming` substep."""
    state_path = repo / "onboarding" / "STATE.yaml"
    doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    doc["status"] = "Drafting"
    doc["validated_steps"] = [
        "mandate", "missions", "layers", "skills_tools",
    ]
    doc.setdefault("step_progress", {})
    doc["step_progress"]["gates_kpis"] = {
        "current_status": "awaiting_validation",
        "classes_confirmed": True,
        "sub_artifacts_validated": [],
        "current_substep": {
            "type": "kpi_naming",
            "draft_payload": {"class_id": class_id},
        },
    }
    state_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _seed_state_at_step5_band_naming(repo: Path, class_id: str = "social_post"):
    state_path = repo / "onboarding" / "STATE.yaml"
    doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    doc["status"] = "Drafting"
    doc["validated_steps"] = [
        "mandate", "missions", "layers", "skills_tools",
    ]
    doc.setdefault("step_progress", {})
    doc["step_progress"]["gates_kpis"] = {
        "current_status": "awaiting_validation",
        "classes_confirmed": True,
        "sub_artifacts_validated": [],
        "current_substep": {
            "type": "band_naming",
            "draft_payload": {"class_id": class_id},
        },
    }
    state_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _seed_state_at_step5_policy_card(repo: Path, class_id: str = "social_post"):
    state_path = repo / "onboarding" / "STATE.yaml"
    doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    doc["status"] = "Drafting"
    doc["validated_steps"] = [
        "mandate", "missions", "layers", "skills_tools",
    ]
    doc.setdefault("step_progress", {})
    doc["step_progress"]["gates_kpis"] = {
        "current_status": "awaiting_validation",
        "classes_confirmed": True,
        "sub_artifacts_validated": [],
        "current_substep": {
            "type": "policy_card",
            "draft_payload": {"class_id": class_id, "refine_variant": 0},
        },
    }
    state_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _seed_state_at_step2_proposing_mission(
    repo: Path, mission_id: str = "signal_scan_task",
):
    state_path = repo / "onboarding" / "STATE.yaml"
    doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    doc["status"] = "Drafting"
    doc["validated_steps"] = ["mandate"]
    doc.setdefault("step_progress", {})
    doc["step_progress"]["missions"] = {
        "current_status": "awaiting_validation",
        "sub_artifacts_validated": [],
        "current_substep": {
            "type": "proposing_mission",
            "draft_payload": {"mission_id": mission_id},
        },
    }
    state_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


# ----- unit tests on humanize_substep -----


def test_humanize_substep_kpi_naming_includes_class_id():
    from console.services.humanize import humanize_substep
    out = humanize_substep({
        "type": "kpi_naming",
        "draft_payload": {"class_id": "social_post"},
    })
    assert "elle te demande" in out.lower()
    assert "nommer" in out.lower()
    assert "kpi" in out.lower() or "garde-fous" in out.lower()
    assert "social_post" in out, f"class_id missing in: {out!r}"


def test_humanize_substep_band_naming_includes_class_id():
    from console.services.humanize import humanize_substep
    out = humanize_substep({
        "type": "band_naming",
        "draft_payload": {"class_id": "social_post"},
    })
    assert "elle te demande" in out.lower()
    assert "bande" in out.lower()
    assert "autoris" in out.lower()
    assert "social_post" in out, f"class_id missing in: {out!r}"


def test_humanize_substep_policy_card_includes_class_id():
    from console.services.humanize import humanize_substep
    out = humanize_substep({
        "type": "policy_card",
        "draft_payload": {"class_id": "social_post"},
    })
    assert "elle te propose" in out.lower()
    assert "gate" in out.lower() or "policy" in out.lower() or "police" in out.lower()
    assert "social_post" in out


def test_humanize_substep_proposing_mission_includes_mission_id():
    from console.services.humanize import humanize_substep
    out = humanize_substep({
        "type": "proposing_mission",
        "draft_payload": {"mission_id": "signal_scan_task"},
    })
    assert "elle te propose" in out.lower()
    assert "mission" in out.lower()
    assert "signal_scan_task" in out


def test_humanize_substep_generic_fallback_unknown_type():
    from console.services.humanize import humanize_substep
    out = humanize_substep({
        "type": "some_brand_new_substep",
        "draft_payload": {"foo": "bar"},
    })
    assert out.lower().startswith("en ce moment")
    # Generic prose, no enum leak
    assert "some_brand_new_substep" not in out


def test_humanize_substep_none_returns_empty():
    from console.services.humanize import humanize_substep
    assert humanize_substep(None) == ""
    assert humanize_substep({}) == ""


# ----- integration tests via the timeline fragment route -----


def test_timeline_fragment_shows_kpi_naming_substep(client, fixture_root):
    """When Step 5 substep is kpi_naming, the timeline fragment surfaces it."""
    repo = fixture_root / "bubble-ops-miranda"
    _seed_state_at_step5_kpi_naming(repo, class_id="social_post")
    r = client.get("/agents/miranda/onboarding/timeline")
    assert r.status_code == 200, r.text
    html = r.text
    # The sub-phase prose appears verbatim
    assert "social_post" in html, f"class_id missing in timeline HTML"
    assert "nommer" in html.lower(), "kpi_naming prose missing"
    assert "garde-fous" in html.lower() or "kpi" in html.lower(), (
        "kpi/garde-fous keyword missing"
    )


def test_timeline_fragment_shows_band_naming_substep(client, fixture_root):
    repo = fixture_root / "bubble-ops-miranda"
    _seed_state_at_step5_band_naming(repo, class_id="social_post")
    r = client.get("/agents/miranda/onboarding/timeline")
    assert r.status_code == 200, r.text
    html = r.text
    assert "social_post" in html
    assert "bande" in html.lower()
    assert "autoris" in html.lower()


def test_timeline_fragment_shows_policy_card_substep(client, fixture_root):
    repo = fixture_root / "bubble-ops-miranda"
    _seed_state_at_step5_policy_card(repo, class_id="social_post")
    r = client.get("/agents/miranda/onboarding/timeline")
    assert r.status_code == 200, r.text
    html = r.text
    assert "social_post" in html
    assert "propose" in html.lower()


def test_timeline_fragment_with_no_substep_shows_no_sub_phase_line(
    client, fixture_root,
):
    """Sanity: when no step_progress.current_substep, no sub-phase blurb."""
    repo = fixture_root / "bubble-ops-miranda"
    # don't seed substep; keep fixture defaults
    r = client.get("/agents/miranda/onboarding/timeline")
    assert r.status_code == 200, r.text
    html = r.text
    # No false positive: the sub-phase marker prose should be absent
    assert "en ce moment" not in html.lower(), (
        f"sub-phase prose appeared without a current_substep: {html!r}"
    )
