# Synthetic example data

**Everything in this folder is FICTIONAL.** Fake tickers (`ACME`, `GLOBEX`,
`INITECH`, `HOOLI`, `UMBRELLA`, `STARKIND`), fake prices, fake NAV. No real
company, no real position, no real money. This exists so the example agent runs
out-of-the-box in a demo.

## Build the database

```bash
python3 agents/ben/data/seed_fund.py
```

This writes `fund.sqlite` (gitignored — it is generated, not committed) with a
minimal schema: `positions`, `decisions`, `trades`, `kpi_snapshots`,
`research_items`, pre-loaded with the synthetic book.

## What's here

```
data/
├── seed_fund.py          # builds fund.sqlite from scratch
├── fund.sqlite           # generated (gitignored)
├── vault/positions/      # example position notes (7-part thesis format)
└── research-notes/       # example dated research notes
```

The `vault/` notes follow the `fund-thesis-format` skill's 7-part structure so you
can see the shape the agent writes. The tickers are fictional; the *structure* is
the point.

## Making it yours

Replace this synthetic book with your own (or keep it synthetic for paper
trading). The agent reads positions/decisions/research from `fund.sqlite` and
theses from `vault/`; point those at your real book when you're ready.
