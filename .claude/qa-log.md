# QA Learning Log
**Stack**: Python 3.9 / FastAPI + Jinja + HTMX | **Test cmd**: `python3 -m pytest tests/ -q` | **Updated**: 2026-05-21

## Known Issues
- [Pre-existing 12 failures]: `tests/test_skeleton_completeness.py` + `tests/round-trip/test_layer4_three_outputs.py`. NOT polish-fixes regressions.
- [Bootstrap-dept]: bot-handle generation has no len/uniqueness validation — long slugs (>22 chars) produce invalid Telegram usernames silently.
- [Wiring gap]: systemd unit `WorkingDirectory=/home/claude/agents/<slug>` vs `deploy-to-morty.sh` clones to `/srv/bubble-ops-<slug>` — paths don't match.
- [ActivationRunner --dry-run trap]: `_run_activation_script` ALWAYS passes `--dry-run` (activation.py:84). In prod after Maya approves the lettre, NO real PR is opened, NO deploy, NO systemd unit. Only STATE.yaml::status flips to "Live" via runner mirror. Operator must MANUALLY run `scripts/activate-dept.sh` w/o --dry-run after. NOT documented.
- [Console UX gap]: `console/templates/onboarding.html` renders STEP-level only. New `kpi_naming` / `band_naming` sub-phases (Fix 3) not surfaced. Operator sees "Step 5 in progress" but not "asking about kpi naming for social_post".
- [Fix 1 sentinel hole]: assertion 4a (qi.kind == creates[0]) does NOT catch fabricated creates[0] — simulator dutifully copies whatever runner produces. Bite test only works via assertion 4b (dept slug propagation).
- [Activation script silent failure]: when `_run_activation_script` returns non-zero, runner stays not-done but re-emits the SAME PR body. Operator gets no feedback that script failed.

## What Works
- 386/386 tests pass on `pytest tests/test_qa_e2e_full_walk.py tests/test_qa_e2e_with_real_dry_run.py skills/department-onboarding-guide/tests/ console/tests/ -q`.
- Bite-tests confirm sentinels work: Fix 1 catches dept_slug drift, Fix 2 catches missing status flip, Fix 3 catches naming parse drift.
- SessionStart hook IS provisioned via scaffold.py (line 121-136) — previous log entry was stale.
- bootstrap-dept.sh DOES document operator-set-secret.sh + SOPS instructions (lines 181-195) — previous "Doc gap" entry now fixed.

## Gotchas
- [Trap]: The auto_drive prompts are SINGLE-TURN (3 options → answer). Notion mandates `[Approve][Edit][Ask agent to refine]` per-step.
- [Trap]: No migration path for existing Maya/Ben/Tony (greenfield only).
- [Naming validator quirks]: `_parse_naming_answer("quality floor")` silently picks "quality" with no warning. Accepts 1-char names. No semantic guard (e.g. "drop_table_users" accepted).
- [Simulator quirks]: 0 recurring_missions → silent generic `kind=research` fixture, all-pass. Doesn't honor `layers.subscribed`.
- [STATE.yaml mirror incomplete]: Fix 2 runner mirrors `status` + `activated_at` but NOT `activation_pr` (number/url) or `systemd_unit_path` that prod `mark_activated` would write.
