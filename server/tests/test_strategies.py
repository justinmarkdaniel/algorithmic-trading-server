"""Strategy smoke tests on synthetic data.

We don't assert specific fires (depends on indicator output) — just verify
the example decision functions return well-formed responses for a non-trivial
input, and that the registry wires them through correctly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401

from server.strategies import (
    REGISTRY,
    decide_macd_crossover,
    decide_rsi_meanreversion,
    get_active,
    get_live_decider,
)


def _synthetic_5m_klines(n: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    base = 77000.0
    rets = rng.normal(0, 0.0008, n).cumsum()
    close = base * (1 + rets)
    high = close * (1 + np.abs(rng.normal(0, 0.0004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.0004, n)))
    open_ = np.r_[close[0], close[:-1]]
    volume = np.abs(rng.normal(50, 15, n))

    idx = pd.date_range("2026-04-25", periods=n, freq="5min")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _with_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.ta.study(ta.AllStudy)
    return out.replace([np.inf, -np.inf], np.nan)


def test_macd_strategy_returns_valid_response():
    df = _with_indicators(_synthetic_5m_klines(600))
    resp = decide_macd_crossover(df)
    assert isinstance(resp, dict)
    assert resp.get("action") in (None, "HOLD", "OPEN_LONG", "OPEN_SHORT")


def test_rsi_strategy_returns_valid_response():
    df = _with_indicators(_synthetic_5m_klines(600))
    resp = decide_rsi_meanreversion(df)
    assert isinstance(resp, dict)
    assert resp.get("action") in (None, "HOLD", "OPEN_LONG", "OPEN_SHORT")


def test_strategies_handle_empty_df():
    empty = pd.DataFrame()
    assert decide_macd_crossover(empty)["action"] == "HOLD"
    assert decide_rsi_meanreversion(empty)["action"] == "HOLD"


def test_registry_active_set_matches_active_files():
    actives = {s.name for s in get_active()}
    assert "macd_crossover" in actives
    assert "rsi_meanreversion" in actives
    for name in actives:
        assert REGISTRY[name].tier == "active"


def test_live_decider_dispatches_to_first_non_hold():
    df = _with_indicators(_synthetic_5m_klines(600))
    decider = get_live_decider()
    resp = decider(df)
    assert isinstance(resp, dict)
    assert resp.get("action") in (None, "HOLD", "OPEN_LONG", "OPEN_SHORT")
