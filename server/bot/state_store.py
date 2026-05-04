"""Shared in-process state between the bot tick loop and the FastAPI app.

Both run in the same Python process (single Docker container, two threads).
The bot writes; the API reads. Append-only ring buffers keep memory bounded
without a database.
"""
from __future__ import annotations

import csv
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque

_logger = logging.getLogger("TradingBot.state_store")


@dataclass
class BotState:
    equity: float = 0.0
    position: str = "flat"          # 'flat' | 'long' | 'short'
    qty: float = 0.0
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    opened_bar: int | None = None
    bars_held: int = 0
    trades_today: int = 0
    current_day: str = ""
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None
    last_tick_ts: str | None = None
    last_tick_price: float | None = None
    last_action: str = "INIT"
    last_why: str = ""
    # Name of the strategy that opened the current position (for diagnostics
    # + the close-event log). Cleared on close.
    strategy: str | None = None
    # Resolved per-combo hold timeout in 5m bars. Set on entry from the
    # firing combo's decide() response (`max_hold_bars` + `max_hold_bars_tf`).
    # When None, the bot falls back to the global MAX_HOLD_BARS. Cleared on
    # close.
    max_hold_bars_5m: int | None = None

    @property
    def cooldown_active(self) -> bool:
        return self.cooldown_until is not None and datetime.now(timezone.utc) < self.cooldown_until

    def as_strategy_dict(self) -> dict:
        d = asdict(self)
        d["cooldown_active"] = self.cooldown_active
        d.pop("cooldown_until", None)
        d.pop("opened_bar", None)
        d.pop("current_day", None)
        d.pop("last_tick_ts", None)
        d.pop("last_tick_price", None)
        d.pop("last_action", None)
        d.pop("last_why", None)
        return d

    def as_api_dict(self) -> dict:
        d = asdict(self)
        d["cooldown_active"] = self.cooldown_active
        d["cooldown_until"] = self.cooldown_until.isoformat() if self.cooldown_until else None
        return d


DEFAULT_ACCOUNT_ID = "account1"


class StateStore:
    """Thread-safe in-memory store. All public methods take/release the lock.

    Multi-account aware:
      - State (position, entry, SL, TP, equity) is namespaced by account_id
        in `_account_states`, lazily created on first reference.
      - The legacy `STATE.state` attribute is preserved as a property that
        returns the default account's state — every existing single-account
        caller continues to work without changes.
      - Writer methods (`update_state`, `record_trade_event`, etc.) accept
        an optional `account_id` kwarg defaulting to 'account1'.
      - Open-orders, decisions, indicators, and signal-hits remain
        account-agnostic (they're observation logs, not position state).
        Trade events stamp `account_id` for downstream filtering.
    """

    def __init__(self, max_decisions: int = 2000, max_trades: int = 500,
                 max_indicators: int = 500):
        self._lock = threading.RLock()
        self._account_states: dict[str, BotState] = {}
        self._decisions: Deque[dict] = deque(maxlen=max_decisions)
        self._trades: Deque[dict] = deque(maxlen=max_trades)
        self._indicators: Deque[dict] = deque(maxlen=max_indicators)
        self._signal_hits: Deque[dict] = deque(maxlen=max_decisions)
        # Rolling buffer of model `predict()` outputs — one record per model
        # per closed bar, regardless of whether the prediction crossed any
        # decision threshold. Useful for offline tuning and threshold sweeps.
        self._model_predictions: Deque[dict] = deque(maxlen=2000)
        self._open_orders: dict[str, list[dict]] = {}
        self.boot_ts = datetime.now(timezone.utc).isoformat()
        self.last_heartbeat: str | None = None

    # --- account-state accessors ---

    def _get_or_create_account_state(self, account_id: str) -> BotState:
        with self._lock:
            if account_id not in self._account_states:
                self._account_states[account_id] = BotState()
            return self._account_states[account_id]

    def account_state(self, account_id: str = DEFAULT_ACCOUNT_ID) -> BotState:
        return self._get_or_create_account_state(account_id)

    @property
    def state(self) -> BotState:
        """Backwards-compat alias. Returns the default account's BotState.

        Existing single-account callers (`STATE.state.position`,
        `STATE.state.entry`, etc.) continue to work unchanged."""
        return self._get_or_create_account_state(DEFAULT_ACCOUNT_ID)

    def account_ids(self) -> list[str]:
        """All account_ids that have state initialised. Order = creation order."""
        with self._lock:
            return list(self._account_states.keys())

    # --- writers (called from bot tick) ---

    def record_decision(self, decision: dict) -> None:
        with self._lock:
            self._decisions.append(decision)

    def record_indicator_snapshot(self, snap: dict) -> None:
        with self._lock:
            self._indicators.append(snap)

    def record_trade_event(self, event: dict, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
        """Append a trade event. Stamps `account_id` if not already present."""
        with self._lock:
            stamped = dict(event)
            stamped.setdefault("account_id", account_id)
            self._trades.append(stamped)

    def record_signal_hit(self, hit: dict) -> None:
        with self._lock:
            self._signal_hits.append(hit)

    def record_model_prediction(self, pred: dict) -> None:
        """Append one model `predict()` output. Strategies that wrap an ML
        model are expected to call this on every closed bar regardless of
        whether the prediction crossed any decision threshold, so offline
        threshold-sweep / WR-vs-threshold tooling can read the full curve."""
        with self._lock:
            self._model_predictions.append(pred)

    def update_open_orders(self, orders: list[dict],
                           account_id: str = DEFAULT_ACCOUNT_ID) -> None:
        with self._lock:
            self._open_orders[account_id] = orders

    def heartbeat(self) -> None:
        with self._lock:
            self.last_heartbeat = datetime.now(timezone.utc).isoformat()

    def update_state(self, account_id: str = DEFAULT_ACCOUNT_ID, **kwargs: Any) -> None:
        """Update one or more BotState fields on the named account. The
        `account_id` kwarg is consumed and never set on the dataclass."""
        state = self._get_or_create_account_state(account_id)
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(state, k):
                    setattr(state, k, v)

    # --- readers (called from FastAPI) ---

    def get_state(self, account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
        with self._lock:
            return self._get_or_create_account_state(account_id).as_api_dict()

    def get_all_states(self) -> dict[str, dict]:
        """Returns {account_id: api_dict} across every initialised account."""
        with self._lock:
            return {aid: s.as_api_dict() for aid, s in self._account_states.items()}

    def get_recent_decisions(self, n: int = 200) -> list[dict]:
        with self._lock:
            return list(self._decisions)[-n:]

    def get_recent_indicators(self, n: int = 200) -> list[dict]:
        with self._lock:
            return list(self._indicators)[-n:]

    def get_recent_trades(self, n: int = 100) -> list[dict]:
        with self._lock:
            return list(self._trades)[-n:]

    def get_signal_hits(self, n: int = 200) -> list[dict]:
        with self._lock:
            return list(self._signal_hits)[-n:]

    def get_model_predictions(self, n: int = 200) -> list[dict]:
        with self._lock:
            return list(self._model_predictions)[-n:]

    def get_open_orders(self, account_id: str = DEFAULT_ACCOUNT_ID) -> list[dict]:
        with self._lock:
            return list(self._open_orders.get(account_id, []))

    def get_all_open_orders(self) -> dict[str, list[dict]]:
        with self._lock:
            return {aid: list(orders) for aid, orders in self._open_orders.items()}

    # --- boot hydration --------------------------------------------------
    # On-disk logs (trades.csv + the per-stream jsonls) persist across
    # container restarts via the host bind-mount. The in-memory ring buffers
    # don't, so a fresh boot would blind /trade-history etc. until new events
    # arrive. Replay the tail of those files at startup so the API survives
    # restarts. Read-only — never writes back.

    def hydrate_from_disk(self, log_dir: Path) -> dict:
        log_dir = Path(log_dir)
        n_trades = self._hydrate_trades_csv(log_dir / "trades.csv")
        n_decisions = self._hydrate_decisions_jsonl(log_dir / "decisions.jsonl")
        n_signals = self._hydrate_signal_hits_jsonl(log_dir / "signal_hits.jsonl")
        n_indicators = self._hydrate_indicators_jsonl(log_dir / "indicators.jsonl")
        n_preds = self._hydrate_model_predictions_jsonl(log_dir / "model_predictions.jsonl")
        result = {"trades_loaded": n_trades, "decisions_loaded": n_decisions,
                  "signal_hits_loaded": n_signals, "indicators_loaded": n_indicators,
                  "model_predictions_loaded": n_preds}
        _logger.info(f"State hydrated from disk: {result}")
        return result

    def _hydrate_trades_csv(self, path: Path) -> int:
        if not path.exists():
            return 0
        loaded = 0
        try:
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            with self._lock:
                cap = self._trades.maxlen or len(rows)
                for row in rows[-cap:]:
                    self._trades.append({
                        "event": "trade_entry",
                        "ts": row.get("timestamp"),
                        "side": row.get("side"),
                        "price": _to_float(row.get("intended_price")),
                        "qty": _to_float(row.get("qty_btc")),
                        "sl_pct": _to_float(row.get("sl_pct")),
                        "tp_pct": _to_float(row.get("tp_pct")),
                        "strategy": row.get("strategy"),
                        "venue": row.get("venue"),
                        "source": "hydrated",
                    })
                    loaded += 1
        except Exception as e:
            _logger.warning(f"hydrate trades.csv failed: {e}")
        return loaded

    def _hydrate_decisions_jsonl(self, path: Path) -> int:
        return self._hydrate_jsonl_into(path, self._decisions, "decisions")

    def _hydrate_signal_hits_jsonl(self, path: Path) -> int:
        return self._hydrate_jsonl_into(path, self._signal_hits, "signal_hits")

    def _hydrate_indicators_jsonl(self, path: Path) -> int:
        return self._hydrate_jsonl_into(path, self._indicators, "indicators")

    def _hydrate_model_predictions_jsonl(self, path: Path) -> int:
        return self._hydrate_jsonl_into(path, self._model_predictions, "model_predictions")

    def _hydrate_jsonl_into(self, path: Path, target: Deque[dict], label: str) -> int:
        """Replay tail of a jsonl file into a target deque (capped to deque maxlen)."""
        if not path.exists():
            return 0
        cap = target.maxlen or 2000
        loaded = 0
        try:
            tail: Deque[str] = deque(maxlen=cap)
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        tail.append(line)
            with self._lock:
                for line in tail:
                    try:
                        target.append(json.loads(line))
                        loaded += 1
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            _logger.warning(f"hydrate {label} jsonl failed: {e}")
        return loaded

    def get_health(self) -> dict:
        """Single-account-flavored health dict for backwards compat. The
        `current_position` field reflects account1 only. Multi-account
        callers should use `get_all_states()` to see every account's
        position."""
        with self._lock:
            account_positions = {aid: s.position
                                 for aid, s in self._account_states.items()}
            return {
                "status": "ok" if self.last_heartbeat else "warming",
                "boot_ts": self.boot_ts,
                "last_heartbeat": self.last_heartbeat,
                "n_decisions": len(self._decisions),
                "n_trades": len(self._trades),
                "n_indicators": len(self._indicators),
                "n_signal_hits": len(self._signal_hits),
                "current_position": self.state.position,
                "account_positions": account_positions,
            }


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Module-level singleton — both bot loop and FastAPI import this.
STATE = StateStore()
