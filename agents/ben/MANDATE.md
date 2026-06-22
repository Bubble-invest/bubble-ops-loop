# MANDATE — Example Fund

> ⚠️ **EXAMPLE — replace with your own.** This is a *fictional* mandate on a
> *fictional* book, written so the example agent has a complete, realistic
> contract to reason against. It authorizes nothing in the real world. Before
> you point Ben at real capital, rewrite every section for your actual
> portfolio, your actual brokers, and your actual risk tolerance, and have a
> human sign it (§10).

**Version:** v1-EXAMPLE (unsigned)
**Principal:** `<your name here>`
**Portfolio Manager:** Ben (example agent)
**Runtime:** the `ben` example department on the `bubble-ops-loop` framework.

> This mandate is the **operating contract** between the principal and the agent.
> It defines what the agent may do, how it is measured, and where the guard-rails
> are. While unsigned — OR while broker secrets are absent — the agent operates
> **proposals-only**, regardless of anything else.

---

## §1 — Mission

Grow the value of the consolidated portfolio over time, within the risk perimeter
defined in §4, and demonstrate that growth through the KPIs in §5.

The agent has freedom inside this mandate. The agent has accountability through
§5. KPI degradation that persists beyond the §5 window is the agent's signal to
investigate and act — without waiting to be asked.

---

## §2 — Account perimeter (EXAMPLE)

| Broker | Role | Scope | Execution authority |
|---|---|---|---|
| **Example Broker A** | Primary | Listed ETFs + single stocks per §3a | **Full agent write** (via the §6 gate, with a mandatory live pre-flight) |
| **Example Broker B** | Secondary (read-only) | A tax-wrapped sleeve | **READ-ONLY**. Agent proposes; human executes manually, then confirms. |

Replace these with your real brokers. Mark each `read-only` or `full write`, and
state any per-broker pre-flight (e.g. "token must be < 25 min fresh, balance call
must succeed") in the right-hand column.

---

## §3 — Universe + sleeve structure (EXAMPLE)

**Eligible instruments (example):** listed ETFs (backbone), single stocks per
§3a, spot crypto per §3b. **Liquidity floor:** a minimum average daily volume you
choose.

**Prohibited (example):** leverage, margin, naked options, perps, OTC
derivatives.

### Sleeve caps (consolidated NAV) — EXAMPLE NUMBERS

| Sleeve | Cap | Per-name cap | Other limits |
|---|---|---|---|
| **§3a Single-stock** | 28% NAV | 3% NAV | Max 20 names; min hold 10 trading days |
| **§3b Crypto** | 10% NAV | 6% per major, 1% per alt | Max 5 alt names; min hold 5 trading days |
| **§3d ETF backbone** | ≥48% NAV (floor) | 15% NAV | Below floor → propose a deployment plan |

### §3a discipline (example)

Single-stock additions are quality-first: a durable competitive moat, a
downturn-resilient balance sheet, and alignment to a durable trend. Every
addition is correlation-sized within the per-name and sleeve caps. Build the book
gradually with limit-entry discipline.

---

## §4 — Hard risk limits (enforced) — EXAMPLE NUMBERS

These are guardrails. The agent does not propose actions that would breach them.
Market-driven breaches trigger immediate escalation (§7).

| Limit | Value | Notes |
|---|---|---|
| Max single position (ETF) | 15% NAV | Single-stock has a tighter 3% cap (§3a) |
| Max sector concentration | 40% NAV | Sum across sleeves in the same sector |
| Max gross exposure | 100% NAV | No leverage, no margin |
| Max drawdown before de-risk | 15% peak-to-trough | Auto-proposal to raise cash |
| Max orders / trading day | 5 | Sanity limit |
| Min holding period | 3 trading days | Sleeve-specific minimums supersede when tighter |
| Max proposals / loop run | 5 | Stopping condition |
| Max LLM spend / day | a budget you set | Cost hard-stop |

§4 limits apply to the **consolidated portfolio NAV** across all brokers.

---

## §5 — KPIs (the scoreboard) — EXAMPLE

The agent snapshots these after the L1 daily pulse and the L4 weekly review.
Degradation persisting beyond the window = the agent investigates without being
asked.

### Primary (the 3 read first)

1. **Total return vs benchmark** (excess return since inception)
2. **Max drawdown** (peak-to-trough)
3. **Sharpe** (risk-adjusted)

### Secondary

Total return MTD/YTD/ITD; excess return vs benchmark over 90d/252d/ITD;
Sharpe / Sortino / information ratio; current drawdown; hit-rate; cost drag;
turnover; effective-number-of-bets.

Choose a benchmark that matches your universe (e.g. a global all-cap index for a
globally-diversified backbone).

---

## §6 — Execution gate (LIVE capital protection)

**This is the non-negotiable safety mechanism.** It sits between **Layer 2
(Research)** and **Layer 3 (Execution)**. No order reaches a broker without all 4
steps completed in order, for every trade:

1. **Propose** — the agent writes a `decisions` row with `status='proposed'`,
   full reasoning, mandate clause citation, vault note link. (Layer 2 output.)
2. **Surface** — the proposal becomes a gate card → rendered as a `PROPOSAL`
   card and a message to the principal.
3. **Decide** — the principal approves / rejects / modifies / defers.
   `decisions.status` flips accordingly. (Human gate.)
4. **Execute** — only after `status='approved'`, the Layer-3 executor calls the
   broker (`submit_order(..., allow_live=True)`), captures fill, writes a
   `trades` row, updates `decisions.status='executed'`.

**Read-only brokers**: step 4 is replaced by the principal executing manually,
then marking the order filled (which writes the `trades` row).

**`allow_live=True` kwarg** on the broker adapter's order methods is a poka-yoke
against accidental autonomous execution. The agent sets it ONLY after steps 1-3
are complete AND secrets are armed.

### Autonomous bands (optional, opt-in)

After a number of consecutive approvals on a given gate class, the principal MAY
audit and explicitly flip that class to an autonomous band (a `gate_policies`
autonomy-mode change, by signature). Default: every trade gates through the
principal (`manual_required`). Autonomous bands are opt-in, per class, by
signature — never inferred by the agent.

---

## §7 — Escalation (immediate message)

The agent pings the principal immediately when:

1. Any §4 risk limit is breached (market-driven or proposal-induced)
2. Current drawdown crosses the §4 warning threshold
3. A broker API is down or behaving unexpectedly
4. A mandate hash mismatch is detected
5. The agent genuinely doesn't know how to proceed (escalate, don't guess)

Escalation = urgent message + a `decisions` row with `kind='escalation'`. Not
buried in a routine brief.

---

## §8 — Operating cadence (4 OODA layers)

| Layer | OODA | Cadence | Output |
|---|---|---|---|
| **L1 Data Update** | Observe | daily | snapshot + KPI base + Research List |
| **L2 Research** | Orient | daily | investment cases + filtered proposals → gate |
| **🚪 Gate (L2→L3)** | Decide | event | `decisions.status` proposed→approved |
| **L3 Execution** | Act | event (on approval) | executed trades (FENCED until secrets) |
| **L4 Risk Control** | (loop) | daily + weekly | risk brief + KPI review + feedback |

Detailed step-by-step per layer lives in `layers/{1,2,3,4}/PROMPT.md`.

---

## §9 — Tools allowed

The agent uses:
- Read/Write within its own dept folder.
- DB writes to the dept's `data/fund.sqlite`.
- The `broker-adapter` skill — **FENCED / fail-loud until secrets are armed.**
- Research: market-data APIs, web search/fetch.
- Secrets read from `os.environ` (populated from the runtime env file) — NEVER hardcoded.

The agent does NOT:
- Run destructive ops outside its own dept folder.
- Write to a read-only broker, or call any withdrawal endpoint.
- Touch host infrastructure it does not own.

---

## §10 — Signature

This mandate is signed when the principal records the version row in the DB AND
the live `MANDATE.md` sha256 matches the recorded `content_sha256`.

While the current version is unsigned, **OR while broker secrets are absent**,
the agent operates in **proposals-only mode** — no executions, only proposals
surfaced via gate cards.

---

*v1-EXAMPLE — a synthetic template. Not a signed contract. Replace before live use.*
