"""
test_step5_kpi_guardrail_naming.py — Polish Fix 3 (2026-05-21).

Operator-named `kpi_guardrail_set` + `authorization_band`.

Today the gates_kpis runner auto-generates `kpi_guardrail_set = <class>_kpis`
(e.g. `social_post_kpis`) — invisible to the operator. Notion v5 lines
894-918 imply these have meaningful names the operator can review and
override. After approving a policy card, the runner should ask:

  "Comment veux-tu nommer ce jeu de garde-fous KPI ?
   (Par défaut : `<class>_kpis`. Tu peux proposer un nom plus parlant,
   ex: `quality_floor`, `posts_safe_zone`, etc.)"

Empty / "ok" / "défaut" → keep `<class>_kpis`.
Any identifier-shaped string (snake_case ≤30 chars) → use it.
Garbage → silently keep default + add an info note.

Same flow for `authorization_band`.

These become editable later via the "Change …" commands too (mirror the
missions/skills edit pattern).
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from skill_lib.step_runners import Action, get_runner


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Drafting",
        "validated_steps": ["mandate", "missions", "layers", "skills_tools"],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft_with_one_skill_class(tmp_path: Path) -> Path:
    """Seed a draft with a single skill that maps to ONE action class
    (social_post) so the per-class loop has a deterministic shape."""
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "owner": "operator",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "outputs": "drafts de posts, calendrier",
            "forbidden": ["publier sans validation"],
            "success_criteria": ["0 breach"],
            "status": "onboarding",
            "layers": {"subscribed": [1, 2, 3, 4]},
            "skills": {
                "layer_3": ["post-publisher"],  # → social_post class
            },
            "tools": ["linkedin-reader"],
        }
    }, sort_keys=False), encoding="utf-8")
    return draft


def _drive_to_policy_card(tmp_path: Path):
    """Return a runner that has displayed the first policy card and is
    awaiting the operator's approve/edit/refine answer."""
    runner = get_runner("gates_kpis")
    runner.start(tmp_path / "STATE.yaml", tmp_path / "dept.yaml.draft")
    # Substep A → confirm classes list
    runner.next_prompt()
    runner.on_answer("ok")
    # Substep B → policy card shown
    runner.next_prompt()
    return runner


# ----- tests -----


def test_after_policy_approve_runner_asks_for_kpi_name(tmp_path):
    """After approving the policy card, the runner asks the operator to
    name the kpi_guardrail_set (default `<class>_kpis`)."""
    _seed_state(tmp_path)
    _seed_draft_with_one_skill_class(tmp_path)
    runner = _drive_to_policy_card(tmp_path)
    runner.on_answer("approuve")
    p = runner.next_prompt()
    assert p is not None, "runner should ask a follow-up after approve"
    pl = p.lower()
    # Bureau-de-Cadre French: must mention 'nom' + 'kpi' / 'garde-fou'
    assert "nom" in pl, f"prompt missing 'nom': {p!r}"
    assert ("kpi" in pl or "garde-fou" in pl or "garde fou" in pl), (
        f"prompt missing 'kpi' or 'garde-fou': {p!r}"
    )
    # The default should be surfaced.
    assert "social_post_kpis" in p, (
        f"prompt should surface default `social_post_kpis`: {p!r}"
    )


def test_operator_custom_kpi_name_persists_in_draft(tmp_path):
    """Operator answers `quality_floor` → committed policy uses that
    as `kpi_guardrail_set`."""
    _seed_state(tmp_path)
    draft_path = _seed_draft_with_one_skill_class(tmp_path)
    runner = _drive_to_policy_card(tmp_path)
    runner.on_answer("approuve")
    # Now the runner is asking for the kpi name
    runner.on_answer("quality_floor")
    # The runner may also ask for an authorization_band name — answer ok
    # for that one (use default).
    while not runner.is_done():
        p = runner.next_prompt()
        if p is None:
            break
        runner.on_answer("ok")
    body = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    policies = body.get("gate_policies") or {}
    assert "social_post" in policies, (
        f"social_post policy not committed: {list(policies.keys())}"
    )
    assert policies["social_post"]["kpi_guardrail_set"] == "quality_floor", (
        f"kpi_guardrail_set not set by operator: "
        f"{policies['social_post'].get('kpi_guardrail_set')!r}"
    )


def test_operator_empty_keeps_default_kpi_name(tmp_path):
    """Operator answers `ok` / empty → keep default `<class>_kpis`."""
    _seed_state(tmp_path)
    draft_path = _seed_draft_with_one_skill_class(tmp_path)
    runner = _drive_to_policy_card(tmp_path)
    runner.on_answer("approuve")
    runner.on_answer("ok")  # accept default kpi name
    # If band name follow-up exists, also ok.
    while not runner.is_done():
        p = runner.next_prompt()
        if p is None:
            break
        runner.on_answer("ok")
    body = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    policies = body.get("gate_policies") or {}
    assert policies["social_post"]["kpi_guardrail_set"] == "social_post_kpis"


def test_operator_garbage_kpi_name_silently_keeps_default(tmp_path):
    """Operator answers something that's not snake_case ≤30 chars →
    silently keep default; no exception."""
    _seed_state(tmp_path)
    draft_path = _seed_draft_with_one_skill_class(tmp_path)
    runner = _drive_to_policy_card(tmp_path)
    runner.on_answer("approuve")
    # Garbage: contains spaces and special chars + too long
    runner.on_answer("THIS IS NOT a valid identifier!!! it is way too long oh dear")
    while not runner.is_done():
        p = runner.next_prompt()
        if p is None:
            break
        runner.on_answer("ok")
    body = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    policies = body.get("gate_policies") or {}
    assert policies["social_post"]["kpi_guardrail_set"] == "social_post_kpis"


def test_operator_custom_authorization_band_persists(tmp_path):
    """Operator can also override authorization_band slug."""
    _seed_state(tmp_path)
    draft_path = _seed_draft_with_one_skill_class(tmp_path)
    runner = _drive_to_policy_card(tmp_path)
    runner.on_answer("approuve")
    # First follow-up: kpi name (keep default)
    runner.on_answer("ok")
    # Second follow-up should be band name — answer with custom slug
    runner.on_answer("posts_safe_zone")
    # Drain any remaining prompts (next class etc).
    safety = 0
    while not runner.is_done() and safety < 10:
        safety += 1
        p = runner.next_prompt()
        if p is None:
            break
        runner.on_answer("ok")
    body = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    policies = body.get("gate_policies") or {}
    assert (
        policies["social_post"]["authorization_band"] == "posts_safe_zone"
    ), (
        f"authorization_band not set by operator: "
        f"{policies['social_post'].get('authorization_band')!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
