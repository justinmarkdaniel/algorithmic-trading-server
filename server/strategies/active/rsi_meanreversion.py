"""RSI mean-reversion — minimal example strategy.

LONG when RSI(14) crosses up out of oversold territory (<30 → >=30), SHORT when
it crosses down out of overbought (>70 → <=70). Same response contract as
`macd_crossover.py` — see that file for the full schema.

Why an example: we want one momentum-style strategy and one mean-reversion
style strategy in this repo so the registry has at least two `tier='active'`
entries to demonstrate priority dispatch and `is_strategy_allowed` per-account
filtering. Real strategies live in your private fork.
"""
from __future__ import annotations

import pandas as pd


def decide_rsi_meanreversion(df: pd.DataFrame) -> dict:
    """LONG on RSI-bull exit from oversold, SHORT on RSI-bear exit from overbought."""
    if len(df) < 2 or "RSI_14" not in df.columns:
        return {"action": "HOLD", "why": "rsi not ready"}

    rsi = df["RSI_14"]
    last, prev = rsi.iloc[-1], rsi.iloc[-2]

    if pd.isna(last) or pd.isna(prev):
        return {"action": "HOLD", "why": "rsi nan"}

    if prev < 30 <= last:
        return {
            "action": "OPEN_LONG",
            "sl_pct": 0.005,
            "tp_pct": 0.010,
            "why": "RSI exit oversold",
        }

    if prev > 70 >= last:
        return {
            "action": "OPEN_SHORT",
            "sl_pct": 0.005,
            "tp_pct": 0.010,
            "why": "RSI exit overbought",
        }

    return {"action": "HOLD", "why": "no RSI extreme"}
