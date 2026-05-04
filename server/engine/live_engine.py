"""Live data engine — turns the venue's REST endpoints into a model-ready
feature dataframe.

Responsibilities:
  - maintain a rolling 5m kline DataFrame (warmup + fresh)
  - compute pandas-ta AllStudy indicators causally (no future leakage)
  - optional: fetch venue-side metrics (open interest, taker ratio, funding…)
    — wire these to your venue inside `fetch_metrics_latest()` /
    `fetch_funding_latest()` if your strategies need them
  - assemble a `compact_packet` — a single dict of everything a strategy
    might want, ready to hand to `decide()`

No execution logic here. This module is a pure feature engine; the bot tick
loop owns position state and order placement.

Wiring this to a venue:
  - replace `KLINES_URL` (and any of the metrics URLs you care about) with
    your venue's REST endpoints
  - confirm the kline column ordering matches `KLINE_COLS` below — if your
    venue returns a different shape, adapt the column list and parsing in
    `fetch_klines()`
  - if your venue exposes an orderbook /depth endpoint, keep
    `fetch_depth_snapshot()` + `depth_to_bands()` and just retarget the URL
"""
from __future__ import annotations

import logging
import os
import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401 — registers df.ta accessor
import requests

# Replace this base URL with your venue's REST root. Endpoint paths below
# are placeholders modelled on a typical perpetual-futures REST API — rename
# freely to match your venue.
EXCHANGE_BASE = os.environ.get("EXCHANGE_BASE", "https://api.example.com")
KLINES_URL    = f"{EXCHANGE_BASE}/v1/klines"
DEPTH_URL     = f"{EXCHANGE_BASE}/v1/depth"
PREMIUM_URL   = f"{EXCHANGE_BASE}/v1/premiumIndex"
FUNDING_URL   = f"{EXCHANGE_BASE}/v1/fundingRate"
OI_URL        = f"{EXCHANGE_BASE}/futures/data/openInterestHist"
TOP_POS_URL   = f"{EXCHANGE_BASE}/futures/data/topLongShortPositionRatio"
TOP_ACC_URL   = f"{EXCHANGE_BASE}/futures/data/topLongShortAccountRatio"
GLOBAL_ACC_URL = f"{EXCHANGE_BASE}/futures/data/globalLongShortAccountRatio"
TAKER_URL     = f"{EXCHANGE_BASE}/futures/data/takerlongshortRatio"

SYMBOL = os.environ.get("DEFAULT_SYMBOL_NATIVE", "BTCUSDT")
TF = "5m"
TF_MS = 5 * 60_000

logger = logging.getLogger("TradingBot.engine")

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trade_count",
    "taker_buy_vol", "taker_buy_quote_vol", "ignore",
]


# -------------------------- HTTP helpers --------------------------

def _get(url: str, params: dict, retries: int = 4) -> Any:
    backoff = 1.0
    for _ in range(retries):
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (418, 429):
            wait = float(r.headers.get("Retry-After", backoff))
            time.sleep(max(wait, backoff))
            backoff = min(backoff * 2, 30.0)
            continue
        r.raise_for_status()
    raise RuntimeError(f"fetch failed after {retries} attempts: {url} {params}")


# -------------------------- fetchers --------------------------

def fetch_klines(symbol: str, interval: str, limit: int = 1500,
                 end_ms: int | None = None) -> pd.DataFrame:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_ms is not None:
        params["endTime"] = end_ms
    rows = _get(KLINES_URL, params)
    df = pd.DataFrame(rows, columns=KLINE_COLS)
    for c in ["open", "high", "low", "close", "volume", "quote_volume",
              "taker_buy_vol", "taker_buy_quote_vol"]:
        df[c] = pd.to_numeric(df[c])
    df["trade_count"] = df["trade_count"].astype(int)
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True).dt.tz_localize(None)
    df = df.drop(columns=["close_time", "ignore"]).set_index("open_time").sort_index()
    return df


def fetch_depth_snapshot(symbol: str = SYMBOL, limit: int = 1000) -> dict:
    """Full orderbook snapshot. We compute cumulative depth at ±1..5% of mid."""
    return _get(DEPTH_URL, {"symbol": symbol, "limit": limit})


def depth_to_bands(snap: dict) -> dict:
    """Derive cumulative bid/ask depth at mid ± 1..5% and imbalance fields."""
    bids = np.asarray(snap["bids"], dtype=float)
    asks = np.asarray(snap["asks"], dtype=float)
    if len(bids) == 0 or len(asks) == 0:
        return {}
    mid = (bids[0, 0] + asks[0, 0]) / 2.0
    out: dict[str, float] = {}
    for pct in (1, 2, 3, 4, 5):
        lo = mid * (1 - pct / 100)
        hi = mid * (1 + pct / 100)
        bid_q = float(bids[bids[:, 0] >= lo, 1].sum())
        ask_q = float(asks[asks[:, 0] <= hi, 1].sum())
        out[f"ob_bid_depth_{pct}pct"] = bid_q
        out[f"ob_ask_depth_{pct}pct"] = ask_q
        denom = bid_q + ask_q
        out[f"ob_imbalance_{pct}pct"] = (bid_q - ask_q) / denom if denom else 0.0
    if out.get("ob_bid_depth_1pct", 0) and out.get("ob_bid_depth_5pct") is not None:
        out["ob_bid_slope_5_1"] = out["ob_bid_depth_5pct"] / out["ob_bid_depth_1pct"]
    if out.get("ob_ask_depth_1pct", 0) and out.get("ob_ask_depth_5pct") is not None:
        out["ob_ask_slope_5_1"] = out["ob_ask_depth_5pct"] / out["ob_ask_depth_1pct"]
    return out


def fetch_metrics_latest() -> dict:
    """Optional venue-side flow metrics — fill in only if your venue exposes
    them and your strategies use them. The reference shape covers OI deltas,
    top-trader long/short, global-account long/short, taker ratio, and a
    derived smart/retail skew. Missing endpoints simply return an empty dict.
    """
    out: dict[str, float] = {}
    try:
        oi_now = _get(OI_URL, {"symbol": SYMBOL, "period": "5m", "limit": 13})
        if oi_now:
            latest = oi_now[-1]
            out["fx_oi_value"] = float(latest["sumOpenInterestValue"])
            if len(oi_now) >= 2:
                prev = oi_now[-2]
                out["fx_oi_change_5m"] = (float(latest["sumOpenInterest"]) /
                                          float(prev["sumOpenInterest"]) - 1.0)
            if len(oi_now) >= 13:
                h = oi_now[-13]
                out["fx_oi_change_1h"] = (float(latest["sumOpenInterest"]) /
                                          float(h["sumOpenInterest"]) - 1.0)
    except Exception as e:
        logger.warning("OI fetch failed: %s", e)

    try:
        tp = _get(TOP_POS_URL, {"symbol": SYMBOL, "period": "5m", "limit": 1})
        if tp:
            r = float(tp[-1]["longShortRatio"])
            out["fx_top_pos_ratio"] = r / (1 + r)
    except Exception as e:
        logger.warning("top_pos fetch failed: %s", e)

    try:
        ga = _get(GLOBAL_ACC_URL, {"symbol": SYMBOL, "period": "5m", "limit": 1})
        if ga:
            r = float(ga[-1]["longShortRatio"])
            out["fx_global_acc_ratio"] = r / (1 + r)
    except Exception as e:
        logger.warning("global_acc fetch failed: %s", e)

    try:
        tk = _get(TAKER_URL, {"symbol": SYMBOL, "period": "5m", "limit": 1})
        if tk:
            out["fx_taker_buy_sell_ratio"] = float(tk[-1]["buySellRatio"])
    except Exception as e:
        logger.warning("taker fetch failed: %s", e)

    if "fx_top_pos_ratio" in out and "fx_global_acc_ratio" in out:
        out["fx_smart_retail_skew"] = out["fx_top_pos_ratio"] - out["fx_global_acc_ratio"]
    return out


def fetch_funding_latest() -> dict:
    """Latest funding rate + 3-day trailing average. Returns {} if your venue
    doesn't have a funding-rate endpoint or it errors."""
    try:
        batch = _get(FUNDING_URL, {"symbol": SYMBOL, "limit": 9})
        rates = [float(x["fundingRate"]) for x in batch]
        return {
            "fund_rate": rates[-1] if rates else None,
            "fund_rate_3d_avg": float(np.mean(rates)) if rates else None,
        }
    except Exception as e:
        logger.warning("funding fetch failed: %s", e)
        return {}


# -------------------------- feature build --------------------------

def build_indicators(df_5m: pd.DataFrame) -> pd.DataFrame:
    """Apply pandas-ta AllStudy on 5m klines. Causal — uses only bars up to
    the current.

    Important: pandas-ta's `dpo` defaults to `centered=True`, which uses
    `close.shift(-(length//2 + 1))` — a forward-looking shift that leaks
    future bars into the value at bar `t` AND leaves the most recent
    `length//2 + 1` bars as NaN. In a backtest computed once over a long
    history, that NaN tail sits past the end of the data and the lookahead is
    invisible. At LIVE evaluation time the strategy's `iloc[-1]` IS the NaN
    tail, so DPO silently returns 0 for every signal check — any strategy
    referencing DPO is effectively disabled in live trading.

    We overwrite DPO with `centered=False` (no lookahead, no NaN tail) so:
      1. live evaluation sees a real DPO value at the tail
      2. backtests no longer benefit from the implicit lookahead bias
      3. the indicator becomes truly causal as the engine docstring claims

    Same suspicion applies to any other pandas-ta function with a `centered`,
    `lookahead`, or negative-`shift` parameter — sanity-check the tail isn't
    NaN before trusting a new indicator at live evaluation time.
    """
    df = df_5m.copy()
    df.ta.study(ta.AllStudy)
    df["DPO_20"] = df.ta.dpo(length=20, centered=False)
    df = df.replace([np.inf, -np.inf], np.nan)
    return df


def compact_packet(bar_idx: int, ts: pd.Timestamp, df: pd.DataFrame,
                   recent_closes: list[float], ob: dict, fx: dict, fu: dict,
                   state_dict: dict) -> dict:
    """Produce a compact dict shaped for strategy `decide()` consumption."""
    last = df.iloc[-1]
    def _g(k: str):
        v = last.get(k, None)
        if v is None or pd.isna(v):
            return None
        return float(v)

    vol_total = _g("volume")
    taker_buy = _g("taker_buy_vol")
    returns = {}
    closes = df["close"].astype(float).values
    for n, label in [(1, "ret_1"), (3, "ret_3"), (6, "ret_6"),
                     (12, "ret_12"), (24, "ret_24"), (72, "ret_72")]:
        if len(closes) > n and closes[-n - 1] > 0:
            returns[label] = float(closes[-1] / closes[-n - 1] - 1)
        else:
            returns[label] = None

    return {
        "bar": bar_idx,
        "ts": ts.isoformat(),
        "price": {
            "o": _g("open"), "h": _g("high"), "l": _g("low"),
            "c": _g("close"), "v": vol_total,
        },
        "recent_closes": recent_closes,
        "returns": returns,
        "trend": {
            "supert_dir": _g("SUPERTd_7_3.0"),
            "adx": _g("ADX_14"),
            "ema10": _g("EMA_10"), "sma10": _g("SMA_10"),
            "hma10": _g("HMA_10"),
        },
        "momentum": {
            "rsi14": _g("RSI_14"),
            "macdh": _g("MACDh_12_26_9"),
            "stoch_k": _g("STOCHk_14_3_3"),
            "willr": _g("WILLR_14"),
        },
        "volatility": {
            "atr14": _g("ATRr_14"),
            "bbp": _g("BBP_5_2.0_2.0"),
            "natr": _g("NATR_14"),
        },
        "volume": {
            "obv": _g("OBV"),
            "cmf": _g("CMF_20"),
            "taker_buy_pct": round(taker_buy / vol_total, 4)
                if (vol_total and taker_buy is not None) else None,
        },
        "orderbook": {
            "imb1pct": ob.get("ob_imbalance_1pct"),
            "imb5pct": ob.get("ob_imbalance_5pct"),
            "bid_slope": ob.get("ob_bid_slope_5_1"),
            "ask_slope": ob.get("ob_ask_slope_5_1"),
        },
        "flow": {
            "taker_ratio": fx.get("fx_taker_buy_sell_ratio"),
            "oi_5m": fx.get("fx_oi_change_5m"),
            "oi_1h": fx.get("fx_oi_change_1h"),
            "top_pos": fx.get("fx_top_pos_ratio"),
            "smart_skew": fx.get("fx_smart_retail_skew"),
        },
        "funding": {"rate": fu.get("fund_rate"), "avg3d": fu.get("fund_rate_3d_avg")},
        "state": state_dict,
    }


# -------------------------- engine --------------------------

def get_engine_warmup_bars() -> int:
    """Read WARMUP_BARS from env, fall back to config file, then 1500."""
    if "WARMUP_BARS" in os.environ:
        try:
            return int(os.environ["WARMUP_BARS"])
        except ValueError:
            logger.warning("Invalid WARMUP_BARS in env, falling back to config.")

    config_path = Path(__file__).resolve().parents[2] / "config" / "production.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config_data = json.load(f)
                if "warmup_bars" in config_data:
                    return int(config_data["warmup_bars"])
        except Exception as e:
            logger.warning(f"Failed to read warmup_bars from config: {e}")

    return 1500


@dataclass
class EngineConfig:
    warmup_bars: int = get_engine_warmup_bars()
    symbol: str = SYMBOL
    # Strategies often guard against pandas-ta initialisation artefacts at
    # session bar 0 (`if bar < 50: HOLD`). In live, the engine ALREADY primes
    # `warmup_bars` of real history, so indicators are fully seasoned on the
    # very first live tick — start `bar_idx` past that guard so we're
    # trade-eligible immediately.
    initial_bar_idx: int = 100


class LiveEngine:
    """Maintains 5m klines + fetches orderbook/flow/funding on demand."""

    def __init__(self, cfg: EngineConfig = EngineConfig()) -> None:
        self.cfg = cfg
        self.klines_5m: pd.DataFrame = pd.DataFrame()
        self.recent_closes: deque[float] = deque(maxlen=6)
        self.bar_idx = cfg.initial_bar_idx

    def prime(self) -> None:
        """Initial warmup fetch."""
        logger.info("priming 5m klines (%d bars)…", self.cfg.warmup_bars)
        df = fetch_klines(self.cfg.symbol, "5m", limit=self.cfg.warmup_bars)

        # Drop the still-forming current bar (matches catch_up behaviour).
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
        minute_floor = now_utc.replace(minute=now_utc.minute - (now_utc.minute % 5))
        last_closed_open = minute_floor - pd.Timedelta(minutes=5)
        df = df[df.index <= last_closed_open]

        self.klines_5m = df
        for c in df["close"].tail(6).astype(float).tolist():
            self.recent_closes.append(c)
        logger.info("primed: %d bars, tail=%s", len(df), df.index[-1])

    def catch_up(self) -> None:
        """Fetch any bars newer than the cache's tail (excluding in-progress bar)."""
        if self.klines_5m.empty:
            self.prime()
            return
        tail = self.klines_5m.index[-1]
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
        minute_floor = now_utc.replace(minute=now_utc.minute - (now_utc.minute % 5))
        last_closed_open = minute_floor - pd.Timedelta(minutes=5)
        if tail >= last_closed_open:
            return
        df = fetch_klines(self.cfg.symbol, "5m", limit=100)
        df = df[df.index <= last_closed_open]
        new = df[df.index > tail]
        if len(new):
            self.klines_5m = pd.concat([self.klines_5m, new]).sort_index()
            for c in new["close"].astype(float).tolist():
                self.recent_closes.append(c)
            logger.info("caught up: appended %d new 5m bars, tail=%s",
                        len(new), self.klines_5m.index[-1])

    def build_packet(self, state_dict: dict) -> tuple[dict, pd.DataFrame]:
        """Compute indicators on the full cache, fetch OB/flow/funding,
        assemble the packet."""
        df_ind = build_indicators(self.klines_5m)
        ts = df_ind.index[-1]
        try:
            ob = depth_to_bands(fetch_depth_snapshot(self.cfg.symbol))
        except Exception as e:
            logger.warning("depth snapshot failed: %s", e)
            ob = {}
        fx = fetch_metrics_latest()
        fu = fetch_funding_latest()
        packet = compact_packet(
            bar_idx=self.bar_idx,
            ts=ts,
            df=df_ind,
            recent_closes=list(self.recent_closes),
            ob=ob, fx=fx, fu=fu,
            state_dict=state_dict,
        )
        self.bar_idx += 1
        return packet, df_ind
