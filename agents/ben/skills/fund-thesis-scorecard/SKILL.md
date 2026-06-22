---
name: fund-thesis-scorecard
description: Maintain a running scorecard tracking each thesis pillar's status over time. Pairs with fund-thesis-format. Use to systematize the "is my thesis still intact?" question — converts narrative review into a structured pillar-by-pillar trend read. Use on a weekly thesis revisit, when a major data point lands (earnings, regulatory, macro), or when the principal asks "is the [TICKER] thesis still working?".
allowed-tools:
  - Read
  - Edit
  - Write
metadata:
  source: "Adapted from a public equity-research thesis-tracker pattern."
---

# fund-thesis-scorecard — pillar-level thesis-status tracking

## Why this exists

`fund-thesis-format` defines HOW to write a thesis. This skill adds what a
narrative `## Action log` misses: a **structured pillar-by-pillar status grid**
that converts narrative drift into a measurable trend.

| Pillar | Original Expectation | Current Status | Trend |
|--------|---------------------|----------------|-------|
| Revenue growth >20% | On track | Q3 was 22% | Stable |
| Margin expansion | Behind | Margins flat YoY | Concerning |

This adds a discipline that's easy to skip: **honest, structured tracking of
disconfirming evidence.**

## When to invoke

- **Weekly thesis-revisit** (formalizes the read on the position you touch)
- **Post-earnings** (pair with a pre-earnings preview)
- **A major data point lands** (regulatory ruling, M&A, macro shift)
- **Quarterly review** for low-touch positions (every ~90 days)
- **The principal asks "is X still working?"** — pull the scorecard, present the
  trend, then the narrative.

## Where the scorecard lives

Embedded in the position's vault note, AFTER the INVALIDATION section and BEFORE
the `## Action log`.

## The 5-step workflow

1. **Load thesis context.** Read the position's §2 (CAUSAL MECHANISM). Each
   numbered link = one pillar.
2. **Define the original expectation per pillar**, quantified where possible
   (avoid "things should generally trend better" — make it falsifiable).
3. **Score the current status** from the latest data point:

   | Score | Meaning |
   |---|---|
   | ✅ Strong | Tracking ABOVE expectation |
   | ✅ Stable | Tracking ON expectation |
   | ⚠️ Watch | Slipping but within 1-σ |
   | ❌ Concerning | Materially behind |
   | 💀 Broken | Disconfirmed; thesis falsified on this dimension |

4. **Trend** — compare to the previous run (↑ improving / → stable / ↓
   deteriorating). The trend matters as much as the level: a "stable but
   deteriorating" thesis is one to trim early.
5. **Overall thesis health + conviction update:**

   | Pillar mix | Action |
   |---|---|
   | All ✅ Stable or stronger | Conviction reaffirmed/upgraded |
   | 1 ⚠️ Watch, rest stable | Unchanged; flag next quarter |
   | 2+ ⚠️ Watch OR 1 ❌ Concerning | Downgrade -1; review trim trigger |
   | 2+ ❌ Concerning OR 1 💀 Broken | Falsified on a major dimension — propose exit OR explicit "kill kept on hold" reasoning |

## Discipline rules

1. **Disconfirming evidence weighted equally** — numbers settle it, not how you
   feel about the company.
2. **Time-stamp every cell** — an undated data point can't track a trend.
3. **Falsifiable expectations only.**
4. **Frequency cap** — no more than weekly for a single position.
5. **Conviction-change is a decisions-row event** — a ≥1-level change files a
   `kind=thesis_update` row.

## What this skill does NOT do

- Doesn't replace the narrative `## Action log` (the scorecard is a snapshot; the
  log is the story).
- Doesn't auto-pull data (you populate "Current status" from research).
- Doesn't make the trim/upsize decision (it surfaces the signal; the principal adjudicates).
