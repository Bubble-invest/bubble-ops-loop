"""layer_templates.py — canonical PROMPT.md templates for the 4 loop layers.

Every ops-loop dept runs the same 4-moment day, so the scaffold ships a
canonical PROMPT.md per layer instead of empty dirs ({{OPERATOR}} 2026-06-01):

  L1 — The morning   : data update (pull state, prepare the day, surface a
                        short worklist)
  L2 — The research  : research & qualification (process one research-queue
                        item → produce its artifact / gate)
  L3 — The execution : action (execute one validated decision)
  L4 — The debrief   : reviewing (audit the day, write artifacts, AND the
                        daily Notion logbook entry)

The templates carry the shared skeleton (stateless-subagent framing,
idempotence guard, force-push, round counter, voice) with {placeholders}
the onboarding agent fills in with dept-specific substance. The L4
template includes the Notion logbook step so every current AND future
dept inherits it.

`render_layer_prompt(n, slug, display_name)` returns the PROMPT.md text.
"""
from __future__ import annotations

_COMMON_HEADER = """You are a **stateless** subagent spawned by the main session of \
{display_name}. You have no access to its context or to Telegram — you \
communicate only via the files you write to disk and the \
commits. You die after your run."""

_IDEMPOTENCE = """## Dispatch completion ownership (#1117)

`<today>` is the **Paris-local date** (Europe/Paris, `YYYY-MM-DD`) — the fleet \
timezone convention (board #1083 / #850). Use the value the main session hands \
you (`ctx['today']`, from `scripts.lib.dispatch_helpers.paris_today`) for EVERY \
`outputs/<today>/…` and `queues/research/<today>/…` path. Never recompute it \
from a box-UTC clock and never hand-type it from memory — the dispatch gate \
reads the SAME `ctx['today']`, so any other clock reopens the ~1–2h/day \
date-boundary miss #1083 closed.

Do not write `.last-run`, `.last-materialized`, or `dispatch.json`. The main
session owns dispatch state and calls `commit_dispatch` only after this worker
returns and its artifacts validate. Write real work artifacts only; a crash
before return intentionally leaves the mission due for the watchdog re-kick.
For an event mission, process only the exact trigger id the main session hands
you and return that id with your artifacts; unrelated pending items remain for
later workers/ticks.
"""

_ROUND_COUNTER = """## Return contract

Return the real artifact paths to the main session. Do not advance
`round_counter.json`; the parent does that inside `commit_dispatch` only after
your return validates. Commit+push artifacts via `bubble-git-guard push
--action runtime_write_own` unless an artifact already pushed."""


_L1 = """# Moment 1 — The morning (Layer 1 — data update)

""" + _COMMON_HEADER + """

## Why you were called

The main session spawned you at the current tick to **prepare the day**: refresh \
the state, spot what has moved since yesterday, and surface a short prioritized \
worklist for {display_name}'s day. You may also be called mid-day when an inbound \
management note arrives (issue #176 — C.mgmt dispatch branch), in which case your \
primary purpose is STEP 0-ter below; the rest of your steps still run.

## Required reads at start (STEP 0)

1. `../CLAUDE.md` + `../MANDATE.md` — who you are, your scope
2. `../dept.yaml` — recurring missions + data sources
3. {l1_sources}

### Inbound management notes (STEP 0-ter — run on EVERY L1 tick)

Tony (management) issues instructions through `queues/management/`. Notes arrive as \
`directive-<id>.yaml` OR under other names (e.g. `tony-<topic>-<date>.yaml` with \
`kind: management_note`). Do NOT rely on the `directive-` prefix alone.

1. List ALL `queues/management/*.yaml`. Keep the inbound ones addressed to you: \
   an inbound note is one whose `audience` includes `{slug}` OR whose `created_by`/`from` \
   is a manager (`tony`, `operator`, `operator2`) — i.e. NOT authored by you. \
   EXCLUDE your own outbound escalations (files you wrote, e.g. `rick-*.yaml`).
   For each inbound note NOT already in `queues/management/.consumed.json` \
   (tracked by `directive_id` or `id` field):
   - Read it. A directive has `directive_id`, `from`, `body`; a `management_note` \
     has `id`, `created_by`, `title`, `detail`.
   - **Act on it within your mandate this cycle** — fold the instruction into your \
     work. A directive is an order from management, not a suggestion; honour it unless \
     it conflicts with a hard guardrail (if so, do NOT act — log it and raise a \
     `strategic_question` card for {{OPERATOR}}).
   - Log outcome: append `{{"ts": ..., "directive_id": ..., "action": "applied|deferred|conflict", "note": ...}}` \
     to `logs.jsonl` and add the id to `.consumed.json`.
2. If no unconsumed inbound notes: continue silently.
3. **After reading (whether or not there were notes)**: stamp the scan marker so the \
   dispatcher knows this tick covered the queue:

```python
from scripts.lib.dispatch_helpers import write_last_mgmt_scan
from pathlib import Path
write_last_mgmt_scan(Path("."))  # writes queues/management/.last-mgmt-scan
```

Never block the tick on a directive read failure — log `[context-skip directives]` \
and proceed. Proceed immediately to STEP 1.

""" + _IDEMPOTENCE + """

## Your work (STEP 2 — update + briefing)

{l1_work}

Produce `outputs/<today>/1/morning_briefing.md` (clean markdown, readable in \
30 sec, executive-office voice): what moved, the day's priorities, and at \
most **one** strategic question for {{OPERATOR}}/{{OPERATOR_2}} if there is a real one. \
Materialize the worklist items in `queues/research/` so that L2 processes \
them. Also write `outputs/<today>/1/summary.md` (3-5 lines).

## Voice + audience

`morning_briefing.md` / `summary.md`: English, executive-office voice, \
readable by {{OPERATOR}}/{{OPERATOR_2}} in the cockpit (`/dept/{slug}`). No bare jargon.
"""


_L2 = """# Moment 2 — Research & qualification (Layer 2 — research)

""" + _COMMON_HEADER + """

## Why you were called

The main session saw **one** item in `queues/research/` (STEP C.2 of the \
/loop) and spawned you with its path. **You process A SINGLE item**; if there \
are several, the main session spawns several subagents in parallel.

## Required reads at start (STEP 0)

1. `../CLAUDE.md` + `../MANDATE.md`
2. `../dept.yaml` — for the missions/gates context
3. Your queue item (path passed in the task description)
4. {l2_sources}

""" + _IDEMPOTENCE + """

## Your work (STEP 2 — process the item → produce its artifact)

{l2_work}

If your work leads to a decision awaiting a human, **create a gate** \
in `queues/gates/<kind>-<slug>-<date>.yaml` (schema: `id`, `kind`, `slug`, \
`risk_level`, `requires_human: true`, `current_mode`, `gate_policy_id`, \
`actions: [approve, reject, modify, defer]`, actionable `summary`, + the \
content of the decision). Log in `logs.jsonl`.

## Voice + audience

Everything you write for a human: executive-office voice, English.
"""


_L3 = """# Moment 3 — The execution (Layer 3 — action)

""" + _COMMON_HEADER + """

## Why you were called

The main session saw **one** validated decision in `inbox/decisions/` (STEP C.3 of the \
/loop) — a gate approved by {{OPERATOR}}/{{OPERATOR_2}}, ready to execute. You execute \
**one** decision then you die.

## Mandatory pre-flight (STEP 0bis — guard-rails)

Before doing any external action, check the applicable guard-rails \
(`../policies/gates.yaml`: kill-switch, quiet-hours, quotas, action-policy). \
If a guard-rail blocks → ABORT, log the reason, the decision stays in \
`inbox/decisions/` for later — **this is for a TRANSIENT block** (one that \
may lift before midnight): return a blocked result with no completion commit, \
so a later tick today retries once it clears.

**Exception — STRUCTURAL human-supervised execution** (check `../dept.yaml` \
/ `../MANDATE.md` for this dept's execution mode, e.g. a board decision \
making a given decision kind permanently human-executed, like manual \
broker booking): if there is no autonomous way to execute this decision \
TODAY OR ANY DAY (not "not yet" — "not by me, ever"), retrying later today \
changes nothing. In that case: log the defer clearly (e.g. \
`"DEFERRED-to-human-supervised"`), leave the item in `inbox/decisions/` for \
a human to execute/archive, and return `DEFERRED-to-human-supervised` with the \
log artifact — the main session commits that terminal outcome after return. You DID your job for today \
(confirmed nothing here is autonomously executable), and Layer 4's evening \
debrief must not be blocked forever waiting on an execution that will never \
happen on its own.

## Required reads at start (STEP 0)

1. `../CLAUDE.md` + `../MANDATE.md` + `../policies/gates.yaml`
2. Your inbox item (the validated decision to execute)
3. {l3_sources}

""" + _IDEMPOTENCE + """

## Your work (STEP 2 — execute the decision)

{l3_work}

After successful execution: move the item to `inbox/decisions/.processed/` \
(so a future tick does not re-process it) and log in `logs.jsonl`. **Then \
archive the corresponding gate card** so `queues/gates/` shows only genuinely- \
open gates (#1076): \
`python3 -c "from scripts.lib.dispatch_helpers import archive_gate_card; \
archive_gate_card('.', '<id>', 'published')"` — the `<id>` is the same as the \
decision filename. If you instead **abandon** the decision (move it to \
`inbox/decisions/.abandoned/`), archive with `'abandoned'` in place of \
`'published'`. Both are atomic + idempotent (no-op if already archived), and a \
per-tick reconcile is the safety net if you forget — but do it inline. In case \
of failure after retries: leave the item + add `<id>.error` with the reason, \
the main session escalates to {{OPERATOR}}.

## Voice + audience

Concrete actions; factual traces in `logs.jsonl` + `summary.md`.
"""


_L4 = """# Moment 4 — The evening debrief (Layer 4 — review)

""" + _COMMON_HEADER + """

## Why you were called

The main session saw that **22:00 UTC ≤ now < 22:30 UTC** AND \
`outputs/<today>/4/.last-run` does not exist yet (STEP C.1 of the /loop). This is \
the mandatory daily audit — you run **once per day**, at the end of \
the day. No parallelism: you do the whole debrief in one run.

## Required reads at start (STEP 0) — exhaustive

1. `../CLAUDE.md` + `../MANDATE.md` + `../dept.yaml` + `../policies/`
2. **All** the day's L1/L2/L3 outputs: \
`outputs/<today>/{{1,2,3}}/{{summary.md,logs.jsonl}}`
3. {l4_sources}

## Your work (STEP 2) — the day's artifacts

Produce the canonical review artifacts (force-commit-push after each one):

1. `outputs/<today>/4/risk-brief.md` — the day's narrative brief: volumes, \
incidents, points awaiting {{OPERATOR}}, tomorrow's actions. {l4_brief}
2. `outputs/<today>/4/management-export.yaml` — export for Tony (format \
`schemas-draft/management-export.schema.yaml`).
{l4_extra}

## STEP 3 — The day's logbook (Notion, MANDATORY)

After the artifacts, write **one** honest logbook entry in the shared \
"Agent Logbook" notebook (Notion). It is the team's narrative journal — \
same spirit as the `main` entries (a short, factual story of the \
day, not a dry status). Two hats:

- **Archivist** (mechanical): re-read your day's outputs (L1-L4) + what you \
  actually did. Compose a `Summary` (short title, catchy but true) \
  and a `Content` (5-12 lines, past tense, factual, executive-office voice).
- **Observer** (judgment): if something in the day deserves \
  {{OPERATOR}}/{{OPERATOR_2}}'s attention tomorrow, say it in the content. Silent on \
  routine days, signal-bearing when there is something real. No placeholder \
  KPI, no keyword regex — it's your judgment (Bubble \
  principle: the intelligence is in the agent).

Write via the shared lib (the slug `{slug}` goes in the Agent column):

```bash
LOGBOOK_AGENT_ID={slug} NOTION_API_KEY="$NOTION_API_KEY" \\
  python3 ../../scripts/lib/notion_logbook.py write \\
    --title "<your Summary>" --body "<your Content>" \\
    --tags {slug} --for operator,operator2 --date <today>
```

If `NOTION_API_KEY` is not in the env, the lib skips cleanly (no \
crash) — log `logbook: skipped (no key)` and continue. One entry per day.

""" + _ROUND_COUNTER.replace("{n}", "4") + """

## Voice + audience

`risk-brief.md` + the logbook entry: English, executive-office voice, readable \
by {{OPERATOR}}/{{OPERATOR_2}}. The logbook is public within the team (shared notebook).
"""


# Per-layer placeholder defaults (the onboarding agent refines these per dept).
_DEFAULTS = {
    "l1_sources": "the data sources specific to the department (see dept.yaml::input_sources)",
    "l1_work": "Refresh the state from your sources, spot the previous day's movements, and build the day's worklist.",
    "l2_sources": "the department's research sources (see dept.yaml::input_sources)",
    "l2_work": "Process the item according to its `kind` (see dept.yaml::missions) and produce its artifact.",
    "l3_sources": "the department's execution tools/skills",
    "l3_work": "Execute the decision according to its `kind` (see dept.yaml::missions), with the guard-rails.",
    "l4_sources": "the day's KPIs (see policies/kpis.yaml if present) + the day's state changes",
    "l4_brief": "",
    "l4_extra": "",
}


def render_layer_prompt(n: int, slug: str, display_name: str,
                        overrides: dict | None = None) -> str:
    """Return the canonical PROMPT.md text for layer ``n`` (1-4)."""
    if n not in (1, 2, 3, 4):
        raise ValueError(f"layer must be 1-4, got {n}")
    body = {1: _L1, 2: _L2, 3: _L3, 4: _L4}[n]
    fields = dict(_DEFAULTS)
    if overrides:
        fields.update(overrides)
    fields.update(slug=slug, display_name=display_name, n=n)
    # The L1 idempotence block is shared; inject it after the "Why" for
    # layers that don't already embed STEP 1. Keep simple: append per-layer.
    text = body.format(**fields)
    # Insert the idempotence block right before "## Your work" for L1-L3
    # (L4 has its own ordering). For simplicity it's already implied by the
    # shared dispatch protocol in CLAUDE.md; templates reference STEP 1.
    return text
