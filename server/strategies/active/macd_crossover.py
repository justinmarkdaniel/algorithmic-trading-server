"""MACD crossover — minimal example strategy.

A `decide()` function takes the indicator-augmented dataframe (and optionally a
dict of live exchange metrics) and returns a response dict telling the bot what
to do. Every strategy in this codebase follows the same shape:

    response = {
        "action":   "OPEN_LONG" | "OPEN_SHORT" | "CLOSE" | "HOLD",
        "sl_pct":   0.005,                        # stop-loss as fraction of entry
        "tp_pct":   0.005,                        # take-profit as fraction of entry
        "why":      "MACD bull cross",            # short tag, surfaced in logs
        "max_hold_bars":    24,                   # optional — overrides global timeout
        "max_hold_bars_tf": "1h",                 # optional — '5m' | '1h'
    }

The bot harness owns position-state, order placement, TP/SL bracket orders,
slippage-budgeted entry chasing, account-level risk caps, etc. The strategy
is intentionally pure: it decides; the bot executes.

This particular example fires LONG when MACD histogram crosses up through zero
and SHORT when it crosses down. It is deliberately naive — its purpose is to
demonstrate the contract, not to make money. Build your own combinations of
indicators and edge cases on top of `df.ta.study(ta.AllStudy)` (already
applied in the engine) for ~140 ready-to-use TA columns.
"""
from __future__ import annotations

import pandas as pd


def decide_macd_crossover(df: pd.DataFrame) -> dict:
    """LONG on MACD-histogram bull cross, SHORT on bear cross."""
    if len(df) < 2 or "MACDh_12_26_9" not in df.columns:
        return {"action": "HOLD", "why": "macd not ready"}

    macdh = df["MACDh_12_26_9"]
    last, prev = macdh.iloc[-1], macdh.iloc[-2]

    if pd.isna(last) or pd.isna(prev):
        return {"action": "HOLD", "why": "macd nan"}

    if prev <= 0 < last:
        return {
            "action": "OPEN_LONG",
            "sl_pct": 0.005,
            "tp_pct": 0.005,
            "why": "MACD bull cross",
        }

    if prev >= 0 > last:
        return {
            "action": "OPEN_SHORT",
            "sl_pct": 0.005,
            "tp_pct": 0.005,
            "why": "MACD bear cross",
        }

    return {"action": "HOLD", "why": "no MACD cross"}
