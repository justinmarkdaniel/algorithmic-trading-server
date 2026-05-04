"""Generic exchange-client placeholder.

The bot doesn't know — and shouldn't know — which venue it's trading on. It
calls a small set of methods on whatever object `get_client(account_id)`
returns. Wire your venue here and leave the rest of the codebase alone.

The `Exchange` protocol below is the surface the bot tick loop uses: market
introspection (precision, min size), balance + position fetch, leverage
control, order placement (limit / market / stop), open-order listing, and
cancellation. ccxt, a vendor SDK, or a hand-rolled HTTP client all satisfy
this — pick whichever fits your venue.

For local development, `get_client()` returns a stub that raises on any real
call. Replace `_StubExchange` (or the whole `get_client`) with your venue
binding before going live.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class Exchange(Protocol):
    """The methods the bot tick loop calls on an exchange client.

    Method names + return shapes mirror ccxt to keep wiring trivial. If you
    bind a non-ccxt SDK, write a thin adapter that satisfies this protocol
    rather than rewriting the bot.
    """

    def load_markets(self) -> dict: ...
    def market(self, symbol: str) -> dict: ...
    def fetch_balance(self) -> dict: ...
    def fetch_positions(self, symbols: list[str]) -> list[dict]: ...
    def fetch_open_orders(self, symbol: str) -> list[dict]: ...
    def fetch_order(self, order_id: str, symbol: str) -> dict: ...
    def fetch_ticker(self, symbol: str) -> dict: ...
    def set_leverage(self, leverage: int, symbol: str) -> Any: ...
    def create_order(self, symbol: str, type: str, side: str,
                     amount: float, price: float | None = None,
                     params: dict | None = None) -> dict: ...
    def cancel_order(self, order_id: str, symbol: str) -> Any: ...
    def cancel_all_orders(self, symbol: str) -> Any: ...
    def price_to_precision(self, symbol: str, price: float) -> str: ...
    def amount_to_precision(self, symbol: str, qty: float) -> str: ...


class _StubExchange:
    """Default placeholder. Raises on any live call — wire a real client.

    `id` and a markets dict with a single placeholder symbol are populated so
    that the bot's startup path (`load_markets()`, `market(symbol)`) doesn't
    crash before you've replaced the stub. Trade methods raise immediately.
    """

    id = "stub"
    has = {"fetchCurrencies": False}

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        symbol = os.environ.get("DEFAULT_SYMBOL", "BTC/USDT:USDT")
        self._markets: dict[str, dict] = {
            symbol: {
                "id": symbol.replace("/", "").split(":")[0],
                "symbol": symbol,
                "precision": {"price": 0.1, "amount": 0.001},
                "limits": {"amount": {"min": 0.001}},
            }
        }
        self.urls: dict = {"api": {}}

    def load_markets(self) -> dict:
        return self._markets

    def market(self, symbol: str) -> dict:
        return self._markets[symbol]

    def _raise(self, op: str) -> None:
        raise NotImplementedError(
            f"_StubExchange.{op}() — wire a real exchange client in "
            f"server/exchange/client.py before going live."
        )

    def fetch_balance(self) -> dict: self._raise("fetch_balance"); return {}
    def fetch_positions(self, symbols: list[str]) -> list[dict]: self._raise("fetch_positions"); return []
    def fetch_open_orders(self, symbol: str) -> list[dict]: self._raise("fetch_open_orders"); return []
    def fetch_order(self, order_id: str, symbol: str) -> dict: self._raise("fetch_order"); return {}
    def fetch_ticker(self, symbol: str) -> dict: self._raise("fetch_ticker"); return {}
    def set_leverage(self, leverage: int, symbol: str) -> Any: self._raise("set_leverage")
    def create_order(self, symbol: str, type: str, side: str,
                     amount: float, price: float | None = None,
                     params: dict | None = None) -> dict:
        self._raise("create_order"); return {}
    def cancel_order(self, order_id: str, symbol: str) -> Any: self._raise("cancel_order")
    def cancel_all_orders(self, symbol: str) -> Any: self._raise("cancel_all_orders")
    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{round(price, 2)}"
    def amount_to_precision(self, symbol: str, qty: float) -> str:
        return f"{round(qty, 6)}"


def get_client(account_id: str = "account1") -> Exchange:
    """Build (or fetch) the exchange client for the given account.

    Default implementation returns a stub. Replace with your venue binding —
    typically:

        import ccxt
        api_key = os.environ[f"{account_id.upper()}_API_KEY"]
        api_secret = os.environ[f"{account_id.upper()}_API_SECRET"]
        client = ccxt.<your_venue>({"apiKey": api_key, "secret": api_secret,
                                    "enableRateLimit": True,
                                    "options": {"defaultType": "future"}})
        return client

    Per-account credential resolution lives entirely inside this function —
    the bot tick loop knows nothing about how keys are sourced.
    """
    return _StubExchange(account_id)


if __name__ == "__main__":
    c = get_client()
    print("Exchange:", c.id)
    print("Markets:", list(c.load_markets()))
