# `schemas-draft/` — bubble-ops-loop Step 0 contracts (v3)

JSON-Schema-draft-07 contracts for the `bubble-ops-loop` fixture and every
future `bubble-ops-<dept>` repo. **v3** supersedes v1 (backed up at
`../schemas-draft.bak-v1-20260520-1505/`). Source of truth: Notion page
`bubble-ops-loop — Architecture finale simplifiée`, last edited
**2026-05-20T15:04 UTC**. Local dump: `/tmp/notion_final.txt`.

## What's new in v3 (6 structural changes vs v1; v3.1 added a 7th schema)

The v3 release introduced 6 of the schemas listed below; **v3.1 added a
7th** (`state.schema.yaml`, for `onboarding/STATE.yaml`). The current
**7 schemas** total are inventoried in [File inventory](#file-inventory)
and enforced by `tests/validate_all.py::EXPECTED_SCHEMA_COUNT`. The
filesystem ↔ README count is guarded by
`tests/test_readme_count_in_sync.py`.

1. **`department:` wrapper** — `dept.yaml` root now requires a
   `department: { slug, level, mandate }` block. Replaces v1's flat
   top-level `name:` / `mandate_url:`. Negative test:
   `tests/negative/dept-missing-department-wrapper.yaml`.
2. **`recurring_missions:`** — REPLACES the v1 `cadence:` block. Crons
   become declarative missions read by Layer 1, materialized into queue
   items. **Required key** even if empty (management depts may have
   `recurring_missions: []`). New schema:
   `recurring-mission.schema.yaml` covers the standalone form.
3. **`gate_policies:`** — declares the 5-mode autonomy doctrine per
   gated-action class (`manual_required → manual_unless_policy_passed →
   auto_if_policy_passed → auto_with_veto_window → disabled`). v1
   gates are always `manual_required`; policies are configured so they
   can be raised without refactoring.
4. **Gate item enriched** — `gate-item.yaml` now requires `current_mode`
   + `future_eligible_modes` on every gate. Domain kinds
   (prospect_dm / trade_order / content_publish / `domain:*`) ALSO
   require `gate_policy_id` + `authorization_band_id`.
5. **`autonomy_readiness:`** (optional) on `management-export.yaml` —
   Layer 4 writes 14/30-day shadow-autonomy deltas: would-have-auto vs
   human-approved/modified/rejected, KPI status, recommended mode.
6. **Sixth schema `recurring-mission.schema.yaml`** — for the new
   `missions/` directory in the repo skeleton. Same shape as the
   sub-schema inlined in `dept.schema.yaml::recurring_missions[]`.

`directive.schema.yaml` is unchanged vs v2.

## File inventory

```
schemas-draft/
├── dept.schema.yaml                      v3 — `department:` wrapper + recurring_missions + gate_policies
├── recurring-mission.schema.yaml         NEW v3 — single-mission schema (for missions/*.yaml)
├── queue-item.schema.yaml                v3 — `kind:` is extensible (snake_case pattern)
├── gate-item.schema.yaml                 v3 — enriched: 5-mode autonomy + domain-kind extensibility
├── management-export.schema.yaml         v3 — optional `autonomy_readiness:` field
├── directive.schema.yaml                 v2 — unchanged
├── examples/                             12 positive examples
└── tests/
    ├── validate_all.py                   harness; expects 7 schemas
    ├── test_readme_count_in_sync.py      asserts README count == fs count
    └── negative/                         8 negative examples (one per rule)
```

## Run the validator

```bash
cd /Users/{{OPERATOR_USER}}/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/schemas-draft
python3 tests/validate_all.py
echo "exit=$?"   # 0 = green
```

Deps: `pip3 install jsonschema pyyaml --break-system-packages` (or use
`bubble-vps-platform/.venv/`).

## How to add a new schema

1. Drop `<name>.schema.yaml` at the root with `$schema` + `$id`.
2. Add a `("<name>-", "<name>.schema.yaml")` tuple at the TOP of
   `PREFIX_TO_SCHEMA` in `tests/validate_all.py` (longer prefixes first
   to avoid collisions — e.g. `recurring-mission-` before any future
   `recurring-`).
3. Bump `EXPECTED_SCHEMA_COUNT` in `validate_all.py`.
4. Add at least one positive `examples/<name>-*.yaml` and one negative
   `tests/negative/<name>-*.yaml` with a comment naming the rule it
   breaks.

## {{OPERATOR}}-confirmed apply-and-flag decisions

Three v3 design choices were taken with `apply, flag for
post-validation` per the task spec. {{OPERATOR}} should sanity-check these
when reviewing Step 0:

1. **`department:` wrapper as canonical root** + `hierarchy:` /
   `optional_domain_ledger:` as SIBLINGS of `department:` (not nested
   inside it). The Notion v3 sample (`/tmp/notion_final.txt` lines
   279-326) is ambiguous on placement; this README documents the
   canonical choice.
2. **`gate-item.kind` = enum extensible** via `oneOf` of (enum of v1
   internal kinds + documented domain kinds) OR (pattern
   `^domain:[a-z][a-z_]+$`). Lets `domain:legal_review` etc. land
   without a schema bump.
3. **`recurring_missions:` is REQUIRED but MAY be empty** (`[]`) for
   management depts. Tony example carries `recurring_missions: []`
   explicitly.

## Deviations from the task spec + Notion

- **dept-ops-maya.yaml line 64** — adds `time: "09:00"` to the
  `dormant_prospect_reactivation` weekly mission. Notion sample omits
  it, but the schema enforces `time` REQUIRED when `cadence=weekly`.
  Choice: tighten the schema rather than relax it, since `weekly` with
  no `time` is non-deterministic.
- **queue-item.schema.yaml::kind** — uses a single
  `pattern: ^[a-z][a-z_]+$` with the enum documented in the
  description. Pure `oneOf` of (enum, pattern) fails JSON-Schema
  semantics when the enum entries also match the pattern (must satisfy
  EXACTLY one, not at least one). The pattern alone is a strict
  superset and serves the same enforcement goal.

## Cross-references

- Notion v3 source: `/tmp/notion_final.txt` (654 lines, fetched
  2026-05-20T15:08). Maya recurring_missions example: lines 336-373.
- MVP-ROADMAP: `../MVP-ROADMAP.md` §3 Step 0.
- v1 backup: `../schemas-draft.bak-v1-20260520-1505/` (kept for the
  TDD harness pattern and the queue-item base structure).
