---
name: fund-source-verification
description: Source-tier classification + independent-verification rubric for any external signal (news, newsletters, paywalled wires, sentiment trackers). Load whenever you're about to act on a third-party claim or write "<outlet> reports X" in a memo.
---

# Fund source verification — tier discipline + corroboration rubric

Operating principle: **the source is a HYPOTHESIS, not an answer.** Cross-source
independent verification is THE workflow, not a polish step.

## When to invoke

Any research that turns a third-party claim into a vault note, a proposal, or KPI
commentary; whenever you're tempted to write "<outlet> reports X" without a
second-source check.

## Source tier definitions

Maintain a curated source list (one per source, with its tier). Tiers drive
corroboration math:

- **tier_a — high trust; institutional wire / central bank / primary data.** Solo
  corroboration accepted for "investigate deeper". (e.g. major financial wires,
  central-bank press, government statistical agencies.)
- **tier_b — medium trust; curated commentary / paywall mirrors.** (e.g. respected
  newsletters, thematic analysis.)
- **tier_c — noise-tolerant; contrarian/sentiment.** NEVER surfaces an idea alone.
  (e.g. social-sentiment trackers, aggregators.)

## Verification rubric

For every candidate, before you score, write up, or propose:

1. **Find ≥2 independent corroborating signals.** Independence means a different
   information chain (wire A citing wire B ≠ two sources — that is ONE chain),
   different measurement (a headline + a price/flow number = independent; two
   headlines saying the same = ONE point), or a different angle/horizon.
2. **Use ≥2 of these methods:** price-action check (bars on the named tickers),
   structural data (filings, official stats, policy docs — primary not
   commentary), your own telemetry (cluster memos, position vault, KPI history),
   independent coverage (web search across 2-3 outlets), logic/math sanity check
   (work the numbers yourself).
3. **Tier gating:**
   - tier_a solo OK if the rest of the evidence is structural (price action,
     cluster flag, calendar event)
   - tier_b needs ≥1 tier_a OR ≥1 additional tier_b
   - tier_c needs ≥2 independent tier_a corroborations — tier_c solo is effectively dead
4. **Paywall check:** if you only see the title/lede, treat the source as a
   hypothesis prompt, not evidence. Never fabricate what the paywalled body
   "probably" says.
5. **Recency / budget:** spend a few minutes per candidate. If it doesn't
   corroborate inside that budget, drop it — a real signal resurfaces tomorrow
   with more evidence.

## What to write when verified

```
Thesis source: <name, tier>
Verification:
  - <independent method 1>: <what you checked, what you found>
  - <independent method 2>: <what you checked, what you found>
Corroborated: yes / partial / no
```

When citing tier_c, ALWAYS pair it with the tier_a signal that promoted it.

## Failure modes

- **No corroboration inside budget** → mark `unverified — discarded`. Land it in
  the brief's `Discarded` section with one line on what failed — transparency matters.
- **Sibling-source dressing (FORBIDDEN):** never count a story cited by an
  aggregator as two sources. That is one chain.
- **Paywall fabrication (FORBIDDEN):** the lede is the only thing that exists for
  verification purposes.
- **Skipping the rubric to pad a TOP-3:** a brief with zero `Discarded` entries on
  an average-signal day is suspicious — the discard tail is a feature.

---

*Pure reference skill — no shell commands, no actions. The rubric only.*
