"""
conftest.py — pytest fixtures for the department-onboarding-guide skill.

Three fixtures:
  - stub_agent_context()  simulates operator answers for each onboarding step,
                          returns deterministic dicts. No LLM involved.
  - schema_validator()    loads a JSON schema from schemas-draft/ and returns
                          a jsonschema.Draft7Validator bound to that schema.
  - tmp_dept_repo()       creates a minimal dept-repo skeleton (queues/,
                          outputs/, inbox/, missions/, layers/{1..4}/) in a
                          tmp_path so tests can write artifacts into it.

Per the skill design constraint (no LLM in the skill itself, pure-Python
deterministic), every fixture is synchronous and side-effect-free except
tmp_dept_repo which writes into pytest's tmp_path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Resolve canonical paths (skill lives at projects/bubble-ops-loop/skills/department-onboarding-guide).
SKILL_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SKILL_ROOT.parent.parent  # .../projects/bubble-ops-loop
SCHEMAS_DIR = PROJECT_ROOT / "schemas-draft"

# Make the skill package importable in tests.
sys.path.insert(0, str(SKILL_ROOT))


@pytest.fixture
def stub_agent_context():
    """
    Return a callable that produces deterministic operator answers per step.

    Usage:
        ctx = stub_agent_context("step1_mandate")  # -> dict for step 1
    """

    def _ctx(step: str) -> dict:
        data = {
            "step1_mandate": {
                "slug": "miranda",
                "display_name": "Miranda",
                "level": "ops",
                "mandate_text": "Produire, planifier et auditer du contenu social pour Bubble Invest.",
                "owner": "joris",
                "forbidden": [
                    "publier informations confidentielles",
                    "donner conseil financier personnalise",
                    "nommer client sans validation",
                ],
            },
            "step2_missions": {
                "missions": [
                    {
                        "id": "content_signal_scan",
                        "layer": 1,
                        "cadence": "daily",
                        "time": "07:30",
                        "description": "Scanner les signaux de contenu (wiki + LinkedIn + notes).",
                        "input_sources": ["wiki", "linkedin", "notes"],
                        "output_queue": "queues/research/",
                        "creates": ["content_idea_task"],
                    },
                    {
                        "id": "weekly_performance_review",
                        "layer": 1,
                        "cadence": "weekly",
                        "day": "monday",
                        "time": "09:00",
                        "description": "Recap hebdomadaire de performance contenu.",
                        "output_queue": "queues/research/",
                        "creates": ["perf_review_task"],
                    },
                    {
                        "id": "hourly_calendar_refresh",
                        "layer": 1,
                        "cadence": "hourly",
                        "active_hours": "08:00-20:00",
                        "description": "Rafraichir le calendrier editorial.",
                        "output_queue": "queues/research/",
                        "creates": ["calendar_refresh_task"],
                    },
                ]
            },
            "step3_layers": {
                "layer_1": "Scanner signaux contenus, calendrier, idees, performances passees.",
                "layer_2": "Transformer les signaux en idees de posts, drafts, variantes, angles.",
                "layer_3": "Programmer / publier / mettre en draft apres gate.",
                "layer_4": "Auditer brand safety, ton, performance, repetition, fatigue audience.",
            },
            "step4_skills_tools": {
                "skills": {
                    "layer_1": ["content-signal-scanner", "content-calendar-reader"],
                    "layer_2": ["post-drafter", "angle-generator"],
                    "layer_3": ["content-scheduler"],
                    "layer_4": ["brand-safety-auditor", "content-performance-reviewer"],
                },
                "tools": [
                    "linkedin-reader",
                    "shared-wiki-reader",
                    "post-scheduler",
                    "analytics-reader",
                ],
                "cards": {
                    "content-signal-scanner": {
                        "purpose": "detecter des idees de contenu",
                        "inputs": ["wiki", "linkedin", "notes"],
                        "outputs": ["content_idea_task"],
                        "status": "draft",
                    }
                },
            },
            "step5_gates_kpis": {
                "policy_id": "social_post",
                "current_mode": "manual_required",
                "eligible_future_modes": [
                    "auto_with_veto_window",
                    "auto_if_policy_passed",
                ],
                "authorization_band": "low_risk_evergreen",
                "allowed_post_types": ["educational", "evergreen", "repost_with_comment"],
                "forbidden": ["client_names", "financial_advice", "controversial_topics"],
                "kpi_guardrail_set": "miranda_content_kpis",
                "kpi_guardrails": {
                    "brand_safety_breaches": 0,
                    "human_edit_rate_30d": "<= 20%",
                    "negative_feedback_rate": "<= 1%",
                    "quality_score_30d": ">= 0.8",
                },
            },
            "step6_dry_run": {
                "fake_queue_item": {
                    "id": "content-dryrun-001",
                    "kind": "content_idea_task",
                    "source_layer": 1,
                    "target_layer": 2,
                    "priority": "low",
                    "created_at": "2026-05-20T08:00:00Z",
                    "payload": {"topic": "MVP architecture lessons"},
                }
            },
            "step7_activation": {
                "slug": "miranda",
                "display_name": "Miranda",
                "validated_steps": [
                    "mandate",
                    "missions",
                    "layers",
                    "skills_tools",
                    "gates_kpis",
                    "dry_run",
                ],
            },
        }
        if step not in data:
            raise KeyError(f"Unknown step: {step}")
        return data[step]

    return _ctx


@pytest.fixture
def schema_validator():
    """
    Return a callable that loads a schema from schemas-draft/ and returns a
    jsonschema.Draft7Validator.

    Usage:
        v = schema_validator("dept")               # loads dept.schema.yaml
        v = schema_validator("recurring-mission")  # loads recurring-mission.schema.yaml
    """
    import jsonschema  # local import keeps top-of-file clean

    def _loader(name: str):
        path = SCHEMAS_DIR / f"{name}.schema.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Schema not found: {path}")
        schema = yaml.safe_load(path.read_text(encoding="utf-8"))
        return jsonschema.Draft7Validator(schema)

    return _loader


@pytest.fixture
def tmp_dept_repo(tmp_path):
    """
    Create a minimal dept-repo skeleton in tmp_path. Returns the Path.

    Skeleton mirrors the bubble-ops-fixture layout (queues/, outputs/, inbox/,
    missions/, layers/{1..4}/, skills/, tools/, tests/).
    """
    root = tmp_path / "dept-repo"
    for sub in [
        "queues/research",
        "queues/management",
        "outputs",
        "inbox/decisions",
        "missions",
        "layers/1",
        "layers/2",
        "layers/3",
        "layers/4",
        "skills",
        "tools",
        "tests",
    ]:
        (root / sub).mkdir(parents=True, exist_ok=True)
    # Initialize a tiny git-like marker so the skill can locate "the repo root".
    (root / ".git").mkdir(exist_ok=True)
    return root


@pytest.fixture
def skill_root():
    """Return the absolute Path to the skill directory under test."""
    return SKILL_ROOT


@pytest.fixture
def schemas_dir():
    """Return the absolute Path to schemas-draft/."""
    return SCHEMAS_DIR
