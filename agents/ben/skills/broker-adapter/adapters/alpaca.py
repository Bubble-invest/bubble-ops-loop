"""
alpaca — a WORKED EXAMPLE adapter for the generic broker interface.

This shows how to wire ONE real broker behind `BrokerAdapter`. It is a reference,
not a tested production integration. Credentials come from environment variables
ONLY — no keys ship in this repo.

Environment variables:
    ALPACA_API_KEY      your Alpaca key id
    ALPACA_SECRET_KEY   your Alpaca secret key
    ALPACA_PAPER        "false" for live; anything else (or unset) => PAPER (default)

The alpaca-py SDK is imported lazily, so the example installs and the stub demo
runs even when alpaca-py is not present. Install it only when you actually wire
Alpaca:  pip install alpaca-py

Safety: every order goes through self._require_live(allow_live), so an order can
only be placed after an approved decision (allow_live=True) AND with armed
secrets. Defaults to PAPER endpoints; you opt into live explicitly.
"""

from __future__ import annotations

import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from broker_adapter import (  # noqa: E402
    AccountSummary,
    BrokerAdapter,
    BrokerAdapterError,
    OrderResult,
    Position,
)

_ENV_KEY = "ALPACA_API_KEY"
_ENV_SECRET = "ALPACA_SECRET_KEY"
_ENV_PAPER = "ALPACA_PAPER"


class AlpacaAdapter(BrokerAdapter):
    name = "alpaca"

    def __init__(self) -> None:
        self._key = os.environ.get(_ENV_KEY)
        self._secret = os.environ.get(_ENV_SECRET)
        # Default to PAPER unless explicitly told otherwise. Live is opt-in.
        self._paper = os.environ.get(_ENV_PAPER, "true").strip().lower() != "false"

    @property
    def mode(self) -> str:
        return "PAPER" if self._paper else "LIVE"

    def secrets_armed(self) -> bool:
        # Arm-state = both creds present in the env. (You could additionally try a
        # get_account() call and return False on failure — but never raise here.)
        return bool(self._key and self._secret)

    def _trading_client(self):
        if not self.secrets_armed():
            raise BrokerAdapterError(
                "alpaca: ALPACA_API_KEY / ALPACA_SECRET_KEY absent from the env."
            )
        try:
            from alpaca.trading.client import TradingClient  # lazy import
        except ImportError as e:  # noqa: F841
            raise BrokerAdapterError(
                "alpaca: alpaca-py not installed. `pip install alpaca-py` to wire "
                "this broker (the stub demo does not need it)."
            )
        return TradingClient(self._key, self._secret, paper=self._paper)

    def get_account(self) -> AccountSummary:
        acct = self._trading_client().get_account()
        return AccountSummary(
            cash=float(acct.cash),
            equity=float(acct.equity),
            buying_power=float(acct.buying_power),
        )

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for p in self._trading_client().get_all_positions():
            out.append(
                Position(
                    symbol=p.symbol,
                    qty=float(p.qty),
                    avg_entry_price=float(p.avg_entry_price),
                    market_value=float(p.market_value),
                    unrealized_pl=float(p.unrealized_pl),
                )
            )
        return out

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
        self._require_live(allow_live)  # poka-yoke + arm-state guard

        from alpaca.trading.client import TradingClient  # noqa: F401
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        client = self._trading_client()
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        if order_type == "limit":
            if limit_price is None:
                raise BrokerAdapterError("alpaca: limit order needs a limit_price.")
            req = LimitOrderRequest(
                symbol=symbol, qty=qty, side=order_side,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol, qty=qty, side=order_side, time_in_force=TimeInForce.DAY,
            )

        o = client.submit_order(req)
        return OrderResult(
            accepted=True,
            broker_order_id=str(o.id),
            symbol=symbol,
            qty=qty,
            side=side,
            detail=f"submitted to Alpaca ({self.mode})",
            filled_qty=float(o.filled_qty or 0),
            filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
        )
