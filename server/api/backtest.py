"""POST /backtest — replay any registered strategy against a historical window.

Routing principle: this endpoint MUST NOT redefine strategy logic, signal
evaluation, or data fetching. It pulls from the same registry the live bot
uses (`server.strategies`) and from the same engine (`server.engine`). Change
either and the backtest changes the next request — no double-bookkeeping.

Position semantics
------------------
Once a fire opens a trade, no further fires are taken until that trade
resolves (TP / SL / horizon timeout). This mirrors the live bot, which
will not open a second position while `STATE.position` is non-flat.
Without this rule the backtest over-counts fires by booking the same
signal-cluster on every adjacent bar where the conditions still hold.

Body schema
-----------
    {
      "strategy":     "macd_crossover",       # name from REGISTRY (any tier)
      "from_ts":      "2026-04-24T10:55:00",  # optional, UTC, inclusive
      "to_ts":        "2026-04-26T10:50:00",  # optional, UTC, inclusive
      "hours":        48,                      # optional, used if from/to omitted
      "warmup_bars":  500,                     # optional, defaults to engine warmup
      "horizon_bars": 288,                     # optional, max hold per trade
      "details":      "summary"                # "summary" | "fires" | "full"
    }
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.engine import live_engine as live_engine_mod
from server.engine.live_engine import (
    SYMBOL, build_indicators, fetch_klines,
    OI_URL, TOP_POS_URL, GLOBAL_ACC_URL, TAKER_URL, FUNDING_URL, _get,
)
from server.strategies import REGISTRY, get_active

logger = logging.getLogger("TradingBot.backtest")


def _fetch_historical_exchange_metrics(start_ts: pd.Timestamp,
                                       end_ts: pd.Timestamp) -> pd.DataFrame:
    """Pull historical OI / top-pos / global-account / taker-ratio / funding
    from the SAME endpoints the live engine uses every tick. No bundled
    parquets, no cached state — each backtest call queries fresh from the
    venue.

    Endpoint history caps (vary per venue — illustrative reference shape):
      - openInterestHist / top-pos / global-acc / taker:  ~30 days each
      - fundingRate: years
      - orderbook /depth: REAL-TIME ONLY (no historical version);
        imb1pct / imb5pct will be NaN for all backtest bars.
    """
    start_ts = pd.Timestamp(start_ts)
    end_ts   = pd.Timestamp(end_ts)
    full_idx = pd.date_range(start_ts, end_ts, freq="5min").astype("datetime64[ms]")
    out = pd.DataFrame(index=full_idx,
                       columns=["imb1pct","imb5pct","smart_skew","taker_ratio",
                                "oi_1h","fund_rate","top_pos"], dtype=float)

    start_ms = int(start_ts.tz_localize("UTC").timestamp() * 1000)
    end_ms   = int(end_ts.tz_localize("UTC").timestamp() * 1000)

    def _paginate(url, key_to_col_map: dict, transform=None):
        cursor = end_ms
        seen = 0
        while True:
            params = {"symbol": SYMBOL, "period": "5m", "limit": 500,
                      "endTime": cursor}
            try:
                rows = _get(url, params)
            except Exception as e:
                logger.warning(f"{url} fetch failed: {e}")
                break
            if not rows:
                break
            for r in rows:
                ts_ms = int(r.get("timestamp") or r.get("time") or 0)
                if ts_ms == 0:
                    continue
                ts = pd.Timestamp(ts_ms, unit="ms").as_unit("ms")
                if ts not in out.index:
                    continue
                values = transform(r) if transform else r
                for src_key, col in key_to_col_map.items():
                    v = values.get(src_key)
                    if v is not None:
                        try:
                            out.at[ts, col] = float(v)
                        except (ValueError, TypeError):
                            pass
            seen += len(rows)
            oldest_ts = int(rows[0].get("timestamp") or rows[0].get("time") or 0)
            if oldest_ts <= start_ms or len(rows) < 500 or seen > 5000:
                break
            cursor = oldest_ts

    raw_oi: list = []
    cursor = end_ms
    for _ in range(20):
        try:
            rows = _get(OI_URL, {"symbol": SYMBOL, "period": "5m",
                                 "limit": 500, "endTime": cursor})
        except Exception as e:
            logger.warning(f"OI fetch failed: {e}")
            break
        if not rows: break
        raw_oi.extend(rows)
        oldest_ts = int(rows[0]["timestamp"])
        if oldest_ts <= start_ms or len(rows) < 500: break
        cursor = oldest_ts
    if raw_oi:
        oi_df = pd.DataFrame(raw_oi)
        oi_df["ts"] = pd.to_datetime(oi_df["timestamp"].astype("int64"),
                                     unit="ms", utc=True).dt.tz_localize(None)
        oi_df = oi_df.set_index("ts").sort_index()
        oi_df = oi_df[~oi_df.index.duplicated(keep="last")]
        oi_df["sumOpenInterest"] = pd.to_numeric(oi_df["sumOpenInterest"])
        oi_chg_1h = oi_df["sumOpenInterest"].pct_change(12)
        out.loc[oi_chg_1h.index.intersection(out.index), "oi_1h"] = \
            oi_chg_1h.reindex(out.index)

    _paginate(TOP_POS_URL, {"longShortRatio": "_top_pos_lsr"})
    if "_top_pos_lsr" in out.columns:
        out["top_pos"] = out["_top_pos_lsr"] / (1 + out["_top_pos_lsr"])
        out = out.drop(columns=["_top_pos_lsr"])

    _paginate(GLOBAL_ACC_URL, {"longShortRatio": "_global_acc_lsr"})
    if "_global_acc_lsr" in out.columns:
        global_acc_ratio = out["_global_acc_lsr"] / (1 + out["_global_acc_lsr"])
        out["smart_skew"] = out["top_pos"] - global_acc_ratio
        out = out.drop(columns=["_global_acc_lsr"])

    _paginate(TAKER_URL, {"buySellRatio": "taker_ratio"})

    try:
        fund_start_ms = start_ms - 5 * 24 * 3600 * 1000
        fund_rows = _get(FUNDING_URL, {"symbol": SYMBOL, "limit": 1000,
                                       "startTime": fund_start_ms,
                                       "endTime": end_ms})
        fund_df = pd.DataFrame(fund_rows)
        if not fund_df.empty:
            fund_df["ts"] = pd.to_datetime(fund_df["fundingTime"].astype("int64"),
                                           unit="ms", utc=True).dt.tz_localize(None)
            fund_df["fund_rate"] = pd.to_numeric(fund_df["fundingRate"])
            fund_df = fund_df.set_index("ts").sort_index()[["fund_rate"]]
            ff = fund_df.reindex(out.index, method="ffill")
            out["fund_rate"] = ff["fund_rate"]
    except Exception as e:
        logger.warning(f"funding fetch failed: {e}")

    return out

router = APIRouter()

MAX_HOURS = 720


class BacktestBody(BaseModel):
    strategy: Optional[str] = Field(
        None, description="Strategy name from REGISTRY. Defaults to current active."
    )
    from_ts: Optional[str] = None
    to_ts:   Optional[str] = None
    hours:   Optional[int] = Field(None, ge=1, le=MAX_HOURS)
    warmup_bars: int = Field(default_factory=live_engine_mod.get_engine_warmup_bars, ge=50, le=5000)
    horizon_bars: int = Field(288, ge=1, le=2016,
        description="Max bars a single trade is held. 288 = 24h on 5m.")
    details: str = Field("summary", pattern=r"^(summary|fires|full)$")


def _resolve_window(b: BacktestBody) -> tuple[datetime, datetime]:
    if b.from_ts and b.to_ts:
        f = datetime.fromisoformat(b.from_ts).replace(tzinfo=None)
        t = datetime.fromisoformat(b.to_ts).replace(tzinfo=None)
    else:
        hours = b.hours or 48
        if hours > MAX_HOURS:
            raise HTTPException(400, f"hours capped at {MAX_HOURS}")
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
        floor5 = now.replace(minute=now.minute - (now.minute % 5))
        t = floor5 - pd.Timedelta(minutes=5)
        f = t - pd.Timedelta(hours=hours) + pd.Timedelta(minutes=5)
    if t <= f:
        raise HTTPException(400, "to_ts must be after from_ts")
    return f, t


@router.post("/backtest")
async def backtest(body: BacktestBody):
    if body.strategy:
        strat_name = body.strategy
    else:
        actives = get_active()
        if not actives:
            raise HTTPException(400, "no tier='active' strategy in registry; pass body.strategy explicitly")
        strat_name = actives[0].name
    if strat_name not in REGISTRY:
        raise HTTPException(404, f"unknown strategy '{strat_name}'. "
                                  f"available: {list(REGISTRY)}")
    strat = REGISTRY[strat_name]

    f_ts, t_ts = _resolve_window(body)
    window_bars = int((t_ts - f_ts).total_seconds() // 300) + 1
    total_bars = body.warmup_bars + window_bars

    end_ms = int((t_ts + pd.Timedelta(minutes=5)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    t0 = time.perf_counter()
    chunks: list[pd.DataFrame] = []
    remaining = total_bars
    cursor_end_ms = end_ms
    while remaining > 0:
        take = min(remaining, 1500)
        chunk = fetch_klines(SYMBOL, "5m", limit=take, end_ms=cursor_end_ms)
        if chunk.empty:
            break
        chunks.append(chunk)
        oldest = chunk.index[0]
        cursor_end_ms = int(oldest.replace(tzinfo=timezone.utc).timestamp() * 1000)
        remaining -= len(chunk)
        if len(chunk) < take:
            break
    df = pd.concat(chunks).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    fetch_ms = (time.perf_counter() - t0) * 1000

    df = df[df.index <= t_ts]
    if len(df) < window_bars + 50:
        raise HTTPException(
            502,
            f"venue returned only {len(df)} bars, need >= {window_bars+50} "
            f"(window={window_bars}, warmup={body.warmup_bars})",
        )

    t1 = time.perf_counter()
    df_ind = build_indicators(df[["open","high","low","close","volume"]].copy())
    indicator_ms = (time.perf_counter() - t1) * 1000

    window_idx = df_ind[df_ind.index >= f_ts].index
    fires = []
    fires_buy = 0
    fires_sell = 0
    unique_why_tags: set[str] = set()
    bars_with_fire: set = set()

    needs_metrics = getattr(strat, "needs_exchange_metrics", False)
    metrics_df = None
    if needs_metrics:
        try:
            metrics_df = _fetch_historical_exchange_metrics(f_ts, t_ts)
            metrics_df = metrics_df.reindex(window_idx)
            logger.info(f"fetched + aligned historical exchange metrics: {metrics_df.shape}, "
                        f"non-null per col: {metrics_df.notna().sum().to_dict()}")
        except Exception as e:
            logger.warning(f"could not fetch historical metrics: {e}")
            metrics_df = None

    ret_6 = df["close"].pct_change(6).reindex(window_idx)

    def _bm_for(ts):
        d = {"taker_ratio": None, "imb1pct": None, "imb5pct": None,
             "smart_skew": None, "oi_1h": None, "fund_rate": None,
             "top_pos": None, "ret_6": None}
        if metrics_df is not None:
            try:
                row = metrics_df.loc[ts]
                for k in d:
                    if k in row.index and pd.notna(row[k]):
                        d[k] = float(row[k])
            except KeyError:
                pass
        try:
            v = ret_6.loc[ts]
            if pd.notna(v):
                d["ret_6"] = float(v)
        except KeyError:
            pass
        return d

    t2 = time.perf_counter()
    n_bm_hits = 0
    skipped_overlap = 0
    wins = 0
    losses = 0
    timeouts = 0
    consec_losses = 0
    max_consec_losses = 0
    bars_held_winners: list[int] = []

    df_idx_for_loc = df_ind.index
    closes_arr = df_ind["close"].to_numpy()
    highs_arr = df_ind["high"].to_numpy()
    lows_arr = df_ind["low"].to_numpy()
    df_len = len(df_ind)

    position_busy_until_idx = -1

    for ts in window_idx:
        abs_idx = df_idx_for_loc.get_loc(ts)
        slice_df = df_ind.loc[:ts]
        try:
            if needs_metrics:
                bm = _bm_for(ts)
                if any(bm.get(k) is not None
                       for k in ("taker_ratio", "smart_skew", "oi_1h",
                                 "fund_rate", "top_pos")):
                    n_bm_hits += 1
                resp = strat.decide(slice_df, bm)
            else:
                resp = strat.decide(slice_df)
        except Exception as e:
            logger.warning(f"strategy {strat_name} raised on {ts}: {e}")
            continue
        action = resp.get("action") or "HOLD"
        if action not in ("OPEN_LONG", "OPEN_SHORT"):
            continue

        if abs_idx <= position_busy_until_idx:
            skipped_overlap += 1
            continue

        why = resp.get("why", "")
        sl_pct = float(resp.get("sl_pct", 0.005))
        tp_pct = float(resp.get("tp_pct", 0.005))
        is_long = (action == "OPEN_LONG")
        entry_price = float(closes_arr[abs_idx])
        if is_long:
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)

        result = "TIMEOUT"
        exit_idx = min(abs_idx + body.horizon_bars, df_len - 1)
        exit_price = float(closes_arr[exit_idx])
        bars_held = exit_idx - abs_idx
        for j in range(1, min(body.horizon_bars + 1, df_len - abs_idx)):
            h = float(highs_arr[abs_idx + j])
            l = float(lows_arr[abs_idx + j])
            if is_long:
                if h >= tp_price:
                    result, exit_idx, exit_price, bars_held = "TP", abs_idx + j, tp_price, j
                    break
                if l <= sl_price:
                    result, exit_idx, exit_price, bars_held = "SL", abs_idx + j, sl_price, j
                    break
            else:
                if l <= tp_price:
                    result, exit_idx, exit_price, bars_held = "TP", abs_idx + j, tp_price, j
                    break
                if h >= sl_price:
                    result, exit_idx, exit_price, bars_held = "SL", abs_idx + j, sl_price, j
                    break

        if result == "TIMEOUT":
            timeouts += 1
            if (is_long and exit_price > entry_price) or (not is_long and exit_price < entry_price):
                result = "TIMEOUT_TP"
            else:
                result = "TIMEOUT_SL"

        is_win = result in ("TP", "TIMEOUT_TP")
        if is_win:
            wins += 1
            consec_losses = 0
            bars_held_winners.append(bars_held)
        else:
            losses += 1
            consec_losses += 1
            if consec_losses > max_consec_losses:
                max_consec_losses = consec_losses

        fires.append({
            "ts": ts.isoformat(),
            "action": action,
            "why": why,
            "entry_price": entry_price,
            "exit_ts": df_idx_for_loc[exit_idx].isoformat(),
            "exit_price": float(exit_price),
            "result": result,
            "bars_held": bars_held,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
        })
        unique_why_tags.add(why)
        bars_with_fire.add(ts)
        if is_long:
            fires_buy += 1
        else:
            fires_sell += 1

        position_busy_until_idx = exit_idx

    eval_ms = (time.perf_counter() - t2) * 1000

    coverage = {
        "n_columns": len(df_ind.columns),
        "sample": sorted(c for c in df_ind.columns
                         if c not in ("open", "high", "low", "close", "volume"))[:50],
    }

    payload = {
        "strategy": {
            "name": strat.name,
            "tier": strat.tier,
            "description": strat.description,
            "file": strat.file,
        },
        "window": {
            "from": f_ts.isoformat(),
            "to":   t_ts.isoformat(),
            "bars": len(window_idx),
        },
        "fires": {
            "total": len(fires),
            "buy":   fires_buy,
            "sell":  fires_sell,
            "unique_why_tags": len(unique_why_tags),
            "bars_with_fire": len(bars_with_fire),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / len(fires)) if fires else 0.0,
            "max_consecutive_losses": max_consec_losses,
            "timeouts": timeouts,
            "skipped_overlap": skipped_overlap,
            "horizon_bars": body.horizon_bars,
            "mean_bars_held_winners": (
                round(sum(bars_held_winners) / len(bars_held_winners), 1)
                if bars_held_winners else None
            ),
        },
        "indicator_coverage": coverage,
        "exchange_metrics_coverage": {
            "needed": needs_metrics,
            "fetched_from_api": metrics_df is not None,
            "bars_with_metrics": int(n_bm_hits),
            "total_bars": len(window_idx),
            "available_metrics": ["taker_ratio", "oi_1h", "smart_skew",
                                  "top_pos", "fund_rate"],
            "unavailable_metrics": {
                "imb1pct": "no historical /depth endpoint exists on most venues",
                "imb5pct": "no historical /depth endpoint exists on most venues",
            },
        } if needs_metrics else None,
        "timing_ms": {
            "fetch_klines":    round(fetch_ms, 1),
            "build_indicators": round(indicator_ms, 1),
            "evaluate_window":  round(eval_ms, 1),
        },
    }
    if body.details in ("fires", "full"):
        payload["fires"]["list"] = fires
    if body.details == "full":
        payload["window"]["bars_evaluated"] = [ts.isoformat() for ts in window_idx]

    return {"ok": True,
            "ts": datetime.now(timezone.utc).isoformat(),
            "data": payload}
