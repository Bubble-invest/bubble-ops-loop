# Ben — example portfolio-manager agent

> **A generic, synthetic example agent** for the `bubble-ops-loop` framework.
> It runs a 4-layer OODA investment loop, operates **proposals-only** until a
> mandate is signed and broker secrets are armed, and routes every real-capital
> action through a human execution gate. Point it at **your own** broker and
> **your own** mandate — the version shipped here trades on a fictional book of
> made-up tickers and cannot touch real money.

## Why this exists

We're not software developers. We built this for our own family office — a
small portfolio managed by an AI agent under a written mandate, with a human
holding the trigger on every order. It worked well enough that we generalized
the framework (`bubble-ops-loop`) and this worked example so others can stand
up the same shape: an accountable, auditable, *proposals-by-default* portfolio
agent that you control.

Ben here is a **reference implementation**, not our live agent. Everything in
this folder is generic or synthetic:

- The **mandate** ([`MANDATE.md`](MANDATE.md)) is a fictional risk perimeter on
  a fictional book. Replace it with your own.
- The **data** ([`data/`](data/)) is a tiny made-up fund (fake tickers like
  `ACME`, `GLOBEX`) so the agent runs out-of-the-box in a demo.
- The **broker wiring** is a single generic [`broker-adapter`](skills/broker-adapter/)
  skill documenting the interface (`get_positions` / `get_nav` / `submit_order`)
  with an `allow_live` poka-yoke. Plug your own broker SDK + credentials behind
  it. An Alpaca adapter is shown as one worked example using env-var creds — **no
  real keys ship here.**

## What Ben is

A portfolio-manager agent that runs a daily **OODA loop** across four layers:

| Layer | OODA | What it does |
|---|---|---|
| **L1 — Observe** | Data update | Snapshot positions/cash/P&L, consolidate, reconcile, build a justified research list. |
| **L2 — Orient** | Research | Write a structured investment case per item, apply the mandate filter + sizing, emit **proposals** (gate cards). Never executes. |
| **🚪 Gate** | Decide | A human approves / rejects / modifies each proposal. No order without it. |
| **L3 — Act** | Execution | Runs *only* approved decisions: plan → validate → execute. **Fenced** — no broker call without armed secrets + a signed mandate. |
| **L4 — Debrief** | Risk control | Independent audit, devil's-advocate counter-briefs, KPI scoreboard, feedback into tomorrow's research. |

The philosophy: **the agent reasons, the code computes, and the human holds the
trigger.** The agent is accountable through KPIs (L4) and free to act *only*
inside the mandate.

## The safety posture (read this before pointing it at real money)

1. **Proposals-only by default.** Until your mandate is signed AND broker
   secrets are armed on the box, Ben *cannot* execute — it only proposes.
2. **The execution gate is non-negotiable.** Every order passes 4 steps:
   propose → surface → human-decide → execute. See [`MANDATE.md`](MANDATE.md) §6.
3. **`allow_live=True` poka-yoke.** Every order method on the broker adapter
   refuses to place an order unless the caller explicitly passes
   `allow_live=True` — a defense-in-depth guard independent of the prompt.
4. **Hard risk limits are enforced, not suggested.** L2 won't propose an action
   that breaches a §4 limit; L4 audits for breaches after the fact.

## Layout

```
agents/ben/
├── README.md              # this file
├── INSTALL.md             # how to install (local launchd OR VPS systemd)
├── CLAUDE.md              # the agent persona / operating brain
├── MANDATE.md             # EXAMPLE mandate — replace with your own
├── dept.yaml              # dept config (slug, layers, missions, gate policies)
├── config.yaml            # notification routing (chat IDs come from env, not here)
├── layers/{1,2,3,4}/PROMPT.md   # the OODA layer prompts
├── policies/gate_policy.yaml     # the gate-policy pattern (kill switches, modes)
├── skills/
│   ├── broker-adapter/    # GENERIC broker interface + Alpaca worked example
│   ├── fund-source-verification/
│   ├── fund-thesis-format/
│   ├── fund-thesis-scorecard/
│   ├── fund-research-posture/
│   └── fund-catalyst-calendar/
└── data/                  # SYNTHETIC demo fund (fake tickers, seed script)
    ├── seed_fund.py       # builds data/fund.sqlite from scratch
    ├── README.md
    ├── vault/positions/   # example position notes (fake names)
    └── research-notes/    # example research notes (fake names)
```

## Quick start (demo, no broker, no real money)

```bash
# 1. Build the synthetic fund database
python3 agents/ben/data/seed_fund.py

# 2. Run the agent loop locally (proposals-only — no secrets armed)
#    See INSTALL.md for the launchd (local) / systemd (VPS) install.
```

With no broker secrets armed, Ben runs the full L1→L2 research path on the
synthetic book and produces **proposal cards** — it never reaches a broker.
That is the intended demo state.

## Making it yours

1. Rewrite [`MANDATE.md`](MANDATE.md) for your actual risk perimeter, sleeve
   caps, KPIs, and execution gate.
2. Replace [`data/`](data/) with your real book (or keep it synthetic for paper
   trading).
3. Implement a real adapter behind [`skills/broker-adapter/`](skills/broker-adapter/)
   for your broker, wiring credentials from environment variables (never commit
   keys).
4. Sign the mandate and arm the secrets only when you are ready for live
   execution. Until then it stays proposals-only — by design.

See [`INSTALL.md`](INSTALL.md) for the install paths.
