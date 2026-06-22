---
name: fund-catalyst-calendar
description: Build and maintain a forward-looking catalyst calendar for holdings + watchlist names. Covers earnings dates, corporate events, regulatory decisions, macro releases. Use during morning catalyst-prep, pre-earnings positioning, or when the principal asks "what's coming up next week/month." Triggers on "catalyst calendar", "upcoming events", "earnings dates", "what's coming up".
allowed-tools:
  - Read
  - Edit
  - Write
  - Bash
  - WebSearch
  - WebFetch
metadata:
  source: "Adapted from a public equity-research catalyst-calendar pattern."
---

# fund-catalyst-calendar — forward-looking event tracker

## Why this exists

This is **portfolio-positioning-oriented**, not generic-calendar-oriented: every
entry must link to either (a) a holding you own, (b) a candidate you're
researching, or (c) a regime gate (CPI / Fed / NFP) you've pre-committed to act on.

## When to invoke

- **Morning catalyst-prep** — the L1 brief includes "Catalysts next 14d".
- **Pre-earnings positioning** — when a holding reports within 7 days.
- **Macro idea-generation** — weekly forward-looking macro events.
- **Ad-hoc** — "what's coming up" / "anything scheduled for X".

## The 4-step workflow

### Step 1 — Define the slice
Window: today / this week / next 14d / next 30d. Scope: holdings-only /
holdings + watchlist / macro-only / full.

### Step 2 — Pull events
- **Earnings**: market-data calendar for the next earnings date; cross-reference
  the company IR page for confirmed timing (pre-market / after-hours).
- **Corporate**: IR calendar (investor day, capital-markets day, AGM), filing windows.
- **Regulatory + macro**: central-bank meetings, CPI, jobs reports, official-stats schedules.
- **Industry-specific** (only when relevant to a held name).

### Step 3 — Position-impact tagging (the value-add)
For each event, tag:

| Field | Why |
|---|---|
| **Cluster touched** | Surfaces correlated-event risk (two holdings in one theme reporting the same week). |
| **Position size (% NAV)** | Prioritizes attention — a 0.5% holding's earnings ≠ a 5% holding's. |
| **Pre-committed trigger** | Connects the entry to invalidation logic (a known trim trigger that could fire on the read). |
| **Conviction at last revisit** | A high-conviction position pre-earnings = a thesis pressure test. |

### Step 4 — Render (3 surfaces)
- **A — brief embed** (concise, top of the morning brief).
- **B — weekly preview** (a markdown table by day: holdings reporting / watchlist /
  macro / industry).
- **C — JSON** for machine consumption by the loop.

## Discipline rules

1. **Every entry MUST have a "so what"** — an event with no portfolio relevance is noise.
2. **Macro gates must link to pre-committed triggers** — a CPI release without
   "what we'd do if hot" is data without discipline.
3. **Position-impact tagging is mandatory** — surfacing correlated event risk is
   the main value-add vs a generic financial calendar.
4. **Time-decay** — rebuild every morning; drop entries older than today.
5. **Honesty rule** — if a holding reports and you have no thesis-revisit prep
   done, flag it "PREP MISSING" — don't pretend you're ready.

## What this skill does NOT do

- Doesn't generate consensus estimates (use a pre-earnings preview for that).
- Doesn't write the post-event thesis update (that's `fund-thesis-format` + the weekly revisit).
- Doesn't predict event reactions.
