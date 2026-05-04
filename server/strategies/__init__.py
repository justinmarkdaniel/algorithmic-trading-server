"""Strategy registry — single source of truth.

Layout
------
    server/strategies/
      active/      ← strategies the bot will trade live (any tier='active' entry).
                     The dispatcher walks them in priority order and takes the
                     first non-HOLD response.
      monitoring/  ← evaluated and logged each tick, but never opens trades.
                     Useful for shadow-running a candidate strategy on real
                     ticks before promoting it.
      archived/    ← removed from runtime entirely (kept on disk for reference).

Adding or removing a strategy = move its file between folders and flip the
`tier=` field on its `StrategyMeta`. Every consumer (the bot tick loop, the
FastAPI `/strategies` endpoint, the `/backtest` route) reads from this
registry — no other place names strategies. That single source of truth keeps
backtest, live trade, and observation surfaces from drifting apart.

Adding a new strategy
---------------------
1. Drop a file in `active/` (or `monitoring/`) that exports a `decide(df, ...)`
   function returning the response dict described in `active/macd_crossover.py`.
2. Import it below and register a `StrategyMeta`.
3. Done — the bot picks it up on next start, the API will list it, and
   `POST /backtest` can replay it against any historical window.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# --- ACTIVE ------------------------------------------------------------------
from .active.macd_crossover import decide_macd_crossover
from .active.rsi_meanreversion import decide_rsi_meanreversion


@dataclass
class StrategyMeta:
    name: str          # short identifier; also the key in REGISTRY
    tier: str          # 'active' | 'monitoring' | 'archived'
    decide: Callable   # signature: (df, exchange_metrics?) -> response dict
    description: str
    file: str          # relative path from server/ — surfaced in /strategies
    needs_exchange_metrics: bool = False  # True if decide() needs orderbook/flow
    priority: int = 100                   # lower runs first when multiple actives


REGISTRY: dict[str, StrategyMeta] = {
    "macd_crossover": StrategyMeta(
        name="macd_crossover",
        tier="active",
        decide=decide_macd_crossover,
        description="MACD-histogram zero-cross — example momentum strategy.",
        file="strategies/active/macd_crossover.py",
        priority=1,
    ),
    "rsi_meanreversion": StrategyMeta(
        name="rsi_meanreversion",
        tier="active",
        decide=decide_rsi_meanreversion,
        description="RSI(14) exit from oversold/overbought — example mean-reversion strategy.",
        file="strategies/active/rsi_meanreversion.py",
        priority=2,
    ),
}


def get_active() -> list[StrategyMeta]:
    """All active-tier strategies, sorted by priority (1 first)."""
    return sorted(
        [s for s in REGISTRY.values() if s.tier == "active"],
        key=lambda s: s.priority,
    )


def get_monitoring() -> list[StrategyMeta]:
    return [s for s in REGISTRY.values() if s.tier == "monitoring"]


def get_archived() -> list[StrategyMeta]:
    return [s for s in REGISTRY.values() if s.tier == "archived"]


def get_live_decider() -> Callable:
    """Default dispatcher: walk active strategies by priority, return the first
    non-HOLD response. Override by registering a custom dispatcher in
    `active/` and pointing this function at it."""
    actives = get_active()

    def _dispatch(df, exchange_metrics: dict | None = None) -> dict:
        for s in actives:
            try:
                resp = (s.decide(df, exchange_metrics)
                        if s.needs_exchange_metrics else s.decide(df))
            except Exception:
                continue
            if resp and resp.get("action") not in (None, "HOLD"):
                return resp
        return {"action": "HOLD", "why": "no active strategy fired"}

    return _dispatch


__all__ = [
    "REGISTRY",
    "StrategyMeta",
    "get_active",
    "get_monitoring",
    "get_archived",
    "get_live_decider",
    "decide_macd_crossover",
    "decide_rsi_meanreversion",
]
