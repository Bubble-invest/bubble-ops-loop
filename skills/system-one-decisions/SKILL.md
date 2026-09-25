---
name: system-one-decisions
description: >-
  How and when to use Jev / System-One-class models — cheap, fast, calibrated
  micro-decisions (yes/no "noul" probability, a 0-10 score, a choice among
  named options) instead of a full LLM turn. Use this whenever a task needs to
  classify, triage, filter, score, route, or gate MANY items cheaply: "is this
  X?" over a batch, pre-filtering before an expensive LLM call, bucketing
  emails/leads/replies/tickets into a few categories, a confidence-gated
  cascade (fast model handles the easy majority, escalate only the uncertain
  band), an action gate/guardrail before a risky tool call, or chaining
  several such classification steps into a faster/cheaper pipeline. Reach for
  this before writing a bespoke keyword filter or spending a full agent turn
  on a decision that's really "pick one of N options" or "true or false, how
  sure are you." Also load this when someone says "use Jev" or "System One."
  Covers picking a pattern, choosing local engines (decider/semif/laya on the
  Macs) vs. the official typesafe/jev-1.13 API, and the mandatory shadow ->
  gold-set -> calibrate -> gate rollout discipline before any Jev verdict
  touches real work.
---

# system-one-decisions — when & how to chain cheap classification steps

## Why this exists

Most agent work is not "think hard and write something new" — it's "which of these
few buckets does this thing fall into." Bucket an email, tag a lead's fit, flag whether
a reply is a bounce, decide if a passage is relevant, gate a risky tool call. Doing that
with a full Sonnet/Opus turn per item works, but it's the expensive way to answer a
question with a handful of possible answers. Jev / "System-One"-class models are built
for exactly this narrower job: given a short state and a typed question (`choice` among
named options, `score` on an ordinal scale, or `noul` — a calibrated P(true) for one
yes/no statement), they answer fast and cheap, and the caller — not the model — decides
what to do with the probability. Chaining several of these micro-decisions together is
how a dept turns a slow, expensive pipeline into a fast, cheap one: cheap steps first,
expensive model only where the cheap step is genuinely unsure.

This is a real, measured local capability today (`prototypes/jev-local/` in the R&D
workspace — three vetted local engines, benchmarked on JevBench + a French set + a real
fleet board sample, plus a completed pilot classifying wiki pages), not a hypothetical.
It is also, per that same research, **not a silver bullet** — read "The use/don't-use
checklist" below before reaching for it.

## Step 0: try a deterministic rule first

Before spinning up any model — local or API — check whether a plain rule already
answers the question. This is not a formality: on the fleet's own board-card
dept-classification task, a rule mapping each wiki page's `owner`/`domain`/top-level
directory to a department scored **P=0.88** on the gold set, beating every local
engine's zero-shot accuracy on the same task (decider 0.45, laya 0.40, semif 0.175 on
the 40-card internal set; see `references/engines.md`). If your state carries a
metadata field, a sender pattern, a fixed keyword, or an existing enum that already
determines the answer most of the time, write that rule and reserve the model for
what the rule can't resolve — as a pre-filter ahead of it, or as the escalation path
for what it doesn't cover. Also check for existing deterministic precedents already in
the fleet before building anything new (fleet standard, don't duplicate): Claudette's
IMAP `\Seen`/UID dedup gate, Maya's `bodacc-signals` `classify_annonce()`, Tonio's
`pep-france` trigram/token-set matcher. If a dept already solved this with code, reuse
it; a Jev call is for questions a rule genuinely can't answer well.

## The use / don't-use checklist

**Good fit — check all four:**
- **Volume is real** (roughly 50+ items, or a recurring per-item decision) — the
  parallel fan-out pattern's economics (12x cheaper, 10x faster per TypeSafe's own
  cookbook) come from amortizing the state's tokens across many questions; on a
  one-off decision this is not worth the plumbing.
- **The answer comes from a short text** the model can read in one pass — an email
  body, a ticket, a short excerpt, a trace summary. Not a decision that needs
  multi-hop reasoning, date arithmetic, or synthesizing many documents (every local
  engine tested is weak here — see `references/engines.md`).
- **A downstream check exists** — a human reviewing a digest, a full agent turn on
  the escalated band, a deterministic guard already in place. Never treat a Jev
  verdict as the last word standing alone.
- **Errors are reversible** — a mis-bucketed email, an over-escalated triage. Not a
  step where a wrong verdict causes real, hard-to-undo harm.

**Never a final call on:**
- **Fund execution or risk** (Ben's `execution.yaml`, `risk-control.yaml`) — fenced,
  deterministic, human/Opus-gated, explicitly off-limits per the fleet opportunity
  research.
- **Regulated financial/investment advice or compliance output** — the local engines
  tested here **missed every regulated-advice positive** in a French compliance test
  at standard thresholds (decider and semif both missed all 3; only the weakest
  all-around engine, laya, caught them — see `references/engines.md`'s "regulated-advice
  miss" section). Never gate a compliance decision on a single engine's threshold.
- **Client-facing output** — Tonio's `pep-france` PEP/sanctions matcher is deliberately
  kept as a hard deterministic scorer for exactly this reason; don't replace a
  client-facing regulated decision with a probabilistic model just because it's cheaper.

If a task fails any of the four "good fit" checks, or touches any of the three
"never" categories, don't build a Jev step for it — use the current method (rule,
full agent, or human).

## Pattern picker

Pick the shape of the decision first, then read the matching entry in
`references/patterns.md` for the full write-up, source citations, and caveats before
building. All patterns are engine-agnostic — same wire format, whichever backend you
pick below.

| You need to... | Pattern | Read |
|---|---|---|
| Cheaply narrow a large batch before an expensive step touches only the survivors | Model/skill router, qualify-cheap-generate-expensive (patterns 3, 4, 17) | `references/patterns.md#3-model-router` / `#4-skill--tool-router` / `#17-classify-then-generate-chaining-qualify-cheap-generate-expensive` |
| Answer several independent questions about the same item in one call | Parallel fan-out (pattern 1) | `references/patterns.md#1-parallel--speculative-fan-out` |
| Let the cheap model handle confident cases, escalate only the unsure band | Confidence-gated cascade (pattern 2) | `references/patterns.md#2-confidence-threshold-gating--system1system2human-cascade` |
| Combine several scored dimensions into one downstream number | Weighted composite (pattern 12) | `references/patterns.md#12-weighted-composite-score` |
| Double-check an output before trusting it (citation support, policy match) | Output verifier / trace evaluator (patterns 7, 8) | `references/patterns.md#7-output--citation-verifier` / `#8-trace-evaluator--llm-as-judge-replacement` |
| Gate a risky tool call (destructive action, scoped permission) | Action gate (pattern 5) | `references/patterns.md#5-action-gate--guardrail-before-a-tool-call` |

The confidence-gated cascade (pattern 2) is the one that shows up almost everywhere
else — most rows in `references/fleet-opportunities.md` are really "cascade, with a
different first-stage question." Its escalation band is illustrative, not fixed:
TypeSafe's own cookbook uses 0.30–0.70 as a starting point, not a guarantee — you
calibrate the real band on your own labeled data (see `references/eval.md`).

## Backend choice

Same wire format (`POST /v1/systemone`, `{"state": ..., "questions": {...}}`) across
every backend — swapping backends is a config change, not a rewrite. Use
`scripts/jev.py` for all of them; see its `--help` for the exact flags.

| Backend | Where it runs | Pick it when |
|---|---|---|
| `local-decider` (**default**) | Mac only (MPS), one engine at a time | Best all-rounder — near-best accuracy on every tested set, fastest/most consistent of the two decoder engines, well-calibrated. Start here. |
| `local-semif` | Mac only (MLX) | French/accuracy-critical text where latency isn't on a user-facing critical path — most accurate on the French test set (0.900 vs decider's 0.825), but has a heavy latency tail (p95 up to ~15s on long items). |
| `local-laya` | Mac only | A coarse pre-filter ONLY, ahead of a stronger step — 5-50x faster than the other two at a real accuracy cost; use it to cut volume, not to make the final call. |
| `openrouter` / `typesafe` (official `typesafe/jev-1.13`) | Any VPS dept, no special networking | The only currently-viable route for VPS-hosted depts (Ben, Maya, Miranda, Tony, Géraldine, Tonio) wanting Jev-class speed today — the local engines are Mac-only (decider is MPS/PyTorch, semif is MLX; neither is proven to port cleanly to Linux). |

**GDPR / EU residency is unconfirmed for the official API.** TypeSafe's own risk
notes flag data residency and sub-processor terms as things to verify, not settled
facts, and this research did not find a citable statement that the OpenRouter route
excludes EU regions. **Until that's confirmed in writing, no Jade/client personal
data or KYC/AML/patrimoine specifics goes to the official API** — use a local engine
or a minimized/pseudonymized state for anything sensitive. See
`references/engines.md` for the full residency note and the local-engine Linux-porting
caveat.

**Only one local engine runs at a time on a 16 GB Mac** (`scripts/jev.py start`
refuses to start a second one while another is listening — don't work around this;
the three engines were benchmarked one at a time for exactly this memory reason, see
`references/engines.md`).

## Mandatory rollout discipline

Every new Jev-backed decision in the fleet follows this order — do not skip a step
because the pattern "obviously" works. The local benchmark's own headline finding is
why: on the fleet's real board-card corpus, every engine scored *below* the trivial
"always guess the majority class" baseline (0.85) zero-shot — a plausible-sounding
classifier can still be worse than doing nothing.

1. **Shadow mode first.** Run the Jev step alongside the current method without
   acting on its output. Log everything (`scripts/jev.py ask` writes full JSONL
   results — keep them).
2. **Build a gold set** of at least ~50 labeled examples from your own domain, not a
   generic benchmark. (Pilot 1's wiki-intent gold set was 46 pages and its own summary
   flags that as small — more is better; treat anything near 50 as a floor, not a
   target.)
3. **Calibrate the threshold(s) on that gold set** — don't reuse the 0.30/0.70 or
   0.5 defaults from this document or any cookbook. See `references/eval.md` for the
   exact recipe (`scripts/jev.py eval`).
4. **Compare against both the trivial baseline and the current method.** A Jev step
   that beats "always guess the majority class" but loses to the dept's existing rule
   or full-agent judgment is not a win — don't ship it. `scripts/jev.py eval` reports
   the trivial baseline automatically alongside precision/recall/F1/ECE.
5. **Only then gate real work**, with the uncertain band still escalating to the
   current (more expensive) method — never a hard cutover. Re-check calibration
   periodically; a threshold tuned once on 50 examples can drift as real traffic
   differs from the gold set.

Always log decisions (state, questions, response, and what the caller did with it)
for later evaluation — an ungated Jev call you can't audit later is a Jev call you
can't improve or debug.

## Using `scripts/jev.py`

```bash
# start/stop/status (local engines only)
scripts/jev.py start --backend local-decider
scripts/jev.py status
scripts/jev.py stop

# ask: one or more questions against a batch of items (JSONL in, JSONL out)
scripts/jev.py ask --backend local-decider --questions questions.json \
  --items items.jsonl --out results.jsonl [--resume] [--parallel 4]

# eval: score results against a gold set
scripts/jev.py eval --results results.jsonl --gold gold.jsonl --out report.json
```

`ask` batches every question in `questions.json` into one request per item (the same
shared-prefix trick `run_engine.py` used in pilot 1 — render the item once, score N
questions off it) and supports `--resume` to skip items already present (by `id`) in
`--out`, so a long run surviving a kill/restart never re-pays for finished items.
`eval` reports precision/recall/F1/ECE (calibration) and P@1 per question, plus the
trivial "always predict the majority label" baseline for direct comparison — read the
eval number next to that baseline, not in isolation.

API keys for `openrouter`/`typesafe` backends come **only** from the environment
(`OPENROUTER_API_KEY` / `TYPESAFE_API_KEY`) — the script never accepts a key as a
flag, never prints one, and fails with a clear message (not a stack trace) if the
relevant variable is unset.

## Reference files

- `references/patterns.md` — the 19-pattern catalog, condensed with citations.
- `references/engines.md` — measured perf/memory/failure modes for each backend
  (governance over-trigger, cross-dept content bleed, the regulated-advice miss, the
  French-vs-English gap, GDPR residency status).
- `references/fleet-opportunities.md` — the dept-by-dept opportunity map and the top
  5 quick wins, each with real file paths.
- `references/eval.md` — the gold-set and calibration recipe in full, with the
  `jev.py eval` metrics explained.

## Full research trail

This skill condenses `~/claude-workspaces/Rick_RnD/prototypes/jev-local/` (board
`Bubble-invest/bubble-ops-board#1505`): `USE-CASES.md` (pattern catalog + opportunity
map), `README.md` (engine install/start/stop), `bench/results/20260925-seq/summary.md`
(cross-engine benchmark), and `pilot1-wiki-intent/results/20260925-pilot1/summary.md`
(a completed real-corpus pilot, including the "Checker addendum" deterministic-rule
finding that opens this skill). Read those directly for anything this skill's
condensed references don't cover.
