# MANDATE — bubble-ops-fixture

> Mandate doctrine: a department's mandate is the **load-bearing contract**.
> Layer 4 (`mandate-guardian`) audits every output against this file daily.
> Any breach → escalation (Telegram) → `outputs/<date>/4/alert.md`.

## §1 Mission

This fixture proves the **bubble-ops-loop pattern end-to-end** (per
[Notion v4 architecture](https://www.notion.so/bubble-ops-loop-Architecture-finale-simplifi-e-366cfc52064481dca58ae2a41e79e11a)
last-edited 2026-05-20T15:49 UTC). It carries no real business domain — its
only job is structural validation of the 4-layer OODA loop, the filesystem-as-bus
contract (`queues/` + `outputs/` + `inbox/`), and the subagent perm isolation
model. Real ops departments (Maya, Ben, Tony, Miranda, Eliot) fork this shape
once it is proven.

## §2 Périmètre (scope)

- **No real money.** No broker calls, no trade execution, no fund snapshots.
- **No real customers.** No CRM writes, no outbound messages, no prospect data.
- **No real external data.** WebFetch in Layer 1/2 is permitted for plumbing
  smoke-tests only (e.g. GitHub API self-check); no production data feeds.
- **GitHub commits to this repo only.** Token broker scoped to
  `vdk888/bubble-ops-fixture` exclusively per Notion v4 §"GitHub access model".

## §3 KPIs

The fixture's only KPIs are observability of the loop itself:

| KPI | Target | Measured by |
|---|---|---|
| Loop ticks observed per day | ≥ 60 (cadence_minutes=20 → ~72/day) | `outputs/<date>/*/.last-run` count |
| Outputs commits per day | ≥ 30 | `git log --since=24h --oneline \| wc -l` |
| `tests/run.sh` green rate | 100% on every HEAD | CI / local run |
| Layer 4 daily run produces all 3 outputs | 7/7 days | `outputs/<date>/4/{risk-brief,risk-kpis}` + `outputs/<date>/management-export.yaml` |

## §4 Hard limits

- **No live execution.** Layer 3 executes ONLY by appending to
  `outputs/<date>/3/exec-log.jsonl`. No broker, no email, no external API
  side-effects, period.
- **No external API calls beyond GitHub commit.** All `WebFetch` / `WebSearch`
  usage is read-only and limited to the loop's own self-audit.
- **No secrets in repo.** `.pem`, `.env`, `.key`, `.tokens.json` are
  `.gitignore`d AND verified absent by `tests/test_skeleton_completeness.py`.

## §5 Escalation

Any of the following triggers an immediate Telegram ping (per Notion v4
§"Human-in-the-loop gates"):

- Layer 4 detects mandate breach → `outputs/<date>/4/alert.md` + Telegram
- `tests/run.sh` fails on HEAD → block on next commit
- `/loop` tick misses by >2× cadence → systemd phone-home

## §6 Signature

| Field | Value |
|---|---|
| **Owner** | vdk888 (Joris) |
| **Co-builder** | Rick (R&D agent) |
| **Mandate version** | v1 (skeleton commit 2, 2026-05-20) |
| **Last-aligned-to Notion** | 2026-05-20T15:49 UTC |
| **Schema** | per `Rick_RnD/projects/bubble-ops-loop/schemas-draft/dept.schema.yaml` |
