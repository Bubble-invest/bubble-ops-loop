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

This is a real, measured capability today (`prototypes/jev-local/` in the R&D
workspace — three vetted local engines AND the official Jev via OpenRouter, all
benchmarked on JevBench + a French set + a real fleet board sample, plus a completed
pilot classifying wiki pages), not a hypothetical. The official Jev is verified live
and is meaningfully more accurate than the local stand-ins on most of what was tested
(see "Backend choice" below) — but it is also, per that same research, **not a silver
bullet**: it still struggles on the fleet's own skewed board-card corpus, and it is
never trusted alone on anything gold-critical. Read "The use/don't-use checklist"
below before reaching for it.

## Step 0: try a deterministic rule first

Before spinning up any model — local or API — check whether a plain rule already
answers the question. Two separate results from this research make this concrete, not
a formality (two different corpora, don't conflate them):
- **On the wiki-page → operator-intent pilot** (pilot 1, 46 gold pages, 12 intents), a
  metadata rule (map each page's `owner`/`domain`/top-level directory → department)
  scored **P=0.88, R=0.63** on the 7 department-shaped intents — beating the SAME gold
  pages scored across all 12 intents by the local engines (decider micro-P 0.550,
  semif micro-P 0.619; see `references/eval.md`'s worked example). The pilot's own
  recommendation: dept intents come from the rule, engines are reserved for the 5
  fleet-wide intents the rule can't resolve.
- **On a separate benchmark** — real closed board cards' own `dept:*` labels (40 cards,
  the `internal_set` in the cross-engine benchmark) — a trivial "always guess the
  majority class" baseline scores 0.85 (the corpus is 85% one department), and every
  local engine scored *below* that baseline zero-shot: decider 0.450, laya 0.400,
  semif 0.175 (see `references/engines.md`'s "base-rate blindness"). The official Jev
  (`jev113`) also struggled on this exact task (0.421 accuracy — see `references/engines.md`)
  even though it clearly outperforms every local engine elsewhere; base-rate skew that
  none of the engines exploit zero-shot is a real limit, not an artifact of one backend.

If your state carries a metadata field, a sender pattern, a fixed keyword, or an
existing enum that already determines the answer most of the time, write that rule and
reserve the model for what the rule can't resolve — as a pre-filter ahead of it, or as
the escalation path for what it doesn't cover. Also check for existing deterministic
precedents already in the fleet before building anything new (fleet standard, don't
duplicate): Claudette's IMAP `\Seen`/UID dedup gate, Maya's `bodacc-signals`
`classify_annonce()`, Tonio's `pep-france` trigram/token-set matcher. If a dept already
solved this with code, reuse it; a Jev call is for questions a rule genuinely can't
answer well.

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
- **Regulated financial/investment advice or compliance output.** This stays a NEVER
  regardless of backend — even the official Jev's much better detection on this
  question (below) is a *flag for human review*, never a decision. On the French
  compliance test's `is_regulated_advice` question (8 items, 3 true positives): **the
  local engines missed every positive** — decider and semif each caught 0/3 (5/8
  overall — right on every negative, wrong on every positive; see
  `references/engines.md`'s "regulated-advice miss" section). The verified official Jev
  (`jev113`, via OpenRouter) caught **3/3 positives with 0 false positives among the 5
  negatives** — p(yes) = 0.92/0.77/0.93 on the true positives vs. 0.01-0.09 on the
  negatives, a wide, confident separation — making it a genuinely useful
  **flag-for-human-review** signal. But detecting well is still not the same as being
  trusted to decide: a regulated-advice call is never made by Jev, of any backend, full
  stop — route a positive/uncertain flag to a human, never to an automatic action.
- **Client-facing output** — Tonio's `pep-france` PEP/sanctions matcher is deliberately
  kept as a hard deterministic scorer for exactly this reason; don't replace a
  client-facing regulated decision with a probabilistic model just because it's cheaper.

If a task fails any of the four "good fit" checks, or touches any of the three
"never" categories, don't build a Jev step for it — use the current method (rule,
full agent, or human).

## Pattern picker

Pick the shape of the decision first, then read the matching entry in
`references/patterns.md` for the full write-up, source citations, and caveats before
building. All patterns are engine-agnostic — `scripts/jev.py` normalizes the (two)
underlying wire formats, so a pattern works the same regardless of which backend you
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

**The official Jev (`openrouter` backend, `typesafe/jev-1.13`) is now VERIFIED LIVE**
(230/230 calls OK, 2026-09-25 — `prototypes/jev-local/bench/results/20260925-openrouter/`)
and is **the recommended DEFAULT backend for INTERNAL Bubble Invest data** — it beats
every local engine on accuracy almost everywhere it was tested, at negligible cost
(~$0.025 per 1,000 decisions) and flat, fast latency (p50 ~0.5s, p95 ~0.6s, no tail).
`openrouter` and `typesafe` (direct API, unverified — see below) speak a **different
wire format** than the local engines: `POST <base_url>/alpha/decisions` with a `model`
field, vs. the local engines' `POST <base_url>/v1/systemone` — `scripts/jev.py` handles
this for you (`request_url_and_payload()`), you never hand-build the request. Use
`scripts/jev.py --help` for the exact flags.

| Backend | Where it runs | Pick it when |
|---|---|---|
| `openrouter` (**recommended default for internal data**, `typesafe/jev-1.13`) | Any dept, VPS or Mac, no special networking | VERIFIED LIVE. Clear accuracy leader: JevBench 0.793 (vs. decider 0.673, semif 0.687, laya 0.527), French 0.950 (highest of any engine on any set in this whole benchmark), and the only engine with genuinely **flat latency** across difficulty tiers (p50 ~0.5s, p95 ~0.6s — no blow-up on hard/long items, unlike semif's p95 up to 18.3s). Also the clear leader on regulated-advice **detection** (3/3, 0 false positives — see the use/don't-use checklist above; still never a final call). Cost is negligible: $0.025/1,000 decisions. **Internal Bubble data only for now** — see the data-residency rule below. |
| `local-decider` | Mac only (MPS), one engine at a time | Free, zero-data-leaves-the-machine fallback and the pick for EXTERNAL client data (below) — best all-rounder of the three local engines, near-best accuracy on every tested set, fastest/most consistent of the two decoder engines. |
| `local-semif` | Mac only (MLX) | French/accuracy-critical EXTERNAL-client text where latency isn't on a user-facing critical path (0.900 vs decider's 0.825 on French, but a heavy latency tail — p95 up to ~15s). |
| `local-laya` | Mac only | A coarse pre-filter ONLY, ahead of a stronger step — 5-50x faster than the other two at a real accuracy cost; use it to cut volume, not to make the final call. |
| `typesafe` (direct API) | Any dept, no special networking | The direct TypeSafe API — same `/v1/systemone`-style protocol as the local engines, but **not independently verified against a live account** in this build (no key/network available). Prefer `openrouter` (verified) unless you have a specific reason to go direct. |

**Data-residency rule (Joris, Telegram msg 9715, 2026-09-25 — "for now it's internal
use so it's ok"):**
- **INTERNAL Bubble Invest fleet data** — board cards, wiki, internal mail/ops, dept
  missions, anything that is Bubble's own operational data about Bubble itself — **MAY
  use the official Jev via OpenRouter now.** This is why `openrouter` is the
  recommended default above.
- **EXTERNAL client data** — client deliverables (e.g. Gefineo/Delahaye), and anything
  processed on behalf of a client such as PEP-France/OpenSanctions screening subjects
  — **stays LOCAL-ONLY** (`local-decider`/`local-semif`/`local-laya`) until EU
  residency and a DPA are confirmed in writing. TypeSafe's own risk notes still flag
  data residency and sub-processor terms as things to verify, not settled facts (see
  `references/engines.md`); this research did not find a citable statement that the
  OpenRouter route excludes EU regions. Nothing about the internal-use clearance above
  changes that for client data.
- If you're not sure which bucket a task's data falls into, treat it as external
  (local-only) until you've checked — this rule only widens the door for data that is
  unambiguously Bubble's own internal operations.

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

# ask against the official Jev (internal data only -- see "Backend choice" above),
# with a hard spend cap:
scripts/jev.py ask --backend openrouter --questions questions.json \
  --items items.jsonl --out results.jsonl --max-spend 0.50

# eval: score results against a gold set
scripts/jev.py eval --results results.jsonl --gold gold.jsonl --out report.json
```

`ask` batches every question in `questions.json` into one request per item (the same
shared-prefix trick `run_engine.py` used in pilot 1 — render the item once, score N
questions off it) and supports `--resume` to skip items already present (by `id`) in
`--out`. Every row is flushed and fsync'd to `--out` as soon as it's computed — not
batched up and written at the end — so a kill/restart mid-run never loses more than the
single in-flight request, and `--resume` genuinely continues from exactly where the run
stopped. On `openrouter`, each row also records the call's real `usage.cost` and a
running total; pass `--max-spend <usd>` to have the run stop issuing new requests once
that total is reached (local calls always cost $0, so the guard never fires for them).
`eval` reports precision/recall/F1/ECE (calibration) and P@1 per question, plus the
trivial "always predict the majority label" baseline for direct comparison — read the
eval number next to that baseline, not in isolation.

API keys for `openrouter`/`typesafe` backends come **only** from the environment
(`JEV_OPENROUTER_API_KEY`, the fleet-dedicated capped "fleet-jev" key provisioned into each VPS dept's env, with `OPENROUTER_API_KEY` as fallback / `TYPESAFE_API_KEY`) — the script never accepts a key as a
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
(the three-local-engine cross-engine benchmark), `pilot1-wiki-intent/results/20260925-pilot1/summary.md`
(a completed real-corpus pilot, including the "Checker addendum" deterministic-rule
finding that opens Step 0), and `bench/results/20260925-openrouter/summary.md` (the
official Jev, `typesafe/jev-1.13` via OpenRouter, re-run against the exact same three
benchmark sets — 230/230 calls OK, verified live 2026-09-25). Read those directly for
anything this skill's condensed references don't cover.
