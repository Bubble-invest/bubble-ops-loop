# Step 9 — Layer 4 Daily Run: 3-Outputs Validation

**Date:** 2026-05-20
**Operator:** Rick (R&D)
**Phase status:** Phase A (RED) and Phase B (trigger drafted) complete; Phase C awaiting Joris-sent Telegram trigger or natural 22:00 UTC fire; Phase D (PROMPT.md audit) complete.

**Verdict:** **AWAITING-TRIGGER**

---

## TL;DR

- Test file created with **11 assertions** (10 hard + 1 optional/skip-tolerant) covering all 3 mandatory Notion v4 outputs PLUS the standard 4-file output schema for Layer 4 itself PLUS schema validation against `management-export.schema.yaml`.
- **RED captured cleanly** (2026-05-20 ~21:50 UTC) — 10 fail, 1 skip (autonomy_readiness is optional). No Layer 4 outputs exist yet for `2026-05-20` on the fixture repo.
- **Telegram trigger message drafted** below — Joris pastes verbatim to `@bubbleops_<slug>_bot`.
- **`layers/4/PROMPT.md` audit:** GREEN — the prompt is well-specified for all 3 outputs, the path-outside-`4/` quirk for `management-export.yaml` is called out explicitly, and the PURE-AUDITOR guard is reaffirmed. **Two small additions recommended for Step 11** (no blocker for Step 9 GREEN).

---

## Phase A — Test FIRST (RED)

### File
`Rick_RnD/projects/bubble-ops-loop/tests/round-trip/test_layer4_three_outputs.py`
(plus `tests/round-trip/__init__.py` package marker)

### Assertions (11 total)

| # | Test | What it asserts |
|---|------|-----------------|
| 1 | `test_risk_brief_md_exists` | `outputs/<date>/4/risk-brief.md` exists (local or GitHub) |
| 2 | `test_risk_kpis_yaml_exists` | `outputs/<date>/4/risk-kpis.yaml` exists |
| 3 | `test_management_export_yaml_exists_at_dept_level` | `outputs/<date>/management-export.yaml` exists — **not inside `4/`** (Notion v4 L465) |
| 4 | `test_risk_kpis_yaml_parses` | risk-kpis.yaml is a non-empty YAML mapping |
| 5 | `test_management_export_validates_against_schema` | full Draft-07 validation vs `management-export.schema.yaml` |
| 6 | `test_management_export_dept_and_date_match` | `dept: fixture`, `date: <today UTC>` |
| 7 | `test_layer4_summary_md_exists` | 4-file standard: `summary.md` |
| 8 | `test_layer4_artifacts_dir_present` | 4-file standard: `artifacts/` (`.gitkeep` OR non-empty) |
| 9 | `test_layer4_logs_jsonl_exists_and_nonempty` | 4-file standard: `logs.jsonl` with >=1 valid JSON line |
| 10 | `test_layer4_last_run_iso_timestamp` | 4-file standard: `.last-run` parses as ISO 8601 |
| 11 | `test_autonomy_readiness_block_if_present_validates` | OPTIONAL: if `autonomy_readiness` present, validates against schema v3 |

### RED capture (verbatim summary)

```
$ cd .
$ python3 -m pytest tests/round-trip/test_layer4_three_outputs.py -v --tb=short

FAILED tests/round-trip/test_layer4_three_outputs.py::test_risk_brief_md_exists
FAILED tests/round-trip/test_layer4_three_outputs.py::test_risk_kpis_yaml_exists
FAILED tests/round-trip/test_layer4_three_outputs.py::test_management_export_yaml_exists_at_dept_level
FAILED tests/round-trip/test_layer4_three_outputs.py::test_risk_kpis_yaml_parses
FAILED tests/round-trip/test_layer4_three_outputs.py::test_management_export_validates_against_schema
FAILED tests/round-trip/test_layer4_three_outputs.py::test_management_export_dept_and_date_match
FAILED tests/round-trip/test_layer4_three_outputs.py::test_layer4_summary_md_exists
FAILED tests/round-trip/test_layer4_three_outputs.py::test_layer4_artifacts_dir_present
FAILED tests/round-trip/test_layer4_three_outputs.py::test_layer4_logs_jsonl_exists_and_nonempty
FAILED tests/round-trip/test_layer4_three_outputs.py::test_layer4_last_run_iso_timestamp
======================== 10 failed, 1 skipped in 5.57s =========================
```

Cross-check (both vantage points confirm no L4 output for today):

```
$ cd /tmp/bubble-ops-fixture && git pull --quiet && find outputs/2026-05-20 -type f
outputs/2026-05-20/step7-guard-proof.md
outputs/2026-05-20/heartbeat.log
outputs/2026-05-20/step7-smoke.md
outputs/2026-05-20/step7-final-verify.md
outputs/2026-05-20/1/summary.md
outputs/2026-05-20/1/logs.jsonl
outputs/2026-05-20/1/.last-run
outputs/2026-05-20/2/summary.md
outputs/2026-05-20/2/logs.jsonl
outputs/2026-05-20/2/.last-run
outputs/2026-05-20/1/artifacts/.gitkeep
outputs/2026-05-20/2/research/research-roundtrip-test-001.md
outputs/2026-05-20/2/artifacts/.gitkeep
# (no outputs/2026-05-20/4/* and no outputs/2026-05-20/management-export.yaml)
```

RED is real, not a tooling artifact.

---

## Phase B — Manual trigger (Telegram message for Joris)

**Send to** `@bubbleops_<slug>_bot` (the fixture agent on Morty, NOT `@ContentbubbleClawbot` which is morty/Tony).

### Trigger message (copy verbatim)

```
Run Layer 4 (Risk Control) now manually. Read layers/4/PROMPT.md, then act as the mandate-guardian subagent: audit today's outputs/2026-05-20/{1,2,3}/ vs MANDATE.md, compute KPIs (loop ticks, commits count, gates count, exec count, errors), propose improvements. Produce the 3 mandatory outputs per Notion v4 line 465:

(1) outputs/2026-05-20/4/risk-brief.md — qualitative narrative
(2) outputs/2026-05-20/4/risk-kpis.yaml — structured KPI snapshot (fields per layers/4/PROMPT.md: nav_status / exec_status / risk_status / stale_runs)
(3) outputs/2026-05-20/management-export.yaml — compact dept summary at the DEPT level (sibling to 1/, 2/, 3/, 4/, NOT inside 4/). Must validate against templates/schemas/management-export.schema.yaml: required fields dept=fixture, date=2026-05-20, status in {clean,warning,red}, last_successful_layer (int 1-4), open_gates, open_exceptions, top_kpis, needs_management_attention (list), links (object).

Plus the standard 4-file output schema for Layer 4 itself, all under outputs/2026-05-20/4/:
- summary.md  (narrative: N audited, K breaches, J improvements)
- artifacts/.gitkeep  (or per-breach evidence)
- logs.jsonl  (>=1 JSON line {ts, scope, check, result, severity})
- .last-run  (ISO 8601 UTC timestamp)

autonomy_readiness is OPTIONAL on day-1 (no 14/30 day window yet) — skip it if you have no data.

Side-effects: git add outputs/2026-05-20/4/ outputs/2026-05-20/management-export.yaml queues/improvements/ ; commit with message "ops-loop Layer 4 manual run: 2026-05-20 — risk audit + management export" ; push via the bubble-git-guard CLI (broker mints token, guard validates write paths). If the guard rejects on "no staged files" (known Step-8 bug surface), invoke the broker directly per the Step 8 workaround pattern and retry git push with the ephemeral token.

PURE-AUDITOR guard reminder: mandate-guardian has Read, Grep, Glob, WebSearch, Write only. No Bash, no Agent. The git commit + push happen at the /loop wrapper level after the subagent returns its writes.

Report back via this chat when all 6 files are committed (or paste the gh api / git log -1 lines proving the commit).
```

### Why this exact phrasing
- Names every file with full path so the agent can't drop one.
- Pins the 4-file standard schema explicitly (PROMPT.md drift earlier in MVP suggested this is needed).
- Names the schema by filename so the agent validates locally before push.
- Calls out the `outputs/<date>/management-export.yaml` NOT in `4/` trap.
- Acknowledges the known guard-bug workaround from Step 8 so the agent doesn't get stuck.
- Asks for proof-of-commit on the way back so we can flip the test to GREEN remotely.

---

## Phase C — Polling for GREEN

Once Joris sends the message and the agent reports a commit (or once 22:00 UTC fires naturally), run:

```bash
cd .
python3 -m pytest tests/round-trip/test_layer4_three_outputs.py -v --tb=short
```

The test fixture issues a `git pull` once per module on `/tmp/bubble-ops-fixture/`, then falls back to `gh api` for any file not yet in the local clone. So you can re-run the test without manually pulling.

To replay for a different date (e.g., to validate yesterday's 22:00 UTC fire if we missed today's):

```bash
BUBBLE_OPS_LAYER4_DATE=2026-05-19 python3 -m pytest tests/round-trip/test_layer4_three_outputs.py -v
```

**Verdict semantics:**

- All 10 hard assertions pass + autonomy_readiness skip → **GREEN-NOW**, Step 9 done.
- 1-2 fail with schema drift (e.g., missing `last_successful_layer` field) → **GREEN-PARTIAL**, file Step 11 patch.
- All fail still → trigger didn't fire; re-prompt Joris or wait for 22:00 UTC cron.
- All present but schemas reject → **SCHEMA-DRIFT** (most likely outcome to watch), document and patch `layers/4/PROMPT.md` per Phase D below.

---

## Phase D — `layers/4/PROMPT.md` audit (gaps for Step 11)

The current prompt (94 lines on Morty + GitHub) is **broadly correct**. Strengths:

- All 3 paths spelled out with audience + validator (lines 55-65).
- The `management-export.yaml` outside-`4/` quirk called out explicitly (lines 61-65) — the most common drift trap.
- 4-file standard schema described in its own table (lines 67-76).
- PURE-AUDITOR guard reaffirmed twice (lines 12-13, 87-93).
- `risk_status: red` → Telegram via Write-to-queue (lines 83-85) — respects no-Bash.

### Recommended additions for Step 11 (don't apply now; document only)

1. **Pin the schema path explicitly.** Line 58 says "Validates against `management-export.schema.yaml`" — but in the fixture repo the live copy is at `templates/schemas/management-export.schema.yaml`. Subagent has no way to know that from the prompt alone. **Suggested patch:** add a line under "Outputs" saying `Schema authority: templates/schemas/management-export.schema.yaml (relative to repo root)`.

2. **Add an autonomy_readiness primer.** The prompt does not mention `autonomy_readiness` at all, yet the schema v3 + Notion Q4 update (`/tmp/notion_final.txt` L430-L438) say Layer 4 must compute & emit it once a 14/30 day window has data. **Suggested patch:** add a "## Outputs — autonomy readiness (optional, rolling-window)" section explaining: skip on day-1, populate from `outputs/<previous-14-days>/3/exec-log.jsonl` once the window exists.

3. **Reaffirm the standard log line schema.** Line 75 says `{ts, scope, check, result, severity}` — this should be tightened to `severity in {info, warn, error}` and `result in {pass, fail, skip}` so the logs are machine-greppable downstream. Worth aligning with whatever Layer 2's logs.jsonl emits (cross-check in Step 11).

4. **Cite the test contract.** Add a final "## Verified by" section pointing at `Rick_RnD/projects/bubble-ops-loop/tests/round-trip/test_layer4_three_outputs.py` so a future Rick editing the prompt sees the live contract immediately.

None of these block Step 9 GREEN — they harden the prompt for Step 11's robustness sweep.

---

## Acceptance criteria checklist

- [x] Test file with >=6 assertions — **11 written**
- [x] RED captured (test fails initially, no L4 outputs) — **10 fail, 1 skip**
- [x] Telegram trigger message documented — **Phase B above**
- [ ] After Joris triggers (or natural 22:00 fire), GREEN verified — **AWAITING**
- [x] Verdict: GREEN-NOW / AWAITING-TRIGGER / FAILED — **AWAITING-TRIGGER**
- [x] Any layers/4/PROMPT.md gaps documented for Step 11 — **4 additions, Phase D above**

---

## Appendix — exact paths the test checks (for 2026-05-20 UTC)

| Path | Layer | Mandatory? |
|------|-------|-----------|
| `outputs/2026-05-20/4/risk-brief.md` | hierarchy | yes |
| `outputs/2026-05-20/4/risk-kpis.yaml` | hierarchy | yes |
| `outputs/2026-05-20/management-export.yaml` | hierarchy (dept-level) | yes |
| `outputs/2026-05-20/4/summary.md` | 4-file standard | yes |
| `outputs/2026-05-20/4/artifacts/.gitkeep` | 4-file standard | yes (or non-empty dir) |
| `outputs/2026-05-20/4/logs.jsonl` | 4-file standard | yes |
| `outputs/2026-05-20/4/.last-run` | 4-file standard | yes |
| `outputs/2026-05-20/management-export.yaml::autonomy_readiness` | schema v3 optional | NO (skip on day-1) |
