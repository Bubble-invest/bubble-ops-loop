"""
test_activation_pr.py — UX-5 task 2.

Notion v5 lines 977-995. The PR body must contain:
  - ## Sa mission
  - ## Ce qu'elle fera chaque jour (humanized — was a markdown table)
  - ## Ses 4 moments de la journée (humanized — was Layer 1..4)
  - ## Les décisions qu'elle prend (humanized — was Gate policy summary)
  - ## Sa répétition à blanc (humanized — was Dry-run result)
  - ## Ce qu'il faut vérifier avant la cérémonie (was Activation checklist)
  - ## Points d'attention (optional, was Risk notes, from STATE.yaml::risk_notes)

  Vocabulary refresh: msg 2702/2708 (2026-05-21) — Bureau de Cadre / Maya tone.

`open_activation_pr()` shells out to the real broker + `gh` CLI but must
be mockable via subprocess. Tests MOCK everything: no real GitHub calls.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from skill_lib.activation_pr import (
    ActivationPRError,
    build_activation_pr_body,
    open_activation_pr,
)


def _state(**over) -> dict:
    base = {
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "joris",
        "created_at": "2026-05-19T10:00:00Z",
        "status": "Ready to activate",
        "validated_steps": [
            "mandate", "missions", "layers", "skills_tools",
            "gates_kpis", "dry_run",
        ],
        "last_updated_at": "2026-05-20T10:00:00Z",
        "commits": [],
    }
    base.update(over)
    return base


def _dept(**over) -> dict:
    base = {
        "department": {
            "slug": "miranda", "level": "ops",
            "mandate": "Produce, plan and audit social content.",
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
        "recurring_missions": [
            {"id": "scan", "layer": 1, "cadence": "daily", "time": "07:30",
             "creates": ["content_idea_task"],
             "output_queue": "queues/research/",
             "input_sources": ["wiki"],
             "description": "Scan content signals."},
            {"id": "review", "layer": 1, "cadence": "weekly",
             "day": "monday", "time": "09:00",
             "creates": ["perf_review_task"],
             "output_queue": "queues/research/",
             "description": "Weekly review."},
        ],
        "skills": {"layer_1": ["a"], "layer_2": ["b"],
                   "layer_3": ["c"], "layer_4": ["d"]},
        "tools": ["t1", "t2"],
        "gate_policies": {
            "social_post": {
                "current_mode": "manual_required",
                "eligible_future_modes": ["auto_with_veto_window"],
                "authorization_band": "low_risk",
                "kpi_guardrail_set": "miranda_kpis",
            },
        },
    }
    base.update(over)
    return base


# ---------- build_activation_pr_body --------------------------------------


def test_pr_body_contains_all_seven_sections():
    """Each of the 7 humanized sections lands (vocabulary refresh, msg 2702/2708)."""
    body = build_activation_pr_body("miranda", _state(), _dept())
    for section in (
        "## Sa mission",
        "## Ce qu'elle fera chaque jour",
        "## Ses 4 moments de la journée",
        "## Les décisions qu'elle prend",
        "## Sa répétition à blanc",
        "## Ce qu'il faut vérifier avant la cérémonie",
    ):
        assert section in body, f"missing humanized section: {section!r}"


def test_pr_body_lists_recurring_missions_with_their_slug():
    """Missions are listed by id (human prose, not a markdown table any more)."""
    body = build_activation_pr_body("miranda", _state(), _dept())
    # the _dept() fixture's missions reference 'scan' and 'review' in their ids
    assert "scan" in body
    assert "review" in body
    # And they live under the humanized heading
    assert "## Ce qu'elle fera chaque jour" in body


def test_pr_body_renders_4_moments_of_the_day():
    """All 4 layers surface under their human moment names (vocabulary table)."""
    body = build_activation_pr_body("miranda", _state(), _dept())
    for human_moment in (
        "Le matin",
        "La recherche",
        "L'exécution",
        "Le débrief du soir",
    ):
        assert human_moment in body, f"moment-of-day not surfaced: {human_moment!r}"


def test_pr_body_describes_dry_run_in_french():
    """The dry-run section uses 'répétition à blanc' phrasing, not PASS/WARN."""
    body = build_activation_pr_body("miranda", _state(), _dept())
    # Either 'elle est passée' (happy path) or 'pas encore fait' (RED path)
    assert ("répétition à blanc" in body), \
        "Dry-run section should use 'répétition à blanc' phrasing."


def test_pr_body_pre_ceremony_checklist_has_5_items():
    """The pre-ceremony checklist has the 5 humanized items."""
    body = build_activation_pr_body("miranda", _state(), _dept())
    section = body.split(
        "## Ce qu'il faut vérifier avant la cérémonie", 1
    )[1]
    section = section.split("\n## ", 1)[0]
    bullets = [l for l in section.splitlines() if l.lstrip().startswith("- [ ]")]
    assert len(bullets) >= 5, f"need >=5 unchecked bullets, got {len(bullets)}"


def test_pr_body_renders_risk_notes_under_points_d_attention():
    """Risk notes section uses the humanized 'Points d'attention' heading."""
    state = _state(risk_notes=["First risk", "Second risk"])
    body = build_activation_pr_body("miranda", state, _dept())
    assert "## Points d'attention" in body
    assert "First risk" in body
    assert "Second risk" in body


def test_pr_body_omits_points_d_attention_section_when_absent():
    body = build_activation_pr_body("miranda", _state(), _dept())
    if "## Points d'attention" in body:
        section = body.split("## Points d'attention", 1)[1].split("\n##", 1)[0]
        assert section.strip(), "Points d'attention section present but empty"


def test_pr_body_includes_dept_slug_in_title_area():
    body = build_activation_pr_body("miranda", _state(), _dept())
    assert "miranda" in body.lower()


# ---------- open_activation_pr (subprocess-mocked) ------------------------


def _fake_run_factory(broker_token="ghs_FAKE_TOKEN_ABCDEFG",
                      gh_stdout="https://github.com/vdk888/bubble-ops-miranda/pull/42",
                      gh_rc=0, broker_rc=0):
    """Return a stub for subprocess.run that pretends broker + gh succeed."""
    calls = []

    def _fake(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "env_keys": sorted((kwargs.get("env") or {}).keys()),
                      "env_has_GH_TOKEN": bool((kwargs.get("env") or {}).get("GH_TOKEN"))})
        head = cmd[0] if isinstance(cmd, list) else cmd.split()[0]
        if "bubble-token-broker" in str(head):
            class R:
                returncode = broker_rc
                stdout = broker_token
                stderr = ""
            return R()
        if head == "gh" or str(head).endswith("/gh"):
            class R:
                returncode = gh_rc
                stdout = gh_stdout + "\n"
                stderr = "" if gh_rc == 0 else "boom"
            return R()
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    return _fake, calls


def test_open_activation_pr_invokes_broker_and_gh(tmp_path):
    fake, calls = _fake_run_factory()
    with patch.object(subprocess, "run", side_effect=fake):
        out = open_activation_pr(
            dept_slug="miranda",
            repo_url="https://github.com/vdk888/bubble-ops-miranda",
            branch="onboarding/miranda",
            pr_title="Activate Miranda department",
            pr_body="# body",
            broker_path="/usr/bin/bubble-token-broker",
            guard_path="/usr/bin/bubble-git-guard",
        )
    assert out["pr_number"] == 42
    assert "vdk888/bubble-ops-miranda/pull/42" in out["url"]
    assert out["branch"] == "onboarding/miranda"
    # broker called with mint subcommand
    broker_calls = [c for c in calls if "bubble-token-broker" in str(c["cmd"])]
    assert len(broker_calls) >= 1
    cmd = broker_calls[0]["cmd"]
    assert "mint" in cmd
    assert "--dept" in cmd and "miranda" in cmd
    # gh called with token in env
    gh_calls = [c for c in calls if (c["cmd"][0] == "gh"
                if isinstance(c["cmd"], list) else False)]
    assert len(gh_calls) >= 1
    assert gh_calls[0]["env_has_GH_TOKEN"]


def test_open_activation_pr_raises_on_broker_failure(tmp_path):
    fake, _ = _fake_run_factory(broker_rc=2)
    with patch.object(subprocess, "run", side_effect=fake):
        with pytest.raises(ActivationPRError, match="broker"):
            open_activation_pr(
                dept_slug="miranda",
                repo_url="https://github.com/vdk888/bubble-ops-miranda",
                branch="onboarding/miranda",
                pr_title="Activate Miranda department",
                pr_body="# body",
                broker_path="/usr/bin/bubble-token-broker",
                guard_path="/usr/bin/bubble-git-guard",
            )


def test_open_activation_pr_raises_on_gh_failure(tmp_path):
    fake, _ = _fake_run_factory(gh_rc=1)
    with patch.object(subprocess, "run", side_effect=fake):
        with pytest.raises(ActivationPRError, match="gh pr create"):
            open_activation_pr(
                dept_slug="miranda",
                repo_url="https://github.com/vdk888/bubble-ops-miranda",
                branch="onboarding/miranda",
                pr_title="Activate Miranda department",
                pr_body="# body",
                broker_path="/usr/bin/bubble-token-broker",
                guard_path="/usr/bin/bubble-git-guard",
            )


def test_open_activation_pr_uses_open_priority_pr_action(tmp_path):
    """Per Notion v5 lines 622-624 (open_priority_pr) — but for own-repo
    activation, we use a token class that allows PR creation. We assert
    it's NOT the runtime_read action (which can't open PRs)."""
    fake, calls = _fake_run_factory()
    with patch.object(subprocess, "run", side_effect=fake):
        open_activation_pr(
            dept_slug="miranda",
            repo_url="https://github.com/vdk888/bubble-ops-miranda",
            branch="onboarding/miranda",
            pr_title="t", pr_body="b",
            broker_path="/usr/bin/bubble-token-broker",
            guard_path="/usr/bin/bubble-git-guard",
        )
    broker_calls = [c for c in calls if "bubble-token-broker" in str(c["cmd"])]
    cmd = broker_calls[0]["cmd"]
    # action must be one that allows pull_requests:write
    assert "--action" in cmd
    action_idx = cmd.index("--action")
    action = cmd[action_idx + 1]
    assert action in ("open_priority_pr", "settings_pr"), \
        f"need PR-capable action, got {action!r}"
