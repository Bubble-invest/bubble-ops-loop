"""
_template — copy this to add a real broker.

Steps:
  1. cp _template.py <yourbroker>.py
  2. Implement the 5 methods against your broker's SDK.
  3. Read EVERY credential from os.environ — never a file in the repo, never a
     literal. secrets_armed() decides arm-state from the env (and optionally a
     cheap read call).
  4. Register it in broker_adapter.REGISTRY:  "<yourbroker>": YourAdapter
  5. Keep the allow_live guard: call self._require_live(allow_live) at the top
     of submit_order, exactly as below.
"""

from __future__ import annotations

import os
import sys

# Make the parent package importable when loaded by get_adapter().
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

# Name your env vars. NEVER commit values — these are just the KEYS to read.
_ENV_API_KEY = "YOURBROKER_API_KEY"
_ENV_API_SECRET = "YOURBROKER_API_SECRET"


class TemplateAdapter(BrokerAdapter):
    name = "yourbroker"

    def _creds(self) -> tuple[str | None, str | None]:
        return os.environ.get(_ENV_API_KEY), os.environ.get(_ENV_API_SECRET)

    def secrets_armed(self) -> bool:
        key, secret = self._creds()
        # Minimal arm-state: both env vars present. Optionally also try a cheap
        # read call here and return False on failure — but keep it fast + never
        # raise (a tick must not crash on arm-state).
        return bool(key and secret)

    def _client(self):
        if not self.secrets_armed():
            raise BrokerAdapterError(f"{self.name}: credentials absent from the env.")
        # Lazy-import your SDK here so the example installs without it:
        #   from yourbroker_sdk import Client
        #   key, secret = self._creds()
        #   return Client(key, secret)
        raise BrokerAdapterError(f"{self.name}: implement _client() with your SDK.")

    def get_account(self) -> AccountSummary:
        raise BrokerAdapterError(f"{self.name}: implement get_account().")

    def get_positions(self) -> list[Position]:
        raise BrokerAdapterError(f"{self.name}: implement get_positions().")

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
        self._require_live(allow_live)  # <-- KEEP THIS: the poka-yoke + arm-state guard
        raise BrokerAdapterError(f"{self.name}: implement submit_order() with your SDK.")
