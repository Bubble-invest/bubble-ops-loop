# Fleet opportunity map (condensed from `jev-local/USE-CASES.md` §B/§C)

Full source with every row and the "do-not-touch" rationale spelled out in detail:
`~/claude-workspaces/Rick_RnD/prototypes/jev-local/USE-CASES.md`. Paths below are as
read via `gh api` / local `Read` on 2026-09-25 — verify a dept's file still lives there
before building against it, files move.

## Top 5 quick wins (ranked by gain × volume / risk)

**1. Claudette's mail-brief bucketing.**
`vdk888/bubble-claudette-workspace/missions/mail-brief.md` — buckets every email into
📦Actionnable / ✅Bouclé / 📌Pour info / 🗑️Bruit (~40 emails/day across 3 mailboxes).
Highest sustained volume in the fleet, lowest risk (internal triage feeding a digest a
human reads before acting). Design: keep the existing deterministic dedup gate (IMAP
`\Seen`/UID watermark, §4 of the mission) first; every new/unseen email then gets one
Jev `Choice` call (bucket) plus, for anything routed 📦, a `Noul` "needs same-day
action?" — both in one parallel fan-out (pattern 1). Only 📦 and ambiguous 📌 reach the
full agent's drafting attention. Confidence below ~0.6 on the bucket falls back to
today's full-agent classification (pattern 2), so recall doesn't regress.

**2. Tony's `inbox_watch` ping-worthy gate.**
`Bubble-invest/bubble-ops-tony/missions/inbox_watch/PROMPT.md` — "ping-worthy?" per
inbound message/calendar delta, every 2h 08:00-20:00 Paris. Already the fleet's best
existing example of right-sizing the model (explicitly "cheap, runs on Sonnet" in-file)
— but still spends a full Sonnet turn on every message check. Design: Jev Noul
"ping-worthy" scores every new message/delta in parallel; below ~0.3 is silently logged
(no Sonnet call), above ~0.7 pings directly with a Jev-drafted one-line reason, and only
the 0.3-0.7 band still goes to Sonnet for a full read. The gain is about volume, not
escaping an expensive model — Jev makes the routine, non-ping majority effectively free.

**3. Maya's `signal-gate` ICP scoring.**
`Bubble-invest/bubble-ops-maya/skills/signal-gate/SKILL.md` — 5 weighted dimensions →
Tier 1/2/3 → action enum, daily Morning Sync batch, no human in the loop per-lead today.
Already the closest thing to Jev-shaped in the fleet (a deterministic Python scorer,
`lib/signal_gate.py`). Design: keep the weighted-combination formula (pattern 12)
unchanged in code, replace the hand-coded per-dimension scoring rules with 5 parallel
Jev Score questions per lead (pattern 1), so the scorer generalizes past phrasing the
rule table can't anticipate, without adding a full-LLM call per lead.

**4. Géraldine's `categorisation` confidence-tiering.**
`Bubble-invest/bubble-ops-accountant/skills/categorisation/SKILL.md` — the skill's own
stated roadmap already says "manual-first, growing into auto-pass for trusted vendors."
Design: per Qonto movement, Jev Choice picks the Dougs category among the known set,
Jev Noul checks agreement with Dougs' own auto-category; high-confidence + agreement on
a known/repeat vendor auto-passes into the daily digest as a proposal, anything
new/ambiguous/disagreeing routes to the skill's existing "à qualifier" list. Risk is
medium, not low (money-adjacent) — but Dougs stays the source of truth for the filed
number either way; Jev only ever speeds up the proposal step.

**5. Generalize Rick's own Haiku "already-done?" pre-check fleet-wide.**
`~/claude-workspaces/Rick_RnD/layers/3/PROMPT.md` "worker doctrine" step 0 — already a
working precedent in production, not a new build: a cheap model gates whether a
Sonnet/Opus worker dispatch is actually needed before paying for the real turn. Design:
every dept that dispatches subagent workers on discrete tasks (Ben's research tagging,
Maya's reply classification, Tony's KPI drift checks) gets the same cheap-model
pre-check wired in front of dispatch. Lower per-instance gain than rows 1-4, but it
multiplies across every dept and needs no new pattern — only replication of something
already proven.

## Other mapped rows (selected — see USE-CASES.md §B for the full table)

| Dept | Decision step | Pattern | Risk | Note |
|---|---|---|---|---|
| Maya | Reply classify: positive/neutral/negative/meeting-booked + bounce/autoreply flags (`reply_handler.yaml`) | Router (3/4) + fan-out (1) | low | Currently a full agent turn per reply — squarely Noul/Choice fan-out shaped |
| Content (Miranda) | X-post notable/skip triage before deep research (`research-x-signal/SKILL.md`) | Pre-filter (3/4) | low | Pure volume triage ahead of an expensive research step |
| Ben | Source-tier tagging (tier_a/b/c) in `fund-source-verification/SKILL.md` | Router (3) | medium | Only the *tagging* half is Jev-shaped — the verification judgment (does this actually corroborate) stays on the full model |
| Ben | Weekly thesis pillar status tag (`fund-thesis-scorecard/SKILL.md`) | Router (3) | medium | Low volume (~20/week) so absolute savings are small; a human/full agent still writes the conviction narrative |
| Rick | Board-card risk classification (`missions/kanban-board.md`) | Confidence-gated cascade (2), asymmetric | low-medium | Jev may only ever *lower* confidence toward `needs:human`, never independently greenlight `risk:low`+`agent:ready` |

## Explicit do-not-touch list

- **Ben's `execution.yaml`** (pre-trade validation gate) and **`risk-control.yaml`**
  (daily mandate audit) — fund execution/compliance, fenced broker scripts + Opus,
  irreversible and financial. No Jev pattern proposed; this is the fleet's clearest
  "never" case.
- **Tonio's `pep-france/packages/core/src/matching.ts`** (PEP/sanctions name matching) —
  already correctly deterministic (trigram + token-set similarity), explicitly never a
  boolean is/isn't-PEP. Cited in the research as the clearest example of "this must stay
  deterministic, not move to a bigger probabilistic model."
- **Miranda's `proofread-visual/SKILL.md`** — needs generated corrections (text diffs),
  not a decision; Jev cannot produce the correction text (pattern-catalog caveat 1: it
  answers typed questions, it doesn't generate free text).

## Existing deterministic precedents (reuse before building — per Step 0 in SKILL.md)

- Claudette's IMAP `\Seen`/UID dedup gate (`mail-brief.md` §4).
- Maya's `bodacc-signals/SKILL.md` `classify_annonce()` (`lib/bodacc_client.py`).
- Tony's `kpi-aggregator/SKILL.md` `aggregate()` — deterministic Python scorer.
- Géraldine's `inbox-invoice-sweep/SKILL.md` — mostly deterministic keyword/PDF filter
  already, a Jev Noul "is-invoice" pre-filter would only add marginal recall for
  differently-worded invoices without an attachment named "facture".
