"""FastAPI introspection + trade-management for the trading bot.

Endpoints
---------
GET  /health                     liveness/readiness probe
GET  /state                      current bot state (position, equity, last tick)
GET  /strategies                 list registered strategies grouped by tier
GET  /live-strategy              the strategies currently driving live trades
GET  /accounts                   configured accounts with leverage + strategy filter
GET  /trade-history?n=100        recent trade events (entries / fills / closes)
GET  /signal-history?n=200       strategy fires (active and monitoring tiers)
GET  /open-trades                live open orders + position from the exchange
GET  /indicators?n=200           per-tick TA / orderbook / flow snapshots
GET  /decisions?n=200            full per-tick decision records
GET  /model-predictions?n=200    rolling buffer of model.predict() outputs
GET  /per-strategy-stats?hours=72  per-strategy WR / fires / PnL replay
POST /trade/close                force-close any open position (reason='manual')
POST /trade/close-limit          maker-only close: postOnly LIMIT reduceOnly retry
POST /trade/open                 open a manual trade  (body: {side, sl_pct, tp_pct})
POST /backtest                   replay a registered strategy over a window

The trade-management POSTs are gated by an IP whitelist at the HTTP layer;
configure via `BOT_IP_WHITELIST` (comma-separated). For local dev set
`BOT_IP_WHITELIST_DISABLED=1`. There is no API key — IP whitelist + whatever
network gate you put in front of the host is the only auth.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

import pandas as pd
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server.api.backtest import router as backtest_router
from server.bot.logging_setup import configure as configure_logging
from server.bot.main import LOG_DIR, get_bot_instance, start_bot_thread
from server.bot.state_store import STATE
from server.config.accounts import (
    DEFAULT_ACCOUNT_ID, get_account_ids, get_accounts, get_strategies_for_account,
)
from server.strategies import REGISTRY, get_active, get_archived, get_monitoring

logger = logging.getLogger("TradingBot.api")

# ---- IP whitelist middleware -------------------------------------------------

_WHITELIST_RAW = os.environ.get("BOT_IP_WHITELIST", "")
_WHITELIST = {ip.strip() for ip in _WHITELIST_RAW.split(",") if ip.strip()}
_WHITELIST_DISABLED = os.environ.get("BOT_IP_WHITELIST_DISABLED", "0") == "1"


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---- lifespan: start the bot tick loop in a daemon thread --------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(LOG_DIR)
    try:
        STATE.hydrate_from_disk(LOG_DIR)
    except Exception as e:
        logger.error(f"state hydration failed (continuing with empty state): {e}", exc_info=True)
    if os.environ.get("BOT_DISABLE_TICK", "0") != "1":
        logger.info("FastAPI startup → launching bot tick thread")
        try:
            start_bot_thread()
        except Exception as e:
            logger.error(f"failed to start bot thread: {e}", exc_info=True)
    else:
        logger.info("BOT_DISABLE_TICK=1 — API only, no bot loop")
    yield
    logger.info("FastAPI shutdown")


app = FastAPI(
    title="TradingBot API",
    version="0.1.0",
    description="Read-only introspection + trade management for an algorithmic trading bot.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ip_whitelist_middleware(request: Request, call_next):
    if _WHITELIST_DISABLED or not _WHITELIST:
        return await call_next(request)
    ip = _client_ip(request)
    if ip not in _WHITELIST:
        logger.warning(f"blocked request from {ip} ({request.url.path})")
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": f"forbidden: {ip} not in whitelist"},
        )
    return await call_next(request)


# ---- response helpers --------------------------------------------------------

def _ok(payload: dict | list, **extra) -> dict:
    return {
        "ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
        "data": payload,
        **extra,
    }


def _strategy_summary(s) -> dict:
    return {
        "name": s.name,
        "tier": s.tier,
        "description": s.description,
        "file": s.file,
        "priority": getattr(s, "priority", 100),
        "needs_exchange_metrics": getattr(s, "needs_exchange_metrics", False),
    }


# ---- endpoints ---------------------------------------------------------------

app.include_router(backtest_router)


@app.get("/")
async def root():
    return _ok({
        "service": "trading-bot-api",
        "endpoints": [
            "/health", "/state", "/accounts", "/strategies", "/live-strategy",
            "/trade-history", "/signal-history", "/decisions", "/indicators",
            "/model-predictions", "/per-strategy-stats", "/open-trades",
            "/trade/close (POST)", "/trade/close-limit (POST)",
            "/trade/open (POST)", "/backtest (POST)",
        ],
    })


@app.get("/live-strategy")
async def live_strategy():
    """The strategies currently driving live trades, in priority order."""
    actives = get_active()
    if not actives:
        return _ok({"stack": [], "note": "no tier='active' strategies in registry"})
    stack = [_strategy_summary(s) for s in actives]
    return _ok({"stack": stack, "primary": stack[0]})


@app.get("/strategies")
async def strategies():
    """All registered strategies, grouped by tier."""
    return _ok({
        "active":     [_strategy_summary(s) for s in get_active()],
        "monitoring": [_strategy_summary(s) for s in get_monitoring()],
        "archived":   [_strategy_summary(s) for s in get_archived()],
    })


@app.get("/health")
async def health():
    h = STATE.get_health()
    code = 200 if h.get("status") == "ok" else 503 if h.get("status") == "warming" else 500
    return JSONResponse(status_code=code, content=_ok(h))


@app.get("/state")
async def state(account: str | None = None):
    """Single-account view by default (account1). Pass ?account=accountN to
    inspect a specific account, or ?account=all for every account at once."""
    if account == "all":
        return _ok(STATE.get_all_states())
    aid = account or DEFAULT_ACCOUNT_ID
    return _ok(STATE.get_state(aid))


@app.get("/accounts")
async def accounts():
    """Configured accounts with their permitted strategies + leverage."""
    return _ok({
        "accounts": [
            {
                "account_id": aid,
                "strategies": get_strategies_for_account(aid),
                "config": get_accounts()[aid],
            }
            for aid in get_account_ids()
        ],
        "default": DEFAULT_ACCOUNT_ID,
    })


@app.get("/trade-history")
async def trade_history(n: int = 100, account: str | None = None):
    n = max(1, min(n, 500))
    trades = STATE.get_recent_trades(n)
    if account and account != "all":
        trades = [t for t in trades if t.get("account_id", DEFAULT_ACCOUNT_ID) == account]
    return _ok(trades, n=n)


@app.get("/signal-history")
async def signal_history(n: int = 200):
    n = max(1, min(n, 1000))
    return _ok(STATE.get_signal_hits(n), n=n)


@app.get("/model-predictions")
async def model_predictions(n: int = 200, engine: str | None = None):
    """Rolling buffer of model `predict()` outputs.

    Strategies that wrap an ML model write a record per closed bar regardless
    of whether the prediction crossed any decision threshold. Filter by
    `?engine=<strategy_name>` to slice predictions from a single model.
    """
    n = max(1, min(n, 2000))
    preds = STATE.get_model_predictions(n)
    if engine:
        preds = [p for p in preds if p.get("engine") == engine]
    return _ok(preds, n=n)


@app.get("/per-strategy-stats")
async def per_strategy_stats(hours: int = 72):
    """Per-strategy independent-slot replay over the last `hours`.

    For every strategy that fired in the window, replay its signals against
    actual klines with a one-trade-per-strategy slot rule (first-fire wins).
    Returns per-strategy fires / wins / losses / timeouts / WR / PnL — the
    'blockchain of trades' view that lets you promote/demote strategies on
    real data instead of vibes."""
    from server.engine.live_engine import fetch_klines, SYMBOL

    hours = max(1, min(hours, 168))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    sigs = STATE.get_signal_hits(1000)

    def _parse(ts):
        t = datetime.fromisoformat(ts) if "+" in ts or "Z" in ts else datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t

    sigs = [s for s in sigs if _parse(s["ts"]) >= cutoff]
    if not sigs:
        return _ok({"strategies": {}, "window_hours": hours, "n_sigs": 0})

    earliest = min(_parse(s["ts"]) for s in sigs) - timedelta(minutes=10)
    end_ms = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp() * 1000)
    chunks = []
    cursor = end_ms
    start_ms = int(earliest.timestamp() * 1000)
    while True:
        rows = fetch_klines(SYMBOL, "5m", limit=1500, end_ms=cursor)
        if rows.empty:
            break
        chunks.append(rows)
        oldest_ms = int(rows.index[0].replace(tzinfo=timezone.utc).timestamp() * 1000)
        if oldest_ms <= start_ms or len(rows) < 1500:
            break
        cursor = oldest_ms - 1
    if not chunks:
        return _ok({"strategies": {}, "window_hours": hours,
                    "n_sigs": len(sigs), "note": "kline fetch returned empty"})
    klines = pd.concat(chunks).sort_index()
    klines = klines[~klines.index.duplicated(keep="first")]
    idx_pos = {ts: i for i, ts in enumerate(klines.index)}

    by_strat: dict[str, list] = {}
    seen = set()
    for s in sigs:
        src = s.get("engine")
        if not src or src not in REGISTRY:
            continue
        bar_key = s.get("bar_ts") or s.get("ts")
        ts = pd.Timestamp(bar_key).tz_localize(None) if pd.Timestamp(bar_key).tz is not None else pd.Timestamp(bar_key)
        key = (src, ts)
        if key in seen:
            continue
        seen.add(key)
        by_strat.setdefault(src, []).append({
            "ts": ts,
            "side": "LONG" if s.get("action") == "OPEN_LONG" else "SHORT",
            "why": (s.get("why") or "")[:60],
        })

    HORIZON = 288   # 24h on 5m

    def walk(side, ei, ep, tp_p, sl_p):
        tp = ep * (1 + tp_p) if side == "LONG" else ep * (1 - tp_p)
        sl = ep * (1 - sl_p) if side == "LONG" else ep * (1 + sl_p)
        for j in range(1, HORIZON + 1):
            if ei + j >= len(klines):
                break
            b = klines.iloc[ei + j]
            if side == "LONG":
                if b["low"] <= sl:  return ("SL", j, sl)
                if b["high"] >= tp: return ("TP", j, tp)
            else:
                if b["high"] >= sl: return ("SL", j, sl)
                if b["low"] <= tp:  return ("TP", j, tp)
        fi = min(ei + HORIZON, len(klines) - 1)
        fp = klines.iloc[fi]["close"]
        won = (fp > ep) if side == "LONG" else (fp < ep)
        return ("TIMEOUT_W" if won else "TIMEOUT_L", HORIZON, fp)

    results: dict[str, dict] = {}
    for strat, fires in by_strat.items():
        fires = sorted(fires, key=lambda x: x["ts"])
        tp_p, sl_p = (0.005, 0.005)
        busy = -1
        taken: list[dict] = []
        skipped = 0
        for f in fires:
            ei = idx_pos.get(f["ts"], -1)
            if ei < 0:
                continue
            ei += 1
            if ei >= len(klines):
                continue
            if ei <= busy:
                skipped += 1
                continue
            ep = klines.iloc[ei]["open"]
            outcome, bars, xp = walk(f["side"], ei, ep, tp_p, sl_p)
            pnl = (xp - ep) / ep if f["side"] == "LONG" else (ep - xp) / ep
            taken.append({
                "ts": f["ts"].isoformat(),
                "side": f["side"],
                "outcome": outcome,
                "bars_held": bars,
                "entry": float(ep),
                "exit": float(xp),
                "pnl_pct": round(pnl * 100, 4),
                "why": f["why"],
            })
            busy = ei + bars

        tps = sum(1 for r in taken if r["outcome"] == "TP")
        sls = sum(1 for r in taken if r["outcome"] == "SL")
        tow = sum(1 for r in taken if r["outcome"] == "TIMEOUT_W")
        tol = sum(1 for r in taken if r["outcome"] == "TIMEOUT_L")
        w = tps + tow
        n = len(taken)
        wr = round(w / n * 100, 2) if n else None
        total_pnl = round(sum(r["pnl_pct"] for r in taken), 4)

        results[strat] = {
            "raw_signals": len(fires),
            "trades_taken": n,
            "skipped_busy": skipped,
            "tp": tps, "sl": sls, "timeout_w": tow, "timeout_l": tol,
            "win_rate_pct": wr,
            "total_pnl_pct": total_pnl,
            "avg_per_trade_pct": round(total_pnl / n, 4) if n else None,
            "tp_pct": tp_p, "sl_pct": sl_p,
            "trades": taken[-30:],
        }

    return _ok({
        "window_hours": hours,
        "n_sigs": len(sigs),
        "n_kline_bars": len(klines),
        "strategies": results,
    })


@app.get("/indicators")
async def indicators(n: int = 200):
    n = max(1, min(n, 500))
    return _ok(STATE.get_recent_indicators(n), n=n)


@app.get("/decisions")
async def decisions(n: int = 200):
    n = max(1, min(n, 1000))
    return _ok(STATE.get_recent_decisions(n), n=n)


@app.get("/open-trades")
async def open_trades(account: str | None = None):
    """Live positions + open orders."""
    bot = get_bot_instance()
    if bot is None:
        aid = account or DEFAULT_ACCOUNT_ID
        return _ok({"position": STATE.get_state(aid),
                    "open_orders": STATE.get_open_orders(aid),
                    "account_id": aid,
                    "live": False, "note": "bot tick thread not running"})

    aids = list(bot.exchanges.keys()) if account == "all" else [account or DEFAULT_ACCOUNT_ID]
    out: dict[str, dict] = {}
    for aid in aids:
        ex = bot.exchanges.get(aid)
        if ex is None:
            out[aid] = {"error": f"no exchange client for {aid}"}
            continue
        try:
            positions = ex.fetch_positions([bot.symbol])
            open_orders = ex.fetch_open_orders(bot.symbol)
            out[aid] = {
                "live": True,
                "positions": [
                    {"symbol": p.get("symbol"), "side": p.get("side"),
                     "contracts": p.get("contracts"), "entryPrice": p.get("entryPrice"),
                     "unrealizedPnl": p.get("unrealizedPnl")}
                    for p in positions
                ],
                "open_orders": [
                    {"id": o.get("id"), "type": o.get("type"), "side": o.get("side"),
                     "price": o.get("price"), "amount": o.get("amount"),
                     "stopPrice": o.get("stopPrice"), "status": o.get("status"),
                     "reduceOnly": o.get("reduceOnly")}
                    for o in open_orders
                ],
            }
        except Exception as e:
            logger.warning(f"open-trades fetch failed for {aid}: {e}")
            out[aid] = {"error": f"exchange fetch failed: {e}"}

    if account == "all":
        return _ok(out)
    return _ok(out[aids[0]])


# ---- trade management --------------------------------------------------------

class OpenTradeBody(BaseModel):
    side: str = Field(..., description="'buy' or 'sell'")
    sl_pct: float = Field(0.005, ge=0.001, le=0.05)
    tp_pct: float = Field(0.005, ge=0.001, le=0.10)


class CloseLimitBody(BaseModel):
    max_attempts: int = Field(3, ge=1, le=20)
    wait_seconds: float = Field(5.0, ge=0.5, le=60.0)
    offset_ticks: int = Field(0, ge=0, le=100,
                              description="Sit N ticks deeper than best bid/ask.")
    fallback_market: bool = Field(False,
                                  description="If true, market-close after maker attempts exhaust.")


@app.post("/trade/close")
async def trade_close(account: str | None = None):
    bot = get_bot_instance()
    if bot is None:
        raise HTTPException(status_code=503, detail="bot tick thread not running")
    aid = account or DEFAULT_ACCOUNT_ID
    st = STATE.account_state(aid)
    if st.position == "flat":
        return _ok({"closed": False, "reason": "no open position", "account_id": aid})
    bot.close_position("MANUAL_API", account_id=aid)
    return _ok({"closed": True, "side": st.position, "account_id": aid})


@app.post("/trade/close-limit")
async def trade_close_limit(body: CloseLimitBody, account: str | None = None):
    """Close current position with postOnly LIMIT reduceOnly orders (maker fee
    only). Retries up to `max_attempts` times, waiting `wait_seconds` per try.
    """
    bot = get_bot_instance()
    if bot is None:
        raise HTTPException(status_code=503, detail="bot tick thread not running")
    aid = account or DEFAULT_ACCOUNT_ID
    st = STATE.account_state(aid)
    if st.position == "flat":
        return _ok({"closed": False, "reason": "no open position",
                    "attempts_used": 0, "account_id": aid})
    result = bot.close_position_limit(
        max_attempts=body.max_attempts,
        wait_seconds=body.wait_seconds,
        offset_ticks=body.offset_ticks,
        fallback_market=body.fallback_market,
        reason="MANUAL_API_LIMIT",
        account_id=aid,
    )
    return _ok({**result, "account_id": aid})


@app.post("/trade/open")
async def trade_open(body: OpenTradeBody, account: str | None = None):
    bot = get_bot_instance()
    if bot is None:
        raise HTTPException(status_code=503, detail="bot tick thread not running")
    if body.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    aid = account or DEFAULT_ACCOUNT_ID
    st = STATE.account_state(aid)
    if st.position != "flat":
        raise HTTPException(status_code=409, detail=f"[{aid}] already in {st.position}")
    bot.execute_trade(body.side, body.sl_pct, body.tp_pct,
                      strategy="manual_api", account_id=aid)
    return _ok({"opened": st.position != "flat",
                "side": st.position,
                "entry": st.entry,
                "sl": st.sl,
                "tp": st.tp,
                "account_id": aid})
