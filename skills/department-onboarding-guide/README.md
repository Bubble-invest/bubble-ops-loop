# department-onboarding-guide

Conversational onboarding skill for new bubble-ops-loop departments.
Drives the 7-step eclosure (Notion v5 §"UX — Agent Nursery / Department
Onboarding").

See `SKILL.md` for the per-step contracts. This README is the end-to-end
walkthrough.

## Install / use locally

The skill is intended to ship with every bubble-ops-loop dept fork (it lives
under `skills/department-onboarding-guide/` in the template repo). For local
testing from any Claude Code session, a global symlink is set up at:

```
~/.claude/skills/department-onboarding-guide -> projects/bubble-ops-loop/skills/department-onboarding-guide
```

## Run the tests

```bash
cd projects/bubble-ops-loop/skills/department-onboarding-guide
python3 -m pytest tests/ -v
```

Expected: 38 tests pass (20 UX-1 + 18 UX-4 dry-run simulator).

## Dry-run details (UX-4)

Per Notion v5 lines 925-946 the dry-run is the gate to activation: an agent
"ne passe pas live sans dry run all-green ou acceptation explicite". UX-4
makes that gate executable end-to-end.

### Entry points

| Tool | Location | Purpose |
|---|---|---|
| `run_dry_run_full(...)` | `skill_lib/dry_run.py` | Python lib: writes artifacts + returns `DryRunResult` |
| `run-dry-run.sh` | `scripts/run-dry-run.sh` (project-level) | CLI wrapper: prints JSON + exit code |
| `render_dry_run_html(...)` | `skill_lib/dry_run_renderer.py` | HTMX swap-safe HTML fragment for the UX-3 frontend |
| `templates/dry_run_fixtures/` | bundled YAML | 4 canonical fake queue items (generic + 3 ops-leaf shapes) |

### Artifact tree (one round-trip)

```
<dept_root>/outputs/dry-run/<ts>/
├── 1/                                       # Layer 1
│   ├── summary.md                           # canonical 4-file output schema
│   ├── artifacts/.gitkeep                   #
│   ├── logs.jsonl                           #
│   ├── .last-run                            #
│   └── synthesized-queue-item.yaml          # validates against queue-item.schema.yaml
├── 2/                                       # Layer 2
│   ├── summary.md + artifacts/ + logs.jsonl + .last-run
│   └── gate-fake-<id>.yaml                  # validates against gate-item.schema.yaml
├── inbox/decisions/<gate_id>.yaml           # synthetic operator approval
├── 3/                                       # Layer 3
│   ├── summary.md + artifacts/ + logs.jsonl + .last-run
│   └── exec-log.jsonl                       # one fake exec line
└── 4/                                       # Layer 4 (3 outputs, Notion v5 line 278)
    ├── summary.md + artifacts/ + logs.jsonl + .last-run
    ├── risk-brief.md
    ├── risk-kpis.yaml
    └── management-export.yaml               # validates against management-export.schema.yaml
```

### CLI exit codes (Notion v5 line 946)

| Result | --accept-warnings | Exit |
|---|---|---|
| PASSED | any | 0 |
| WARNING | true | 0 |
| WARNING | false | 1 |
| FAILED | any (override is ignored) | 1 |

### Determinism

Pass `--seed=42` (or `seed=42` from Python) to get byte-identical artifacts
across runs. The wall-clock `.last-run` sentinel is the only varying file.
Used by CI to assert artifact stability.

### Canonical warning (Notion v5 line 944)

When `dept.yaml.draft::gate_policies.*.kpi_guardrails` declares a
`brand_safety_*` key but no `tests/*brand_safety*.yaml` fixture exists,
the simulator emits a `layer_4_brand_safety_fixture` warning. The dept
can still be activated if the operator passes `--accept-warnings`.

## Full walkthrough — bringing up Miranda

This is a literal trace of what the agent does, using the skill, to take
Miranda from `Idea` to `Live`.

### Pre-conditions
- New repo `bubble-ops-miranda` exists with the standard skeleton.
- `dept.yaml.draft` is empty.
- Operator clicks "Continue onboarding" on the Miranda card in the front-end.

### Step 1 — Mandate

Agent uses `skill_lib.state_machine`:
```python
from skill_lib.state_machine import OnboardingStateMachine
sm = OnboardingStateMachine(current="Idea")
sm.advance_to("Configuring")
```

Agent asks: "Quelle est ta mission, qui est ton owner, quels sont tes interdits ?"
Operator answers. Agent fills the context dict and renders:

```python
from skill_lib.templates import render_template
ctx = {
    "slug": "miranda",
    "display_name": "Miranda",
    "level": "ops",
    "mandate_text": "Produire, planifier et auditer du contenu social pour Bubble.",
    "owner": "operator",
    "forbidden": [
        "publier informations confidentielles",
        "donner conseil financier personnalise",
        "nommer client sans validation",
    ],
}
yaml_text = render_template("dept.yaml", ctx)
```

Agent writes `dept.yaml` to the repo, commits on branch `onboarding/miranda`:
```
git checkout -b onboarding/miranda
git add dept.yaml
git commit -m "onboarding: validate mandate"
```

### Step 2 — Recurring missions

```python
sm.advance_to("Drafting")
```

Agent asks: "Que dois-je surveiller / produire regulierement ?"
Operator describes 3 missions. Agent renders each via `mission.yaml.template`,
writes them to `missions/<id>.yaml`, validates against
`recurring-mission.schema.yaml`, commits:
```
git add missions/
git commit -m "onboarding: add recurring missions"
```

### Step 3 — Layer mapping

Agent asks: "Que dois-je faire a chaque etape du flow standard (4 layers) ?"
Operator answers in 4 sentences. Agent:

```python
from skill_lib.layers import map_layers, generate_layer_prompt_stub
mapping = map_layers({
    "layer_1": "Scanner signaux contenus, calendrier, idees, performances passees.",
    "layer_2": "Transformer les signaux en idees de posts, drafts, variantes.",
    "layer_3": "Programmer / publier / mettre en draft apres gate.",
    "layer_4": "Auditer brand safety, ton, performance, repetition, fatigue audience.",
})
for n in (1, 2, 3, 4):
    stub = generate_layer_prompt_stub(layer=n, description=mapping[f"layer_{n}"])
    Path(f"layers/{n}/PROMPT.md").write_text(stub)
```

Commit: `onboarding: map 4 layers`.

### Step 4 — Skills & tools

```python
sm.advance_to("Needs validation")
from skill_lib.skills_tools import build_manifest, validate_card
manifest = build_manifest({
    "skills": {
        "layer_1": ["content-signal-scanner", "content-calendar-reader"],
        "layer_2": ["post-drafter", "angle-generator"],
        "layer_3": ["content-scheduler"],
        "layer_4": ["brand-safety-auditor", "content-performance-reviewer"],
    },
    "tools": ["linkedin-reader", "shared-wiki-reader", "post-scheduler", "analytics-reader"],
})
# Per-card validation (one card per skill/tool):
card = validate_card("content-signal-scanner", {
    "purpose": "detecter des idees de contenu",
    "inputs": ["wiki", "linkedin", "notes"],
    "outputs": ["content_idea_task"],
    "status": "draft",
})
```

Agent merges the manifest into `dept.yaml::skills` + `dept.yaml::tools`,
writes cards to `skills/<slug>/SKILL.md` and `tools/<slug>/README.md`,
commits: `onboarding: declare skills and tools`.

### Step 5 — Gates, autonomy bands, KPI guardrails

```python
sm.advance_to("Dry run")
from skill_lib.gates import build_authorization_band
band = build_authorization_band(
    band_id="low_risk_evergreen",
    allowed_types=["educational", "evergreen", "repost_with_comment"],
    forbidden=["client_names", "financial_advice", "controversial_topics"],
)
gate_yaml = render_template("gate_policy.yaml", {
    "policy_id": "social_post",
    "current_mode": "manual_required",
    "eligible_future_modes": ["auto_with_veto_window", "auto_if_policy_passed"],
    "authorization_band": band["id"],
    "kpi_guardrail_set": "miranda_content_kpis",
    "kpi_guardrails": {
        "brand_safety_breaches": 0,
        "human_edit_rate_30d": "<= 20%",
        "negative_feedback_rate": "<= 1%",
        "quality_score_30d": ">= 0.8",
    },
})
```

Agent merges into `dept.yaml::gate_policies`, writes the band to
`policies/bands/low_risk_evergreen.yaml`, commits: `onboarding: add gates and kpis`.

### Step 6 — Dry-run

```python
from skill_lib.dry_run import run_dry_run, DryRunStatus
result = run_dry_run(
    dept_root=Path("."),
    fake_queue_item={
        "id": "content-dryrun-001",
        "kind": "content_idea_task",
        "source_layer": 1,
        "target_layer": 2,
        "priority": "low",
        "created_at": "2026-05-20T08:00:00Z",
        "payload": {"topic": "MVP architecture lessons"},
    },
    layer_checks={
        "layer_1": "passed",
        "layer_2": "passed",
        "layer_3": "passed",
        "layer_4": "warning",   # Missing brand safety test fixture
    },
    operator_accepts_warnings=True,  # operator explicitly accepts (Notion v5 line 946)
)
assert result["overall_status"] == DryRunStatus.WARNING
assert result["can_advance_to_ready"] is True
sm.advance_to("Ready to activate")
```

Commit: `onboarding: add dry-run fixtures`.

### Step 7 — Activation

```python
from skill_lib.activation import flip_status_to_live, build_activation_pr_body
flip_status_to_live(Path("dept.yaml"))
body = build_activation_pr_body(
    display_name="Miranda",
    slug="miranda",
    validated_steps=["mandate", "missions", "layers", "skills_tools", "gates_kpis", "dry_run"],
)
# Agent then:
#   git add dept.yaml
#   git commit -m "onboarding: activate Miranda"
#   git push origin onboarding/miranda
#   gh pr create --title "Activate Miranda department" --body "$body"
sm.advance_to("Live")
```

Effect:
- `dept.yaml::department.status` flips `onboarding` -> `live`
- Front-end card moves: `Agents a eclore` -> `Live departments`
- Session becomes `ops-loop-miranda`

## Reference examples

Three example `dept.yaml` files live under `examples/`:

| File | Status | Purpose |
|---|---|---|
| `examples/maya/dept.yaml` | `live` | Sales prospection, end-of-onboarding shape |
| `examples/ben/dept.yaml` | `live` | Family office WITH SQLite `optional_domain_ledger` |
| `examples/miranda/dept.yaml` | `onboarding` | Mid-eclosure shape (Steps 1-5 done, 6-7 pending) |

All three validate against `schemas-draft/dept.schema.yaml`.
