"""
test_dry_run_synthesizes_queue_item_from_recurring_missions.py — UX-4

When `fake_queue_item=None`, the simulator must synthesize a canonical
fake item from dept.yaml::recurring_missions[0].creates[0]. This wires
the dry-run to the dept's actual mission contract instead of a generic
echo.
"""
from __future__ import annotations

import yaml

from skill_lib.dry_run import run_dry_run_full


def test_synthesizes_from_recurring_missions(tmp_dept_repo):
    dept_draft = {
        "department": {
            "slug": "miranda",
            "level": "ops",
            "mandate": "Produire et publier du contenu social.",
            "status": "onboarding",
        },
        "recurring_missions": [
            {
                "id": "content_signal_scan",
                "layer": 1,
                "cadence": "daily",
                "time": "07:30",
                "description": "Scanner les signaux.",
                "output_queue": "queues/research/",
                "creates": ["content_idea_task"],
            }
        ],
    }
    (tmp_dept_repo / "dept.yaml.draft").write_text(
        yaml.safe_dump(dept_draft, sort_keys=False), encoding="utf-8"
    )

    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=None,  # let the simulator synthesize
        seed=42,
    )
    qi_path = result.artifacts_dir / "1" / "synthesized-queue-item.yaml"
    qi = yaml.safe_load(qi_path.read_text(encoding="utf-8"))
    assert qi["kind"] == "content_idea_task", (
        f"expected kind from recurring_missions[0].creates[0], got {qi['kind']!r}"
    )


def test_falls_back_to_generic_when_no_recurring_missions(tmp_dept_repo):
    dept_draft = {
        "department": {
            "slug": "minimal",
            "level": "ops",
            "mandate": "Minimal dept with no recurring missions yet.",
            "status": "onboarding",
        },
    }
    (tmp_dept_repo / "dept.yaml.draft").write_text(
        yaml.safe_dump(dept_draft, sort_keys=False), encoding="utf-8"
    )
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=None,
        seed=42,
    )
    qi_path = result.artifacts_dir / "1" / "synthesized-queue-item.yaml"
    qi = yaml.safe_load(qi_path.read_text(encoding="utf-8"))
    assert qi["kind"] == "research", "should fall back to generic kind"
