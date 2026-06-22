---
name: broker-adapter
description: Generic broker interface for the example portfolio agent. Documents the adapter pattern (get_positions / get_nav / submit_order) with an allow_live poka-yoke, a no-op stub for demos, and Alpaca shown as one worked example using env-var credentials. Plug your own broker SDK behind this interface — NO real keys ship here.
---

# broker-adapter — the generic broker interface

This skill is the **single seam** between the example agent and the outside world
where real money lives. The agent never imports a broker SDK directly; it talks to
a `BrokerAdapter`. To support a new broker you implement one class behind this
interface. To run a safe demo you use the built-in `StubAdapter`.

## Why a single adapter seam

1. **One fence, one poka-yoke.** Every mutating call goes through `submit_order`,
   which **refuses to place an order unless the caller explicitly passes
   `allow_live=True`**. This guard is independent of any agent prompt — accidental
   or exploratory calls cannot move money.
2. **Arm-state is testable.** `secrets_armed(broker)` is the *only* authority on
   whether a broker is live. The agent RUNS it; it never infers from memory.
3. **No SDK lock-in.** The agent reasons against `Position` / `AccountSummary` /
   `OrderResult` dataclasses, not a vendor's response shape.
4. **Credentials stay out of the repo.** Adapters read keys from environment
   variables only. Nothing is committed, nothing is hardcoded.

## The interface

`broker_adapter.py` defines:

```python
class BrokerAdapter(ABC):
    name: str

    def secrets_armed(self) -> bool: ...        # are creds present in the env?
    def get_account(self) -> AccountSummary: ...  # cash, equity, buying_power
    def get_positions(self) -> list[Position]: ...
    def get_nav(self) -> float: ...
    def submit_order(self, *, symbol, qty, side,
                     order_type="market", limit_price=None,
                     allow_live=False) -> OrderResult: ...
```

`submit_order` raises `BrokerAdapterError` if `allow_live` is not `True`, OR if
`secrets_armed()` is `False`. Both checks must pass before any real call.

## Picking an adapter

```python
import sys; sys.path.insert(0, "skills/broker-adapter")
from broker_adapter import get_adapter, secrets_armed

# Module-level helper the layer prompts call (RUN, never infer):
print(secrets_armed("alpaca"))     # True | False — reads the env this instant

adapter = get_adapter("alpaca")    # or "stub" for a safe demo
acct = adapter.get_account()       # raises cleanly if not armed
positions = adapter.get_positions()
nav = adapter.get_nav()

# Execution — only after an approved decision (MANDATE §6) AND armed secrets:
result = adapter.submit_order(symbol="ACME", qty=10, side="buy", allow_live=True)
```

`get_adapter(name)` resolves the registry in `broker_adapter.py`. The shipped
registry maps:

- `"stub"`   → `StubAdapter` — a deterministic no-op book for demos/tests. Never
  touches a network. `secrets_armed()` is always `True` so the demo can exercise
  the full L3 path safely.
- `"alpaca"` → `AlpacaAdapter` — the **worked example** in `adapters/alpaca.py`.
  Reads `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `ALPACA_PAPER` from the env. It
  imports `alpaca-py` lazily, so the example installs and the demo runs without
  the SDK present.

## Adding your own broker

1. Copy `adapters/_template.py` to `adapters/<yourbroker>.py`.
2. Implement the 5 methods against your broker's SDK.
3. Read every credential from `os.environ` — never a file in the repo, never a
   literal. Decide arm-state in `secrets_armed()` by checking the env vars are
   present (and optionally that a cheap read call succeeds).
4. Register it: add `"<yourbroker>": YourAdapter` to the `REGISTRY` in
   `broker_adapter.py`.
5. Keep the `allow_live` guard intact — call `self._require_live(allow_live)` at
   the top of `submit_order`, exactly like the template does.

## Credential handling (read this)

- **Keys come from the environment.** On a VPS, a SOPS-decrypted env file
  populates `os.environ`; locally, export them in your shell or a launchd plist.
  See `agents/ben/INSTALL.md`.
- **Nothing is stored in the repo.** No `credentials.py` with keys, no `.env`
  committed. The example ships with placeholders only.
- **Paper first.** The Alpaca example defaults to paper (`ALPACA_PAPER` unset →
  paper). Flip to live only when your mandate is signed and you mean it.

## Files

```
skills/broker-adapter/
├── SKILL.md                 # this file
├── broker_adapter.py        # interface + dataclasses + StubAdapter + get_adapter/secrets_armed
└── adapters/
    ├── _template.py         # copy this to add a broker
    └── alpaca.py            # worked example (env-var creds, alpaca-py lazy import)
```
