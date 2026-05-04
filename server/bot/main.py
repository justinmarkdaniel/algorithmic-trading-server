"""Trading bot tick loop — synchronous, single-process, FastAPI-friendly.

Architecture summary
--------------------
The bot is a daemon thread that wakes on the 5-minute boundary, asks the
engine for the latest indicator-augmented dataframe, walks the active strategy
registry, and dispatches a trade if any strategy returns a non-HOLD response.

Concerns are split deliberately:
  - state           lives in `server.bot.state_store.STATE` (shared with FastAPI)
  - structured logs go through `server.bot.logging_setup.configure()`
  - trade events    emitted as JSON-line records (`event=trade_*`) for replay
  - tick decisions  written into the state-store ring-buffer for `/decisions`
  - order placement uses a multi-stage chaser (maker first, taker fallback)

To run as a standalone daemon:
    python -m server.bot.main

To run inside the FastAPI container, the API process imports
`start_bot_thread` and runs this loop alongside it. State is shared in
memory; reads are non-blocking.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from server.bot.logging_setup import configure as configure_logging
from server.bot.state_store import STATE
from server.config.accounts import (
    DEFAULT_ACCOUNT_ID, get_account_ids, get_leverage, get_notional_pct,
    is_multi_account_mode, is_strategy_allowed,
)
from server.engine import LiveEngine, EngineConfig
from server.exchange import get_client
from server.strategies import REGISTRY, get_active, get_live_decider, get_monitoring

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")

CONFIG_PATH = Path(os.environ.get(
    "BOT_CONFIG_PATH",
    str(PROJECT_ROOT / "server" / "config" / "production.json"),
))
LOG_DIR = Path(os.environ.get("BOT_LOG_DIR", str(PROJECT_ROOT / "logs")))

MAX_DAILY_TRADES = int(os.environ.get("BOT_MAX_DAILY_TRADES", "10"))
MAX_CONSEC_LOSSES = int(os.environ.get("BOT_MAX_CONSEC_LOSSES", "10"))
COOLDOWN_HOURS_AFTER_LOSS_STREAK = int(os.environ.get("BOT_COOLDOWN_HOURS", "24"))
# Default hold-timeout in 5m bars when a strategy doesn't declare its own.
# 48 × 5min = 4h — wide enough for most rule-based 5m strategies but short
# enough to free the slot if a setup decays without hitting TP/SL.
#
# Per-strategy overrides come from the decide() response dict (alongside
# `sl_pct` / `tp_pct` / `why`) so different strategies — or different combos
# within one strategy — can request different timeouts. A strategy declares
# the override by returning `max_hold_bars` (int) plus `max_hold_bars_tf`
# (str: '5m' | '1h'). The bot converts to 5m bars on entry and stores the
# resolved count on BotState.max_hold_bars_5m.
MAX_HOLD_BARS = int(os.environ.get("BOT_MAX_HOLD_BARS", "48"))
MAX_HOLD_BARS_TF = "5m"
_TF_TO_5M_BARS = {"5m": 1, "1h": 12}


def resolve_combo_max_hold_5m(resp: dict | None) -> int | None:
    """Read `max_hold_bars` + `max_hold_bars_tf` from a strategy's decide()
    response and convert to 5m bars. Returns None when not declared (caller
    falls back to global MAX_HOLD_BARS)."""
    if not resp:
        return None
    n = resp.get("max_hold_bars")
    if n is None:
        return None
    tf = resp.get("max_hold_bars_tf", "5m")
    mult = _TF_TO_5M_BARS.get(tf, 1)
    return int(n) * mult

# Adaptive escalation chaser — replaces the 3×15s GTX-only loop.
# Designed to fill ≥95% of signals while preserving maker rebate when possible.
# Wall-clock budget per signal: ~6s total (was 45s).
ENTRY_STAGE1_POST_ONLY_S    = 3    # Stage 1: GTX post-only at bid (maker rebate)
ENTRY_STAGE2_GTC_INSIDE_S   = 2    # Stage 2: GTC limit at bid+1tick — inside the spread, rests as maker
ENTRY_STAGE3_IOC_TAKE_S     = 1    # Stage 3: IOC limit at ask+3bps (taker, instant)
ENTRY_STAGE3_SLIP_BPS       = 3    # 3bps slippage budget for taker fill
ENTRY_POLL_SEC              = 1    # 1Hz poll — comfortably under typical venue rate limits, near RTT floor

logger = logging.getLogger("TradingBot")


def _ms_between(start_iso: str | None, end_dt: datetime) -> float | None:
    """Return ms elapsed between an ISO timestamp and a datetime, or None."""
    if not start_iso:
        return None
    try:
        start = datetime.fromisoformat(start_iso)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return round((end_dt - start).total_seconds() * 1000, 1)
    except (ValueError, TypeError):
        return None


def _send_alert(message: str, data: dict | None = None) -> None:
    try:
        requests.post(WEBHOOK_URL, json={"message": message, "data": data or {}},
                      headers={"Content-Type": "application/json"}, timeout=10)
    except Exception as e:
        logger.warning(f"alert webhook failed: {e}")


def engine_from_why(why: str, fallback: str = "active_stack") -> str:
    """Identify the firing strategy from its response's `why` tag.

    When multiple actives chain through a dispatcher, the response's
    `why` is the only signal of which one fired. Strategies prefix their
    `why` with a stable marker; this helper matches that marker against
    the live registry so trade events, signal hits, and trades.csv all
    attribute to the same name.

    Falls back to `fallback` when nothing matches — never raises."""
    w = (why or "").strip()
    if not w:
        return fallback
    from server.strategies import REGISTRY
    for name in REGISTRY:
        if w.lower().startswith(name.lower()):
            return name
    return fallback


class TradingBot:
    def __init__(self) -> None:
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)
        self.symbol = self.config.get("symbol", "BTC/USDT:USDT")
        self.account_ids = get_account_ids()

        mode = "MULTI-ACCOUNT" if is_multi_account_mode() else "single-account"
        logger.info(
            "Initialising TradingBot %s — %s mode | accounts=%s | symbol=%s",
            self.config.get("version", "?"), mode, self.account_ids, self.symbol,
        )

        # Build per-account ccxt clients. account1 always — others only if
        # their accounts.json entry exists AND credentials resolve.
        self.exchanges: dict = {}
        for aid in self.account_ids:
            try:
                client = get_client(aid)
                client.load_markets()
                self.exchanges[aid] = client
                logger.info(f"  [{aid}] ccxt client ready")
            except Exception as e:
                if aid == DEFAULT_ACCOUNT_ID:
                    raise   # account1 must always work
                logger.warning(f"  [{aid}] init failed ({e}) — skipping this account")

        if not self.exchanges:
            raise RuntimeError("no accounts initialised — at least account1 must auth")

        # Pick any client to read market metadata from (same symbol everywhere)
        any_client = next(iter(self.exchanges.values()))
        self.market = any_client.market(self.symbol)

        # Single shared engine — klines + indicators are account-agnostic;
        # all accounts trade the same symbol on the same 5m feed.
        self.engine = LiveEngine(EngineConfig(initial_bar_idx=100))

        # Per-account: fetch initial balance, set leverage, log
        self._leverages: dict[str, int] = {}
        for aid, ex in self.exchanges.items():
            try:
                bal = ex.fetch_balance()
                usdt = float(bal.get("USDT", {}).get("total") or 0)
                logger.info(f"  [{aid}] connected. USDT total={usdt}")
                STATE.update_state(account_id=aid, equity=usdt)
            except Exception as e:
                logger.error(f"  [{aid}] auth failed: {e}")
                raise

            leverage = get_leverage(aid)
            self._leverages[aid] = leverage
            try:
                ex.set_leverage(leverage, self.symbol)
                logger.info(f"  [{aid}] leverage set: {leverage}x")
            except Exception as e:
                logger.warning(f"  [{aid}] set_leverage failed (may already be set): {e}")

        # Backwards-compat aliases — single-account code paths keep working
        self.exchange = self.exchanges.get(DEFAULT_ACCOUNT_ID)
        self.leverage = self._leverages.get(DEFAULT_ACCOUNT_ID, 7)

        self.engine.prime()

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._decisions_fp = open(LOG_DIR / "decisions.jsonl", "a")
        self._signal_hits_fp = open(LOG_DIR / "signal_hits.jsonl", "a")
        self._indicators_fp = open(LOG_DIR / "indicators.jsonl", "a")
        self._trades_csv = LOG_DIR / "trades.csv"
        if not self._trades_csv.exists():
            with open(self._trades_csv, "w") as f:
                f.write("timestamp,side,intended_price,qty,sl_pct,tp_pct,strategy,venue\n")

    # ---- exchange helpers ----

    def _refresh_equity(self, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
        try:
            bal = self.exchanges[account_id].fetch_balance()
            STATE.update_state(account_id=account_id,
                               equity=float(bal.get("USDT", {}).get("total") or 0))
        except Exception as e:
            logger.warning(f"[{account_id}] equity refresh failed: {e}")

    def _refresh_position(self, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
        ex = self.exchanges[account_id]
        st = STATE.account_state(account_id)
        try:
            positions = ex.fetch_positions([self.symbol])
            found = False
            for p in positions:
                contracts = float(p.get("contracts") or 0)
                if contracts > 0:
                    found = True
                    side = "long" if p.get("side") == "long" else "short"
                    entry = float(p.get("entryPrice") or 0) or None
                    STATE.update_state(account_id=account_id, position=side, qty=contracts)
                    if st.entry is None and entry is not None:
                        STATE.update_state(account_id=account_id, entry=entry)

                    if st.strategy is None:
                        # Reboot recovery: when the bot restarts mid-trade, the
                        # in-memory `strategy` / `max_hold_bars_5m` / `opened_bar`
                        # are lost. Without restoring them, the soft hold-timeout
                        # check silently skips and the trade can outlive its
                        # intended horizon. We can't recover the original
                        # opened_bar, so we anchor to "now" — worst case is
                        # the timeout fires later than originally intended,
                        # never earlier.
                        STATE.update_state(
                            account_id=account_id,
                            strategy="recovered_on_reboot",
                            opened_bar=self.engine.bar_idx,
                        )
                        logger.info(
                            f"[{account_id}] reboot-recovery: reattached open "
                            f"{side} position (opened_bar={self.engine.bar_idx} = now)"
                        )

                    if st.sl is None or st.tp is None:
                        try:
                            orders = ex.fetch_open_orders(self.symbol)
                            STATE.update_open_orders([
                                {"id": o.get("id"), "type": o.get("type"),
                                 "side": o.get("side"), "price": o.get("price"),
                                 "stopPrice": o.get("stopPrice"),
                                 "reduceOnly": o.get("reduceOnly")}
                                for o in orders
                            ], account_id=account_id)
                            for o in orders:
                                if o.get("reduceOnly") or o.get("info", {}).get("reduceOnly"):
                                    if o.get("type") == "limit":
                                        STATE.update_state(account_id=account_id,
                                                           tp=float(o.get("price") or 0) or st.tp)
                                    elif "stop" in (o.get("type") or "").lower() or o.get("stopPrice"):
                                        STATE.update_state(account_id=account_id,
                                                           sl=float(o.get("stopPrice") or o.get("price") or 0) or st.sl)
                        except Exception as e:
                            logger.warning(f"[{account_id}] open-order sync failed: {e}")
                    break

            if not found and st.position != "flat":
                # external close (TP/SL hit server-side)
                STATE.update_state(account_id=account_id,
                                   position="flat", qty=0.0, entry=None,
                                   sl=None, tp=None, opened_bar=None, bars_held=0)
                STATE.record_trade_event({
                    "event": "trade_close",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "reason": "external_tp_or_sl",
                }, account_id=account_id)
        except Exception as e:
            logger.warning(f"[{account_id}] position refresh failed: {e}")

    def _rollover_day_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for aid in self.account_ids:
            st = STATE.account_state(aid)
            if st.current_day != today:
                STATE.update_state(account_id=aid, current_day=today, trades_today=0)

    def _price_to_prec(self, price: float, account_id: str = DEFAULT_ACCOUNT_ID) -> float:
        return float(self.exchanges[account_id].price_to_precision(self.symbol, price))

    def _amt_to_prec(self, qty: float, account_id: str = DEFAULT_ACCOUNT_ID) -> float:
        return float(self.exchanges[account_id].amount_to_precision(self.symbol, qty))

    def _qty_from_usd(self, notional_usd: float, price: float,
                      account_id: str = DEFAULT_ACCOUNT_ID) -> float:
        return self._amt_to_prec(notional_usd / max(price, 1e-6), account_id)

    def _log_trade_csv(self, side: str, intended_price: float, qty: float,
                       sl_pct: float, tp_pct: float, strategy: str,
                       account_id: str = DEFAULT_ACCOUNT_ID) -> None:
        # 8-column CSV — kept stable so the hydrator + any downstream
        # tooling can rely on the schema across restarts. Account-aware
        # filtering uses the in-memory trade event (which carries
        # account_id) and the JSONL log (per-event JSON).
        venue = os.environ.get("EXCHANGE_VENUE", "exchange")
        with open(self._trades_csv, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()},{side},{intended_price},"
                    f"{qty},{sl_pct},{tp_pct},{strategy},{venue}\n")

    def _record_decision(self, packet: dict, response: dict) -> None:
        decision = {
            "ts": packet["ts"],
            "action": response.get("action"),
            "sl_pct": response.get("sl_pct"),
            "tp_pct": response.get("tp_pct"),
            "why": response.get("why"),
            "price": packet["price"]["c"],
            "state": packet["state"],
            "trend": packet["trend"],
            "momentum": packet["momentum"],
            "orderbook": packet["orderbook"],
            "flow": packet["flow"],
            "funding": packet["funding"],
            "returns": packet["returns"],
        }
        self._decisions_fp.write(json.dumps(decision) + "\n")
        self._decisions_fp.flush()
        STATE.record_decision(decision)

    # ---- order placement ----

    def _cancel_all(self, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
        try:
            self.exchanges[account_id].cancel_all_orders(self.symbol)
            logger.info(f"[{account_id}] Cancelled all open orders.")
        except Exception as e:
            logger.error(f"[{account_id}] cancel_all_orders: {e}")

    def _wait_for_fill(self, order_id: str, deadline_s: float,
                       account_id: str = DEFAULT_ACCOUNT_ID) -> tuple[bool, dict | None]:
        """Poll the order until filled or deadline passes. Returns (filled, last_status)."""
        ex = self.exchanges[account_id]
        deadline = time.monotonic() + deadline_s
        last_status = None
        while time.monotonic() < deadline:
            try:
                last_status = ex.fetch_order(order_id, self.symbol)
            except Exception:
                last_status = None
            if last_status and last_status.get("status") == "closed":
                return True, last_status
            time.sleep(ENTRY_POLL_SEC)
        return False, last_status

    def _refresh_quote(self, side: str,
                       account_id: str = DEFAULT_ACCOUNT_ID) -> tuple[float, float, float]:
        """Return (bid, ask, mid) live."""
        t = self.exchanges[account_id].fetch_ticker(self.symbol)
        bid = float(t.get("bid") or t.get("last"))
        ask = float(t.get("ask") or t.get("last"))
        return bid, ask, (bid + ask) / 2.0

    def execute_trade(self, side: str, sl_pct: float, tp_pct: float,
                      signal_fire_ts: str | None = None,
                      strategy: str | None = None,
                      account_id: str = DEFAULT_ACCOUNT_ID,
                      max_hold_bars_5m: int | None = None) -> None:
        # Per-account: each account has its own ccxt client + isolated state
        ex = self.exchanges[account_id]
        st = STATE.account_state(account_id)
        leverage = self._leverages.get(account_id, 7)

        if st.position != "flat":
            logger.info(f"[{account_id}] Already in {st.position}. Ignoring new signal.")
            return
        # Default to 'active_stack' so trades.csv / trade-history always carry
        # an engine name even on the unlikely path where the caller forgot to
        # pass one (e.g. /trade/open manual entries from the API).
        strategy = strategy or "active_stack"

        attempt_started_at = datetime.now(timezone.utc)
        t_attempt = time.monotonic()
        signal_to_attempt_ms = _ms_between(signal_fire_ts, attempt_started_at)

        self._cancel_all(account_id)
        try:
            bid0, ask0, mid0 = self._refresh_quote(side, account_id)
            decision_price = bid0 if side == "buy" else ask0

            bal = ex.fetch_balance()
            usdt_total = float(bal.get("USDT", {}).get("total") or 0)
            # 3% margin headroom: many venues compute initial-margin off the mark
            # price (not our decision price) and adds a maintenance buffer, so
            # using the full equity*leverage as notional repeatedly hits -2019
            # "Margin is insufficient" mid-chaser. Reserve 3% for mark drift +
            # maintenance margin so all four chaser stages can place freely.
            notional_pct = get_notional_pct(account_id)
            notional_usd = max(100.0, usdt_total * leverage * notional_pct)
            qty = self._qty_from_usd(notional_usd, decision_price, account_id)

            min_qty = float(self.market.get("limits", {}).get("amount", {}).get("min") or 0)
            if min_qty and qty < min_qty:
                logger.warning(f"[{account_id}] qty {qty} < minQty {min_qty}; aborting")
                STATE.record_trade_event({
                    "event": "trade_entry_aborted",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "side": side, "reason": "below_min_qty",
                    "qty": qty, "min_qty": min_qty,
                    "strategy": strategy,
                    "signal_fire_ts": signal_fire_ts,
                    "signal_to_attempt_ms": signal_to_attempt_ms,
                }, account_id=account_id)
                return

            logger.info(
                f"[{account_id}] Executing {side.upper()} adaptive entry: qty={qty} BTC  "
                f"decision_bid/ask={bid0}/{ask0}  notional≈${notional_usd:.2f}  "
                f"sl={sl_pct*100:.2f}% tp={tp_pct*100:.2f}%"
            )
            STATE.record_trade_event({
                "event": "trade_entry_attempt",
                "ts": attempt_started_at.isoformat(),
                "side": side, "price": decision_price, "qty": qty,
                "sl_pct": sl_pct, "tp_pct": tp_pct,
                "decision_bid": bid0, "decision_ask": ask0,
                "strategy": strategy,
                "signal_fire_ts": signal_fire_ts,
                "signal_to_attempt_ms": signal_to_attempt_ms,
            }, account_id=account_id)
            _send_alert(f"ENTRY ATTEMPT [{account_id}] {side.upper()}",
                        {"price": decision_price, "qty": qty,
                         "sl_pct": sl_pct, "tp_pct": tp_pct, "account_id": account_id})

            filled = False
            entry_method = None
            entry_price = None
            stages: list[dict] = []

            def _run_stage(stage_num: int, name: str, place_fn, deadline_s: float) -> tuple[bool, dict | None, dict]:
                """Run one chaser stage. Returns (filled, fill_status, stage_record)."""
                started = datetime.now(timezone.utc)
                t_place = time.monotonic()
                rec: dict = {
                    "stage": stage_num, "name": name,
                    "started_at": started.isoformat(),
                    "deadline_s": deadline_s,
                }
                try:
                    order_obj, intended_price, tif = place_fn()
                    rec["intended_price"] = intended_price
                    rec["tif"] = tif
                    rec["order_id"] = order_obj.get("id") if isinstance(order_obj, dict) else None
                    if name == "market_failsafe":
                        time.sleep(1)
                        self._refresh_position(account_id)
                        rec["duration_ms"] = round((time.monotonic() - t_place) * 1000, 1)
                        if st.position != "flat":
                            rec["outcome"] = "filled"
                            rec["fill_price"] = st.entry
                            return True, None, rec
                        rec["outcome"] = "unfilled"
                        return False, None, rec
                    f, status = self._wait_for_fill(order_obj["id"], deadline_s, account_id)
                    rec["duration_ms"] = round((time.monotonic() - t_place) * 1000, 1)
                    if f:
                        fp = float(status.get("average") or status.get("price") or intended_price)
                        rec["outcome"] = "filled"
                        rec["fill_price"] = fp
                        return True, status, rec
                    rec["outcome"] = "unfilled"
                    try:
                        ex.cancel_order(order_obj["id"], self.symbol)
                        rec["cancelled"] = True
                    except Exception:
                        rec["cancelled"] = False
                    return False, None, rec
                except Exception as e:
                    rec["duration_ms"] = round((time.monotonic() - t_place) * 1000, 1)
                    rec["outcome"] = "error"
                    rec["error"] = str(e)
                    return False, None, rec

            # ---------- Stage 1: GTX (post-only) at bid — maker rebate ----------
            def _place_stage1():
                bid, ask, _ = self._refresh_quote(side, account_id)
                px = self._price_to_prec(bid if side == "buy" else ask, account_id)
                logger.info(f"[{account_id}]   Stage 1 (GTX maker) {side} @ ${px}")
                o = ex.create_order(
                    symbol=self.symbol, type="limit", side=side, amount=qty, price=px,
                    params={"timeInForce": "GTX"},
                )
                return o, px, "GTX"

            filled, status, rec = _run_stage(1, "gtx_maker", _place_stage1, ENTRY_STAGE1_POST_ONLY_S)
            stages.append(rec)
            if filled:
                entry_method = "stage1_gtx_maker"
                entry_price = rec.get("fill_price")

            # ---------- Stage 2: GTC limit at bid+1tick — inside spread, maker if filled ----------
            if not filled:
                def _place_stage2():
                    bid, ask, _ = self._refresh_quote(side, account_id)
                    tick = self.market.get("precision", {}).get("price", 0.1)
                    if not isinstance(tick, (int, float)) or tick <= 0:
                        tick = 0.1
                    px = self._price_to_prec(bid + tick, account_id) if side == "buy" else self._price_to_prec(ask - tick, account_id)
                    logger.info(f"[{account_id}]   Stage 2 (GTC inside-spread) {side} @ ${px}  (bid/ask={bid}/{ask})")
                    o = ex.create_order(
                        symbol=self.symbol, type="limit", side=side, amount=qty, price=px,
                        params={"timeInForce": "GTC"},
                    )
                    return o, px, "GTC"

                filled, status, rec = _run_stage(2, "gtc_inside", _place_stage2, ENTRY_STAGE2_GTC_INSIDE_S)
                stages.append(rec)
                if filled:
                    entry_method = "stage2_gtc_inside"
                    entry_price = rec.get("fill_price")

            # ---------- Stage 3: IOC limit at ask + 3bps — guaranteed taker fill ----------
            if not filled:
                def _place_stage3():
                    bid, ask, _ = self._refresh_quote(side, account_id)
                    slip = ENTRY_STAGE3_SLIP_BPS / 10000.0
                    px = self._price_to_prec(ask * (1 + slip), account_id) if side == "buy" else self._price_to_prec(bid * (1 - slip), account_id)
                    logger.info(f"[{account_id}]   Stage 3 (IOC taker) {side} @ ${px}  ask={ask} bid={bid}")
                    o = ex.create_order(
                        symbol=self.symbol, type="limit", side=side, amount=qty, price=px,
                        params={"timeInForce": "IOC"},
                    )
                    return o, px, "IOC"

                filled, status, rec = _run_stage(3, "ioc_taker", _place_stage3, ENTRY_STAGE3_IOC_TAKE_S)
                stages.append(rec)
                if filled:
                    entry_method = "stage3_ioc_taker"
                    entry_price = rec.get("fill_price")

            # ---------- Stage 4: MARKET failsafe with priceProtect ----------
            if not filled:
                def _place_stage4():
                    logger.info(f"[{account_id}]   Stage 4 (MARKET failsafe) {side} qty={qty}")
                    o = ex.create_order(
                        symbol=self.symbol, type="market", side=side, amount=qty,
                        params={"priceProtect": True},
                    )
                    return o, None, "MARKET"

                filled, _status, rec = _run_stage(4, "market_failsafe", _place_stage4, 0.0)
                stages.append(rec)
                if filled:
                    entry_method = "stage4_market"
                    entry_price = rec.get("fill_price") or self._refresh_quote(side, account_id)[0]

            total_ms = round((time.monotonic() - t_attempt) * 1000, 1)

            # Defensive post-chaser sync: a stage's order can fill async after
            # our wait deadline expires. Before declaring aborted, refresh the
            # actual position from the exchange — if we're not flat, treat it
            # as a successful entry and recover state.
            if not filled:
                try:
                    self._refresh_position(account_id)
                    if st.position != "flat" and st.qty:
                        logger.warning(
                            f"[{account_id}] chaser reported all-stages-failed but exchange shows "
                            f"position={st.position} qty={st.qty} "
                            f"entry={st.entry} — recovering as filled"
                        )
                        filled = True
                        entry_method = "post_chaser_recovery"
                        entry_price = st.entry
                except Exception as e:
                    logger.warning(f"[{account_id}] post-chaser position refresh failed: {e}")

            if not filled:
                logger.info(f"[{account_id}] All 4 entry stages failed. Aborting trade.")
                try:
                    ex.cancel_all_orders(self.symbol)
                except Exception:
                    pass
                STATE.record_trade_event({
                    "event": "trade_entry_aborted",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "side": side, "reason": "chaser_unfilled",
                    "decision_price": decision_price, "qty": qty,
                    "strategy": strategy,
                    "stages": stages,
                    "total_ms": total_ms,
                    "signal_fire_ts": signal_fire_ts,
                    "signal_to_attempt_ms": signal_to_attempt_ms,
                }, account_id=account_id)
                return

            # Final entry price (real fill price, may differ from decision_price by slippage)
            assert entry_price is not None, "filled=True but entry_price unset"
            slippage_bps = ((entry_price - decision_price) / decision_price) * 10000.0
            if side == "sell":
                slippage_bps = -slippage_bps
            self._log_trade_csv(side, entry_price, qty, sl_pct, tp_pct, strategy, account_id)

            close_side = "sell" if side == "buy" else "buy"
            tp_price = self._price_to_prec(
                entry_price * (1 + tp_pct) if side == "buy" else entry_price * (1 - tp_pct),
                account_id,
            )
            sl_price = self._price_to_prec(
                entry_price * (1 - sl_pct) if side == "buy" else entry_price * (1 + sl_pct),
                account_id,
            )
            try:
                ex.create_order(
                    symbol=self.symbol, type="limit", side=close_side, amount=qty, price=tp_price,
                    params={"reduceOnly": True},
                )
            except Exception as e:
                logger.error(f"[{account_id}] TP placement failed: {e}")
            try:
                ex.create_order(
                    symbol=self.symbol, type="STOP_MARKET", side=close_side, amount=qty, price=None,
                    params={"stopPrice": sl_price, "reduceOnly": True},
                )
            except Exception as e:
                logger.error(f"[{account_id}] SL placement failed: {e}")

            logger.info(
                f"[{account_id}] FILLED via {entry_method} @ ${entry_price}  "
                f"slip={slippage_bps:+.1f}bps  TP=${tp_price}  SL=${sl_price}"
            )
            STATE.update_state(
                account_id=account_id,
                position="long" if side == "buy" else "short",
                entry=entry_price, sl=sl_price, tp=tp_price,
                opened_bar=self.engine.bar_idx, bars_held=0,
                trades_today=st.trades_today + 1,
                strategy=strategy,
                # Combo-level hold-timeout (resolved to 5m bars by the caller).
                # When None the bot's tick falls back to global MAX_HOLD_BARS.
                max_hold_bars_5m=max_hold_bars_5m,
            )
            filled_at = datetime.now(timezone.utc)
            signal_to_fill_ms = _ms_between(signal_fire_ts, filled_at)
            STATE.record_trade_event({
                "event": "trade_filled",
                "ts": filled_at.isoformat(),
                "side": side, "entry": entry_price, "qty": qty,
                "tp": tp_price, "sl": sl_price,
                "entry_method": entry_method,
                "slippage_bps": round(slippage_bps, 2),
                "strategy": strategy,
                "stages": stages,
                "total_ms": total_ms,
                "signal_fire_ts": signal_fire_ts,
                "signal_to_attempt_ms": signal_to_attempt_ms,
                "signal_to_fill_ms": signal_to_fill_ms,
            }, account_id=account_id)
        except Exception as e:
            logger.error(f"[{account_id}] Trade execution failed: {e}", exc_info=True)
            _send_alert(f"TRADE FAILED [{account_id}]", {"error": str(e), "account_id": account_id})

    def close_position(self, reason: str = "AGENT_CLOSE",
                       account_id: str = DEFAULT_ACCOUNT_ID) -> None:
        ex = self.exchanges[account_id]
        st = STATE.account_state(account_id)
        if st.position == "flat":
            return
        self._cancel_all(account_id)
        try:
            positions = ex.fetch_positions([self.symbol])
            for p in positions:
                contracts = float(p.get("contracts") or 0)
                if contracts == 0:
                    continue
                close_side = "sell" if p.get("side") == "long" else "buy"
                ex.create_order(
                    symbol=self.symbol, type="market", side=close_side, amount=contracts, price=None,
                    params={"reduceOnly": True},
                )
                logger.info(f"[{account_id}] Closed {st.position} position ({reason}).")
                STATE.record_trade_event({
                    "event": "trade_close",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "side": st.position, "reason": reason,
                }, account_id=account_id)
                _send_alert(f"CLOSE [{account_id}] ({reason})",
                            {"side": st.position, "account_id": account_id})
            STATE.update_state(account_id=account_id, position="flat", entry=None,
                               strategy=None, max_hold_bars_5m=None,
                               sl=None, tp=None, opened_bar=None, bars_held=0)
        except Exception as e:
            logger.error(f"[{account_id}] close_position failed: {e}", exc_info=True)

    def close_position_limit(
        self,
        max_attempts: int = 3,
        wait_seconds: float = 5.0,
        offset_ticks: int = 0,
        fallback_market: bool = False,
        reason: str = "MANUAL_LIMIT",
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict:
        """Close current position with postOnly LIMIT (maker), retrying up to
        `max_attempts` times. Existing SL/TP reduceOnly orders are NOT touched —
        our reduceOnly limit races against them on the same position. If
        `fallback_market` and all maker attempts whiff, fall through to a
        market close via `close_position`. Returns a structured result.
        """
        ex = self.exchanges[account_id]
        st = STATE.account_state(account_id)
        if st.position == "flat":
            return {"closed": False, "reason": "no open position", "attempts_used": 0,
                    "attempts": []}

        pos_side = st.position  # 'long' or 'short'
        close_side = "sell" if pos_side == "long" else "buy"

        tick = self.market.get("precision", {}).get("price", 0.1)
        if not isinstance(tick, (int, float)) or tick <= 0:
            tick = 0.1

        attempts: list[dict] = []
        for i in range(max_attempts):
            try:
                positions = ex.fetch_positions([self.symbol])
            except Exception as e:
                logger.error(f"[{account_id}] close_position_limit fetch_positions failed: {e}")
                attempts.append({"i": i + 1, "outcome": "fetch_positions_error", "error": str(e)})
                break

            contracts = 0.0
            for p in positions:
                c = float(p.get("contracts") or 0)
                if c > 0:
                    contracts = c
                    break

            if contracts == 0:
                # something else closed it (TP/SL hit between attempts)
                STATE.update_state(account_id=account_id, position="flat", entry=None,
                                   strategy=None, max_hold_bars_5m=None,
                                   sl=None, tp=None, opened_bar=None, bars_held=0)
                STATE.record_trade_event({
                    "event": "trade_close",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "side": pos_side, "reason": "external_during_limit_attempts",
                }, account_id=account_id)
                return {"closed": True, "method": "external_during_attempts",
                        "attempts_used": i, "attempts": attempts}

            try:
                bid, ask, _ = self._refresh_quote(close_side, account_id)
            except Exception as e:
                attempts.append({"i": i + 1, "outcome": "quote_error", "error": str(e)})
                logger.warning(f"[{account_id}]  close-limit attempt {i+1}: quote failed: {e}")
                continue

            # postOnly safety: stay strictly on the resting side of the book.
            # Closing a short → we are buying → price must be ≤ best bid.
            # Closing a long  → we are selling → price must be ≥ best ask.
            if close_side == "buy":
                px = self._price_to_prec(bid - offset_ticks * tick, account_id)
            else:
                px = self._price_to_prec(ask + offset_ticks * tick, account_id)

            attempt_rec: dict = {"i": i + 1, "price": px, "qty": contracts,
                                 "bid": bid, "ask": ask, "outcome": None}

            try:
                logger.info(
                    f"[{account_id}]  close-limit attempt {i+1}/{max_attempts}: {close_side} qty={contracts} "
                    f"@ ${px} (GTX postOnly reduceOnly, bid/ask={bid}/{ask})"
                )
                o = ex.create_order(
                    symbol=self.symbol, type="limit", side=close_side, amount=contracts, price=px,
                    params={"timeInForce": "GTX", "reduceOnly": True},
                )
            except Exception as e:
                attempt_rec["outcome"] = "place_error"
                attempt_rec["error"] = str(e)
                attempts.append(attempt_rec)
                logger.warning(f"[{account_id}]  close-limit place failed: {e}")
                continue

            order_id = o.get("id")
            filled, status = self._wait_for_fill(order_id, wait_seconds, account_id)

            if filled:
                fill_price = float((status or {}).get("average") or px)
                attempt_rec["outcome"] = "filled"
                attempt_rec["fill_price"] = fill_price
                attempts.append(attempt_rec)
                logger.info(f"[{account_id}] Closed {pos_side} via maker limit ({reason}) on attempt {i+1}.")
                STATE.record_trade_event({
                    "event": "trade_close",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "side": pos_side, "reason": reason,
                    "method": "limit_postOnly",
                    "fee_type": "maker",
                    "attempts": i + 1,
                    "fill_price": fill_price,
                }, account_id=account_id)
                _send_alert(f"CLOSE [{account_id}] ({reason})",
                            {"side": pos_side, "method": "maker_limit", "attempts": i + 1,
                             "fill_price": fill_price, "account_id": account_id})
                STATE.update_state(account_id=account_id, position="flat", entry=None,
                                   strategy=None, max_hold_bars_5m=None,
                                   sl=None, tp=None, opened_bar=None, bars_held=0)
                return {"closed": True, "method": "limit_postOnly",
                        "attempts_used": i + 1, "fill_price": fill_price,
                        "attempts": attempts}

            # not filled within wait_seconds → cancel by id
            try:
                ex.cancel_order(order_id, self.symbol)
                attempt_rec["outcome"] = "unfilled_cancelled"
            except Exception as e:
                attempt_rec["outcome"] = "unfilled_cancel_error"
                attempt_rec["error"] = str(e)
            attempts.append(attempt_rec)

        if fallback_market:
            logger.info(f"[{account_id}] close-limit: {max_attempts} maker attempts whiffed → market fallback.")
            self.close_position(reason + "_FALLBACK", account_id=account_id)
            return {"closed": st.position == "flat",
                    "method": "market_fallback",
                    "attempts_used": max_attempts, "attempts": attempts}

        return {"closed": False, "method": "limit_postOnly_exhausted",
                "attempts_used": max_attempts, "attempts": attempts}

    # ---- main loop ----

    def _tick(self) -> None:  # noqa: C901
        self._rollover_day_if_needed()

        # Per-account: refresh equity + position (light), check hold timeout
        for aid in self.account_ids:
            self._refresh_equity(aid)
            self._refresh_position(aid)
            st = STATE.account_state(aid)
            if st.position != "flat" and st.opened_bar is not None:
                STATE.update_state(account_id=aid,
                                   bars_held=self.engine.bar_idx - st.opened_bar)
                # Per-combo hold-timeout override (5m bars). The firing combo
                # set `max_hold_bars_5m` on the BotState at entry-time (from its
                # decide() response). When None, fall back to global default.
                effective_max = st.max_hold_bars_5m or MAX_HOLD_BARS
                if st.bars_held >= effective_max:
                    logger.info(f"[{aid}] Hold timeout ({st.bars_held}/{effective_max} 5m bars, "
                                f"strategy={st.strategy or 'unknown'}) — closing.")
                    self.close_position("TIMEOUT", account_id=aid)

        # Single shared engine call — both accounts trade the same symbol
        # on the same 5m feed.
        self.engine.catch_up()
        packet, df_ind = self.engine.build_packet(STATE.state.as_strategy_dict())

        # Live exchange-side metrics (orderbook, flow, funding) for any
        # strategy that needs more than just OHLCV+TA. Strategies opt in by
        # setting `needs_exchange_metrics=True` on their StrategyMeta.
        exchange_metrics = {
            "imb1pct":     packet["orderbook"].get("imb1pct"),
            "imb5pct":     packet["orderbook"].get("imb5pct"),
            "smart_skew":  packet["flow"].get("smart_skew"),
            "taker_ratio": packet["flow"].get("taker_ratio"),
            "oi_5m":       packet["flow"].get("oi_5m"),
            "oi_1h":       packet["flow"].get("oi_1h"),
            "top_pos":     packet["flow"].get("top_pos"),
            "fund_rate":   packet["funding"].get("rate"),
            "ret_6":       packet["returns"].get("ret_6"),
            "ret_12":      packet["returns"].get("ret_12"),
        }

        def _call(strategy_meta, df):
            """Call decide() with exchange_metrics if the strategy needs it."""
            if getattr(strategy_meta, "needs_exchange_metrics", False):
                return strategy_meta.decide(df, exchange_metrics)
            return strategy_meta.decide(df)

        # Snapshot indicator + decision once (account-agnostic — these are
        # observation logs, not trade actions). Indicator snapshots and
        # decision records use account1's state for backwards-compat with
        # /indicators and /decisions endpoint shapes.
        snap = {
            "ts": packet["ts"], "price": packet["price"]["c"],
            "trend": packet["trend"], "momentum": packet["momentum"],
            "volatility": packet.get("volatility"), "volume": packet.get("volume"),
            "orderbook": packet["orderbook"], "flow": packet["flow"],
            "funding": packet["funding"],
        }
        STATE.record_indicator_snapshot(snap)
        self._indicators_fp.write(json.dumps(snap) + "\n")
        self._indicators_fp.flush()

        # ---- Per-account dispatch ----
        # Walk the active registry in priority order, filtered to each
        # account's allowed strategy set. First non-HOLD wins. If the
        # account's slot is free and gating passes, fire the trade on
        # THAT account's exchange client.
        signal_fire_ts = datetime.now(timezone.utc).isoformat()
        any_action_for_log = "HOLD"
        any_why_for_log = ""

        for aid in self.account_ids:
            st = STATE.account_state(aid)

            # Walk active strategies in registry priority order, filter by
            # this account's permitted set. First non-HOLD wins.
            active_resp = {"action": "HOLD"}
            engine_name = "active_stack"
            why_str = ""
            for strat in get_active():
                if not is_strategy_allowed(aid, strat.name):
                    continue
                try:
                    resp = _call(strat, df_ind)
                except Exception as e:
                    logger.error(f"[{aid}] strategy {strat.name} raised: {e}", exc_info=True)
                    continue
                if resp.get("action") not in (None, "HOLD"):
                    active_resp = resp
                    why_str = resp.get("why", "") or ""
                    engine_name = engine_from_why(why_str) or strat.name
                    break

            logger.info(f"[{aid}] [active] {engine_name}: {active_resp.get('action', 'HOLD')} "
                        f"| {why_str or 'No confluence'}")

            if active_resp.get("action") not in (None, "HOLD"):
                # `ts`     — wall-clock UTC time the signal actually fired (what humans expect)
                # `bar_ts` — open_time of the latest closed 5m bar the strategy evaluated
                #            against (kept for backtest replay alignment in
                #            /per-strategy-stats etc — those endpoints look up
                #            kline rows by bar_ts)
                hit = {
                    "ts": signal_fire_ts,
                    "bar_ts": packet["ts"],
                    "engine": engine_name, "tier": "active",
                    "account_id": aid,
                    "action": active_resp.get("action"), "why": why_str,
                    "fire_ts": signal_fire_ts,
                }
                STATE.record_signal_hit(hit)
                self._signal_hits_fp.write(json.dumps(hit) + "\n")
                self._signal_hits_fp.flush()
                any_action_for_log = active_resp.get("action")
                any_why_for_log = why_str

            action = (active_resp.get("action") or "HOLD").upper()
            why = active_resp.get("why", "")
            sl_pct = float(active_resp.get("sl_pct", 0.005))
            tp_pct = float(active_resp.get("tp_pct", 0.005))

            if st.position != "flat":
                action = "HOLD"
                why = f"[{aid}] in position; harness manages TP/SL/timeout"

            STATE.update_state(account_id=aid,
                               last_tick_ts=packet["ts"],
                               last_tick_price=packet["price"]["c"],
                               last_action=action, last_why=why)

            # Trade execution per account
            if action in ("OPEN_LONG", "OPEN_SHORT"):
                if st.position != "flat":
                    logger.info(f"[{aid}] signal ignored — already in position")
                elif st.trades_today >= MAX_DAILY_TRADES:
                    logger.info(f"[{aid}] signal ignored — daily trade cap hit")
                elif st.cooldown_active:
                    logger.info(f"[{aid}] signal ignored — cooldown active")
                else:
                    # Resolve any combo-level max_hold_bars override (in 5m bars)
                    combo_max_hold_5m = resolve_combo_max_hold_5m(active_resp)
                    self.execute_trade(
                        "buy" if action == "OPEN_LONG" else "sell",
                        sl_pct, tp_pct, signal_fire_ts=signal_fire_ts,
                        strategy=engine_name, account_id=aid,
                        max_hold_bars_5m=combo_max_hold_5m,
                    )
            elif action == "CLOSE":
                self.close_position("AGENT_CLOSE", account_id=aid)

        # Decision log — record once per tick (account-agnostic snapshot of
        # the bar's market state). Use whatever the last meaningful action was.
        record_resp = {"action": any_action_for_log, "why": any_why_for_log,
                       "sl_pct": 0.005, "tp_pct": 0.005}
        t = packet["trend"]; m = packet["momentum"]; ob = packet["orderbook"]
        logger.info(
            "bar=%s ts=%s price=%.2f any_action=%s | supert=%s adx=%s rsi=%s macdh=%s imb1=%s imb5=%s | %s",
            packet["bar"], packet["ts"], packet["price"]["c"], any_action_for_log,
            t.get("supert_dir"),
            f"{t.get('adx'):.1f}" if t.get("adx") is not None else None,
            f"{m.get('rsi14'):.1f}" if m.get("rsi14") is not None else None,
            f"{m.get('macdh'):.3f}" if m.get("macdh") is not None else None,
            f"{ob.get('imb1pct'):+.3f}" if ob.get("imb1pct") is not None else None,
            f"{ob.get('imb5pct'):+.3f}" if ob.get("imb5pct") is not None else None,
            any_why_for_log if any_why_for_log else "no confluence",
        )
        self._record_decision(packet, record_resp)

        # ---- Monitoring strategies last — account-agnostic, never trade ----
        for s in get_monitoring():
            try:
                resp = _call(s, df_ind)
            except Exception as e:
                logger.warning(f"monitoring strategy {s.name} raised: {e}")
                resp = {"action": "HOLD", "why": f"error: {e}"}
            logger.info(f"[monitor] {s.name}: {resp.get('action', 'HOLD')} | {resp.get('why', 'no signal')}")
            if resp.get("action") not in (None, "HOLD"):
                mon_hit = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "bar_ts": packet["ts"],
                    "engine": s.name, "tier": "monitoring",
                    "action": resp.get("action"), "why": resp.get("why"),
                }
                STATE.record_signal_hit(mon_hit)
                self._signal_hits_fp.write(json.dumps(mon_hit) + "\n")
                self._signal_hits_fp.flush()

        STATE.heartbeat()

    def run(self) -> None:
        _send_alert("BOT STARTED", {"status": "TradingBot online"})
        logger.info("Bot is running. Initialising state with a startup tick...")
        try:
            self._tick()
        except Exception as e:
            logger.error(f"Startup tick failed: {e}", exc_info=True)

        last_fire_minute: int | None = None
        while True:
            try:
                now = datetime.now(timezone.utc)
                if now.minute % 5 == 0 and 5 <= now.second < 15 and now.minute != last_fire_minute:
                    self._tick()
                    last_fire_minute = now.minute
                    time.sleep(50)
                else:
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Bot manually stopped.")
                break
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
                time.sleep(10)


_BOT_THREAD: threading.Thread | None = None
_BOT_INSTANCE: TradingBot | None = None


def start_bot_thread() -> TradingBot:
    """Start the bot tick loop in a background daemon thread (used by FastAPI)."""
    global _BOT_THREAD, _BOT_INSTANCE
    if _BOT_THREAD is not None and _BOT_THREAD.is_alive():
        return _BOT_INSTANCE
    _BOT_INSTANCE = TradingBot()
    _BOT_THREAD = threading.Thread(target=_BOT_INSTANCE.run, name="trading-bot-tick",
                                   daemon=True)
    _BOT_THREAD.start()
    return _BOT_INSTANCE


def get_bot_instance() -> TradingBot | None:
    return _BOT_INSTANCE


if __name__ == "__main__":
    configure_logging(LOG_DIR)
    TradingBot().run()
