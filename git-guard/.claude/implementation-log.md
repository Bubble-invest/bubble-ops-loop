# Implementation Log
**Stack**: Python (pytest + pyyaml + jsonschema) | **Updated**: 2026-05-20

## Project Patterns
- **Test location**: `Rick_RnD/projects/bubble-ops-loop/tests/{round-trip,subagent-perms}/`
- **Schemas**: `schemas-draft/*.schema.yaml` (Draft-07 JSON Schema in YAML form)
- **Fixture clone**: `/tmp/bubble-ops-fixture/` (writable local replica); remote = `vdk888/bubble-ops-fixture`
- **Date convention**: UTC for all `outputs/<date>/` paths (Notion v4 cron schedules in UTC)

## What Works
- `gh api repos/vdk888/bubble-ops-fixture/contents/<path>` for remote file probing without cloning
- `yaml.safe_load + jsonschema.Draft7Validator` validates management-export structure cleanly
- `BUBBLE_OPS_LAYER4_DATE` env override lets the same test replay for any date

## Gotchas
- `management-export.yaml` lives at `outputs/<date>/` NOT `outputs/<date>/4/` (Notion v4 L465) — common drift trap
- `mandate-guardian` subagent has NO Bash → it cannot itself commit/push. Loop wrapper handles that.
- `.last-run` files use `Z` suffix; `datetime.fromisoformat` needs `Z → +00:00` normalisation on Py<3.11
- gh api raises CalledProcessError(1) on 404 — assertions should wrap with `_file_exists_local_or_remote()` guard before `_read_file_local_or_remote()`
- dept.schema `department:` block has `additionalProperties: false` — any new optional field (status, display_name, owner, forbidden) MUST be declared in schema before templates emit them; otherwise validation fails silently in tests.
- Jinja2 multi-line YAML rendering: use `trim_blocks=True, lstrip_blocks=True` else `{% for %}` over `- {{ item }}` leaves blank lines that confuse yaml.safe_load on edge cases.

## Recent Changes
- 2026-05-20: Step 9 — created `tests/round-trip/test_layer4_three_outputs.py` (11 assertions; 10 RED captured), wrote `STEP-9-LAYER4-VALIDATION.md`, prepared Telegram trigger for @bubtiktikbot
- 2026-05-20: UX-1 — built `skills/department-onboarding-guide/` (TDD: 20 tests GREEN, 8 skill_lib modules, 4 Jinja2 templates, 3 example dept.yaml). Extended `dept.schema.yaml` v3->v3.1 (added optional `status`/`display_name`/`owner`/`forbidden` under `department:`); Step 0 (9/9) + Step 6 round-trip stay green. Global symlink at `~/.claude/skills/department-onboarding-guide`.
