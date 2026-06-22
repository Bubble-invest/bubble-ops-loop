---
name: fund-thesis-format
description: Canonical 7-part investment thesis structure. Use whenever the agent writes a position note, proposal memo, decisions.reasoning row, theme note, or a brief naming a specific bet. Every new thesis MUST follow this skeleton.
allowed-tools:
  - Read
  - Edit
  - Write
---

# fund-thesis-format — the 7-part structure

The single source of truth for HOW the agent articulates an investment bet. Every
new thesis MUST conform. The structure renders at three depths — vault file,
proposal memo, short message — using the same skeleton at different densities.
The idea is **progressive disclosure**: same content, three surfaces.

## Why this exists

1. **Comparability.** A standardized skeleton makes any thesis comparable to any
   other in the book — rank conviction, track it, audit kill triggers.
2. **Falsifiability.** Section 4 (INVALIDATION) is mandatory. No thesis enters the
   book without a written kill condition.
3. **AI-agent + human dual readability.** Machine-queryable frontmatter +
   human-readable prose + a phone-skim-friendly short render. One source, three surfaces.

## The 7 sections

Every thesis MUST have these 7 sections, in this order. Skip a section ONLY by
writing one line stating WHY it doesn't apply. A silently-omitted section is a bug.

### 1. CLAIM
A single declarative sentence stating the directional bet. No qualifiers.
Pattern: `[Buy / Sell / Hedge with] [INSTRUMENT] because [SPECIFIC OBSERVABLE WILL HAPPEN] over [HORIZON].`

### 2. CAUSAL MECHANISM
The chain of cause-and-effect connecting the §1 observable to the price moving in
your favor. Numbered chain, 3-7 links. Each link names an actor or mechanism, not
a vibe. This forces "I believe X because Y because Z" — no pattern-matching.

### 3. PORTFOLIO FIT
Where this slots in the existing book. CITE NUMBERS — correlations to current
holdings, cluster impact, sleeve consumption. Required: what it diversifies, what
it duplicates, cluster impact, and how much of the relevant sleeve it consumes
post-add.

### 4. INVALIDATION (kill conditions) — MANDATORY, NUMERIC
The most important section. What concrete, observable, numeric event would PROVE
this thesis wrong? Must be **numeric** ("rate spikes >5.5%" not "rates rise"),
**time-bound** ("5+ consecutive sessions"), **distinct** (full-exit vs trim vs
"watch closely" triggers separated), and **asymmetric to entry** (exit triggers
tighter than entry conviction). Three classes: HARD STOP (full exit, no judgment),
THESIS KILL (the §2 mechanism is broken), TRIM TRIGGER (partial exit on heightened
risk that doesn't fully break the thesis). A thesis without numeric invalidation
is faith, not investment.

### 5. SIZING + TIME HORIZON
Why THIS size, not bigger or smaller. Why this horizon to evaluate. Size class:
STARTER (1-2% NAV) / CONVICTION (3-5% NAV) / CORE (6%+, only after multi-month
positive evidence) / HEDGE (sized to the risk it offsets). State when you'll re-grade.

### 6. ASYMMETRY
Up-case vs down-case rough magnitudes.
Format: `Up: +X% over <horizon> if <condition>. Down: -Y% over <horizon> if <condition>. Ratio ≈ X:Y.`
If asymmetry is ≤ 1:1, the thesis MUST be framed explicitly as a HEDGE.

### 7. CONVICTION LEVERS
The middle ground between "intact" and "killed." Concrete observables that would
move conviction up or down WITHOUT triggering full exit/upsize. Bulleted,
declarative + observable.

## Surface 1: VAULT FILE — the reference implementation

`data/vault/positions/<TICKER>.md`. YAML frontmatter (machine-queryable) carries
`ticker`, `weight_pct`, `conviction`, `claim`, `size_class`, `horizon_weeks`,
`asymmetry`, `invalidation_triggers`, `review_by`, etc. Body = prose sections 1-7
with exact headers (`## 1. CLAIM`, …). Length 400-800 words.

## Surface 2: PROPOSAL MEMO + decisions.reasoning

Opens with a 6-line summary block (CLAIM / SIZE / HORIZON / CONVICTION / ASYMMETRY
/ KILL), then sections 1-7 in detail. The `decisions.reasoning` column gets the
6-line block + 2-3 sentences per section.

## Surface 3: SHORT MESSAGE — 5-line compact render

The phone-friendly version (header + CLAIM + WHY + KILL + ASYM). Line 3 (WHY) must
cite at least one concrete number. Don't drop the WHY line — pure CLAIM + KILL is
just a trade alert; you're aiming for analysis the principal can challenge.

## Failure modes to avoid

- **Filling sections with vibes.** §2 is the test — if you can't write 3-7 specific
  links, you have a hunch, not a thesis.
- **Vague invalidators.** Numeric + time-bound or it doesn't count.
- **Sizing without horizon.** Say when you'll re-grade.
- **Pretending a hedge is a directional bet.** If §6 ratio ≤ 1:1, frame it as a hedge.
