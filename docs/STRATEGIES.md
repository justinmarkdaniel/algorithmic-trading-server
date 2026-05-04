# Adding a strategy

Every strategy in this codebase obeys one contract:

```python
def decide(df: pd.DataFrame, exchange_metrics: dict | None = None) -> dict:
    ...
    return {
        "action": "OPEN_LONG" | "OPEN_SHORT" | "CLOSE" | "HOLD",
        "sl_pct": 0.005,                      # stop-loss as fraction of entry
        "tp_pct": 0.005,                      # take-profit as fraction of entry
        "why":    "short tag for logs",
        "max_hold_bars":    24,               # optional override of global timeout
        "max_hold_bars_tf": "1h",             # optional — '5m' or '1h'
    }
```

`df` is the indicator-augmented kline DataFrame produced by
`server.engine.live_engine.build_indicators` — roughly 140 pandas-ta columns
plus OHLCV, all causally computed. The strategy reads `df.iloc[-1]` (the most
recent closed bar) and decides.

`exchange_metrics` is only passed when the strategy declares
`needs_exchange_metrics=True` in its `StrategyMeta`. It carries live orderbook
imbalance, top-trader long/short, taker ratio, OI deltas, and funding rate —
the things you can't get from klines alone.

The bot harness owns everything else: position state, order placement,
TP/SL bracket orders, slippage-budgeted entry chasing, account-level risk
caps, hold-timeout enforcement. The strategy is intentionally pure: it
decides; the bot executes.

## The five-minute walkthrough

Say you want to add a strategy that goes long when RSI(14) is below 25 *and*
the bar's close is above the 20-period EMA. Three steps.

### 1. Write the file

`server/strategies/active/rsi_pullback_long.py`:

```python
from __future__ import annotations
import pandas as pd


def decide_rsi_pullback_long(df: pd.DataFrame) -> dict:
    if len(df) < 2 or "RSI_14" not in df.columns or "EMA_20" not in df.columns:
        return {"action": "HOLD", "why": "indicators not ready"}

    last = df.iloc[-1]
    rsi, ema, close = last["RSI_14"], last["EMA_20"], last["close"]

    if pd.notna(rsi) and pd.notna(ema) and rsi < 25 and close > ema:
        return {
            "action": "OPEN_LONG",
            "sl_pct": 0.005,
            "tp_pct": 0.010,
            "why":    "rsi<25 + close>EMA20",
        }
    return {"action": "HOLD", "why": "no pullback"}
```

### 2. Register it

In `server/strategies/__init__.py`:

```python
from .active.rsi_pullback_long import decide_rsi_pullback_long

REGISTRY["rsi_pullback_long"] = StrategyMeta(
    name="rsi_pullback_long",
    tier="active",
    decide=decide_rsi_pullback_long,
    description="LONG when RSI(14)<25 AND close > EMA(20).",
    file="strategies/active/rsi_pullback_long.py",
    priority=3,
)
```

### 3. Test it

```bash
# Smoke test the registry surface
pytest server/tests/test_imports.py server/tests/test_strategies.py -v

# Backtest 7 days of live data through the same engine the bot uses
curl -s -X POST http://localhost:8765/backtest \
    -H 'content-type: application/json' \
    -d '{"strategy": "rsi_pullback_long", "hours": 168, "details": "summary"}' | jq
```

That's it. The bot picks it up at next start, the API lists it, and
`/per-strategy-stats` will track it once it starts firing.

## Tiers

Three folders, three behaviours:

- `active/` — the bot trades these. The default dispatcher walks them in
  priority order and takes the first non-HOLD response.
- `monitoring/` — evaluated and logged on every tick, never opens trades.
  Use this to shadow-run a candidate strategy on real ticks before promoting
  it. `/per-strategy-stats` replays them like the active strategies, so
  promotion is a data-driven decision.
- `archived/` — skipped entirely at runtime. Kept on disk for reference.

Promote/demote by `git mv`-ing the file between folders and flipping
`tier=` on its `StrategyMeta`. The registry is the single source of truth —
no other place in the codebase names strategies.

## Per-strategy hold timeouts

The global hold timeout (`BOT_MAX_HOLD_BARS`, default 48 5m bars = 4 h) is
applied when a strategy doesn't override it. Override per-trade by returning
`max_hold_bars` and `max_hold_bars_tf` in the decide response:

```python
return {
    "action": "OPEN_LONG", "sl_pct": 0.005, "tp_pct": 0.005,
    "why": "...",
    "max_hold_bars":    24,
    "max_hold_bars_tf": "1h",   # 24 hours
}
```

This is per-trade, not per-strategy — different code paths inside one strategy
file can request different timeouts.

## Strategies that need orderbook / flow data

Set `needs_exchange_metrics=True` on the `StrategyMeta`, accept the dict as a
second positional arg:

```python
def decide_with_orderbook(df: pd.DataFrame, em: dict) -> dict:
    if em.get("imb1pct") is not None and em["imb1pct"] > 0.20:
        return {"action": "OPEN_LONG", "sl_pct": 0.005, "tp_pct": 0.005,
                "why": "imb1pct>+0.20"}
    return {"action": "HOLD"}
```

The `em` dict carries: `imb1pct`, `imb5pct`, `smart_skew`, `taker_ratio`,
`oi_5m`, `oi_1h`, `top_pos`, `fund_rate`, `ret_6`, `ret_12`. See
`server/bot/main.py:_tick` for where it's assembled and
`server/api/backtest.py:_fetch_historical_exchange_metrics` for the historical
backfill (orderbook depth has no historical endpoint on most venues and is
`None` in backtests).

## Common pitfalls

- **Indicator name typos.** `df["RSI_14"]` is correct; `df["RSI14"]` returns
  `KeyError` and your strategy silently HOLDs. Run a backtest on a small
  window first; `/backtest` reports the indicator coverage available.
- **Non-causal calculations.** Anything that uses `df.iloc[-1]` is fine.
  Anything that uses `.shift(-N)`, `.rolling(...).apply(...)`-with-future-bars,
  or pandas-ta functions with `centered=True` defaults will leak future data
  into the live signal. The engine already overrides DPO; sanity-check the
  tail with `df["YOUR_COL"].tail(20).isna().sum() == 0` before trusting any
  new indicator at live evaluation time.
- **Returning `None` for `action`.** The bot treats `None` and `"HOLD"` as
  identical, but always return a dict — empty/`None` returns will look
  alarming in the decision log.
