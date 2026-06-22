---
ticker: ACME
name: Acme Corp (FICTIONAL — example data)
sleeve: single_stock
weight_pct: 14.8
conviction: 4
ben_view: hold
opened_at: 2026-01-15
claim: "Buy ACME 3% NAV because its installed-base lock-in lets it raise prices ahead of inflation, over 12-24 months."
size_class: conviction
horizon_weeks: 52
review_by: 2026-09-15
asymmetry:
  up_pct: 35
  down_pct: -18
  ratio: "2:1"
invalidation_triggers:
  - type: hard_stop
    rule: "ACME -20% from entry ($95) = $76"
  - type: thesis_kill
    rule: "Net retention falls below 100% for 2 consecutive quarters"
  - type: trim_50
    rule: "A credible substitute ships at <half the switching cost"
---

# ACME — installed-base pricing power (EXAMPLE)

> Fictional position note. Illustrates the 7-part thesis format on fake data.

## 1. CLAIM
Buy ACME 3% NAV because its installed-base lock-in lets it raise prices ahead of
inflation, over 12-24 months.

## 2. CAUSAL MECHANISM
1. ACME's product is embedded in customer workflows → high switching cost.
2. Renewal contracts reprice annually → pricing power compounds.
3. Net revenue retention >115% means the base grows without new logos.
4. Margins expand as fixed R&D amortizes over a larger base.
5. → Free-cash-flow per share grows faster than revenue; multiple re-rates.

## 3. PORTFOLIO FIT
- **Diversifies:** low correlation to the GLOBEX consumer-cyclical bet (~0.3).
- **Duplicates:** partial overlap with the INITECH software-ETF backbone.
- **Cluster impact:** adds to a small "quality compounders" cluster, not yet concentrated.
- **Sleeve consumption:** single-stock sleeve at ~15% post-add (cap 28%).

## 4. INVALIDATION (kill conditions)
- HARD STOP: ACME -20% from entry ($95) = $76.
- THESIS KILL: net retention < 100% for 2 consecutive quarters (lock-in broken).
- TRIM 50%: a credible substitute ships at < half the switching cost.

## 5. SIZING + TIME HORIZON
Conviction size (3% NAV). Why not bigger: single-name concentration cap is 3%; the
moat is proven but the macro cycle is mid. Re-grade at the next earnings print.

## 6. ASYMMETRY
Up: +35% over 18m if pricing power compounds as modeled. Down: -18% if retention
slips. Ratio ≈ 2:1.

## 7. CONVICTION LEVERS
- Up: a price increase lands with no churn spike; a new module attaches >20%.
- Down: a single quarter of <105% retention; a well-funded substitute appears.

## Action log
- 2026-01-15: Opened starter at $95. Capsule: research-notes/2026-01-15-acme-initiation.md
