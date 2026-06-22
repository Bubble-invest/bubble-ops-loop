"""
broker_adapter — the generic broker interface for the example agent.

The agent NEVER imports a broker SDK directly. It talks to a `BrokerAdapter`,
which gives it three safety properties for free:

  1. `allow_live=True` poka-yoke — `submit_order` refuses to place an order
     unless the caller passes `allow_live=True` explicitly. Defense in depth,
     independent of any prompt.
  2. Run-only arm-state — `secrets_armed()` is the ONLY authority on whether a
     broker is live. The agent runs it; it never infers from memory.
  3. No SDK lock-in — the agent reasons against the dataclasses below, not a
     vendor response shape.

Credentials are read from os.environ ONLY. Nothing is hardcoded, nothing is
committed.

Usage:
    import sys; sys.path.insert(0, "skills/broker-adapter")
    from broker_adapter import get_adapter, secrets_armed

    print(secrets_armed("alpaca"))            # True | False (reads env NOW)
    adapter = get_adapter("stub")             # safe demo, no network
    print(adapter.get_nav())
    adapter.submit_order(symbol="ACME", qty=1, side="buy", allow_live=True)
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class BrokerAdapterError(Exception):
    """Raised on any adapter-side invariant violation (e.g. the live-mode guard)."""


def _log(msg: str) -> None:
    print(f"[broker_adapter] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Vendor-neutral data shapes the agent reasons against.
# --------------------------------------------------------------------------- #
@dataclass
class AccountSummary:
    cash: float
    equity: float
    buying_power: float


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


@dataclass
class OrderResult:
    accepted: bool
    broker_order_id: str | None
    symbol: str
    qty: float
    side: str
    detail: str = ""
    filled_qty: float = 0.0
    filled_avg_price: float | None = None


# --------------------------------------------------------------------------- #
# The interface every broker implements.
# --------------------------------------------------------------------------- #
class BrokerAdapter(ABC):
    name: str = "abstract"

    @abstractmethod
    def secrets_armed(self) -> bool:
        """True iff this broker's credentials are present + usable in the env.

        This is the ONLY authority on arm-state. RUN it; never infer from memory.
        """

    @abstractmethod
    def get_account(self) -> AccountSummary: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_nav(self) -> float: ...

    @abstractmethod
    def submit_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        limit_price: float | None = None,
        allow_live: bool = False,
    ) -> OrderResult:
        """Place an order. MUST refuse unless allow_live=True AND secrets_armed()."""

    # ----- shared guard every concrete submit_order calls first -----------
    def _require_live(self, allow_live: bool) -> None:
        """The poka-yoke. Call this at the top of every concrete submit_order."""
        if allow_live is not True:
            raise BrokerAdapterError(
                f"{self.name}: refusing to submit order without allow_live=True "
                "(safety poka-yoke — set it only after an approved decision)."
            )
        if not self.secrets_armed():
            raise BrokerAdapterError(
                f"{self.name}: secrets are NOT armed — cannot execute. "
                "The agent stays proposals-only until creds are present in the env."
            )


# --------------------------------------------------------------------------- #
# StubAdapter — a deterministic no-op book for demos + tests. No network.
# secrets_armed() is always True so the full L3 path can be exercised safely:
# the order is "accepted" against the in-memory book, no real money exists.
# --------------------------------------------------------------------------- #
class StubAdapter(BrokerAdapter):
    name = "stub"

    def __init__(self) -> None:
        # A tiny fictional book. Matches the synthetic seed fund (fake tickers).
        self._cash = 50_000.0
        self._positions: dict[str, Position] = {
            "ACME": Position("ACME", 100, 95.0, 10_200.0, 700.0),
            "GLOBEX": Position("GLOBEX", 40, 210.0, 8_600.0, 200.0),
        }
        self._order_seq = 0

    def secrets_armed(self) -> bool:
        return True  # the stub is always "armed" — it can never touch real money

    def get_account(self) -> AccountSummary:
        equity = self._cash + sum(p.market_value for p in self._positions.values())
        return AccountSummary(cash=self._cash, equity=equity, buying_power=self._cash)

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_nav(self) -> float:
        return self.get_account().equity

    def submit_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        limit_price: float | None = None,
        allow_live: bool = False,
    ) -> OrderResult:
        self._require_live(allow_live)
        self._order_seq += 1
        _log(f"STUB order accepted (no real money): {side} {qty} {symbol} [{order_type}]")
        return OrderResult(
            accepted=True,
            broker_order_id=f"STUB-{self._order_seq:04d}",
            symbol=symbol,
            qty=qty,
            side=side,
            detail="stub fill — synthetic book, no network, no real money",
            filled_qty=qty,
            filled_avg_price=limit_price or 100.0,
        )


# --------------------------------------------------------------------------- #
# Registry + helpers the layer prompts use.
# --------------------------------------------------------------------------- #
def _make_alpaca() -> BrokerAdapter:
    # Lazy import so the example installs + the stub demo runs without alpaca-py
    # or the adapters package on the path.
    import os

    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from adapters.alpaca import AlpacaAdapter  # noqa: E402

    return AlpacaAdapter()


# name -> zero-arg factory. Add your own broker here.
REGISTRY: dict[str, "callable"] = {
    "stub": StubAdapter,
    "alpaca": _make_alpaca,
}


def get_adapter(name: str) -> BrokerAdapter:
    """Resolve a broker adapter by name. Defaults to the safe stub if unknown."""
    factory = REGISTRY.get(name)
    if factory is None:
        raise BrokerAdapterError(
            f"unknown broker '{name}'. Known: {sorted(REGISTRY)}. "
            "Register yours in broker_adapter.REGISTRY."
        )
    return factory()


def secrets_armed(name: str) -> bool:
    """Module-level convenience: RUN the named broker's arm-state check NOW.

    The layer prompts call THIS. It instantiates the adapter and asks it — it
    never reads a cached value. A construction failure (missing SDK, etc.) is
    treated as 'not armed' rather than crashing the caller.
    """
    try:
        return get_adapter(name).secrets_armed()
    except Exception as e:  # noqa: BLE001 — arm-state must never crash a tick
        _log(f"secrets_armed({name!r}) -> False ({type(e).__name__}: {e})")
        return False


if __name__ == "__main__":
    # Smoke demo: exercise the stub end-to-end, no network, no real money.
    a = get_adapter("stub")
    print("armed:", a.secrets_armed())
    print("nav:", a.get_nav())
    print("positions:", a.get_positions())
    print("order:", a.submit_order(symbol="ACME", qty=1, side="buy", allow_live=True))
    try:
        a.submit_order(symbol="ACME", qty=1, side="buy")  # no allow_live -> refused
    except BrokerAdapterError as e:
        print("poka-yoke held:", e)
