"""Generic indicator interpretation helpers.

Two utilities, both designed to work over any pandas-ta indicator column without
hard-coding individual indicator semantics:

  - `get_signal_logic_single_row(df, col)` returns +1/-1/0 for an event-style
    classification (cross-up / cross-down / nothing) on the indicator named by
    `col`, dispatching on a coarse family taxonomy (oscillator vs. histogram vs.
    moving-average vs. band, etc.).

  - `get_state_logic_single_row(df, col)` returns +1/-1/0 for an environmental
    state classification (bullish / bearish / neutral) — used as a regime/filter
    leg in confluence strategies.

These let a single piece of strategy code combine arbitrary indicators (e.g.
"RSI cross AND price above EMA AND BBP bullish") without needing a custom
interpreter per indicator name.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def get_signal_logic_single_row(df: pd.DataFrame, col: str) -> int:
    """+1 = bullish trigger this bar, -1 = bearish trigger, 0 = neither."""
    if col not in df.columns or len(df) < 2:
        return 0
    data, close = df[col].values, df["close"].values
    val, prev, c_val, c_prev = data[-1], data[-2], close[-1], close[-2]
    c = col.lower()
    if all(np.isnan(data)) or pd.isna(val):
        return 0
    d_max = np.nanmax(data)
    d_min = np.nanmin(data)
    d_mean = np.nanmean(data)

    if "cdl_" in c:
        if val > 0: return 1
        if val < 0: return -1
    elif any(x in c for x in ["rsi","mfi","rsx","uo","cfo","cg","willr","stoch","cci","cmo","fisher","bbp","cmf","pgo","stc","tmo","kurt","smio"]):
        buy_thr, sell_thr = (20, 80)
        if d_max <= 5: buy_thr, sell_thr = (0.2, 0.8)
        if d_min < -10: buy_thr, sell_thr = (-80, 80)
        if val < buy_thr and prev >= buy_thr: return 1
        if val > sell_thr and prev <= sell_thr: return -1
    elif any(x in c for x in ["macd","apo","ao","ppo","copc","tsi","mom","roc","kst","trix","efi","ebsw","pvo","tsv","bias","bop","br","ar","sqzpro","sqz_","reflex","chdlrext","ldecay","exhc","slope","pctret","logret"]):
        if val > 0 and prev <= 0: return 1
        if val < 0 and prev >= 0: return -1
    elif any(x in c for x in ["ma_","hma","alma","kama","jma","supertrend","dema","tema","zlma","vidya","midpoint","midprice","trima","sinwma","pwma","swma","ssf","fwma","hl2","hlc3","ohlc4","wcp","psar","ht_tl","vwap","zl_ema","pivots","linreg","ha_","mama"]):
        if c_val > val and c_prev <= prev: return 1
        if c_val < val and c_prev >= prev: return -1
    elif any(x in c for x in ["bbl","kcl","accbl","dcl","tos_stdevall_l"]):
        if c_val > val and c_prev <= prev: return 1
    elif any(x in c for x in ["bbu","kcu","accbu","dcu","tos_stdevall_u"]):
        if c_val < val and c_prev >= prev: return -1
    elif "dmp" in c or "vtxp" in c:
        if val > 0: return 1
    elif "dmn" in c or "vtxm" in c:
        if val > 0: return -1
    else:
        if d_mean > 1000:
            if c_val > val and c_prev <= prev: return 1
            if c_val < val and c_prev >= prev: return -1
        else:
            if val > 0 and prev <= 0: return 1
            if val < 0 and prev >= 0: return -1
    return 0


def get_state_logic_single_row(df: pd.DataFrame, col: str) -> int:
    """+1 = bullish regime, -1 = bearish, 0 = unknown.

    Use this to gate a trigger on an environmental condition (e.g. "only fire
    a long when the regime leg also reads bullish")."""
    if col not in df.columns or df.empty:
        return 0
    data, close = df[col].values, df["close"].values
    val, c_val = data[-1], close[-1]
    c = col.lower()
    if pd.isna(val):
        return 0
    d_max = np.nanmax(data)
    d_min = np.nanmin(data)

    if "cdl_" in c:
        return 1 if val != 0 else 0
    elif any(x in c for x in ["rsi","mfi","rsx","uo","cfo","cg","willr","stoch","cci","cmo","fisher","bbp","cmf","pgo","stc","tmo","kurt","smio"]):
        if d_max > 5:
            if d_min < -10: return 1 if val > 0 else -1
            return 1 if val > 50 else -1
        return 1 if val > 0.5 else -1
    elif any(x in c for x in ["macd","apo","ao","ppo","copc","tsi","mom","roc","kst","trix","efi","ebsw","pvo","tsv","bias","bop","br","ar","sqzpro","sqz_","reflex","chdlrext","ldecay","exhc","slope","pctret","logret"]):
        return 1 if val > 0 else -1
    elif any(x in c for x in ["ma_","hma","alma","kama","jma","supertrend","dema","tema","zlma","vidya","midpoint","midprice","trima","sinwma","pwma","swma","ssf","fwma","hl2","hlc3","ohlc4","wcp","psar","ht_tl","vwap","zl_ema","pivots","linreg","ha_","mama"]):
        return 1 if c_val > val else -1
    elif any(x in c for x in ["bbl","kcl","accbl","dcl","tos_stdevall_l"]):
        return 1 if c_val < val else -1
    elif any(x in c for x in ["bbu","kcu","accbu","dcu","tos_stdevall_u"]):
        return -1 if c_val > val else 1
    return 1 if val > np.nanmean(data) else -1
