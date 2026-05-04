"""Tick-order invariants and helper tests for `server.bot.main`.

We don't run the full `_tick` (it needs a live exchange connection); instead
we verify the structural invariant from source — active+trade evaluation must
happen before the monitoring loop — and unit-test the helpers around timing
and per-strategy hold-timeout overrides.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

from server.bot.main import _ms_between


def _tick_source() -> str:
    p = Path(__file__).resolve().parents[1] / "bot" / "main.py"
    return p.read_text()


def test_active_runs_before_monitoring_in_tick() -> None:
    src = _tick_source()
    tick_start = src.index("def _tick(self)")
    next_def = src.index("\n    def ", tick_start + 1)
    body = src[tick_start:next_def]

    active_call = body.index("for strat in get_active()")
    execute_trade = body.index("self.execute_trade(")
    monitoring_loop = body.index("for s in get_monitoring()")

    assert active_call < execute_trade < monitoring_loop, (
        "tick ordering violated: expected active → execute_trade → monitoring loop"
    )


def test_heartbeat_is_last_in_tick() -> None:
    src = _tick_source()
    tick_start = src.index("def _tick(self)")
    next_def = src.index("\n    def ", tick_start + 1)
    body = src[tick_start:next_def]

    heartbeat = body.rindex("STATE.heartbeat()")
    monitoring_loop = body.index("for s in get_monitoring()")
    assert monitoring_loop < heartbeat, (
        "heartbeat() should run after monitoring strategies have been evaluated"
    )


def test_ms_between_basic() -> None:
    start = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(milliseconds=1234)
    assert _ms_between(start.isoformat(), end) == 1234.0


def test_ms_between_naive_start_treated_as_utc() -> None:
    end = datetime(2026, 4, 27, 12, 0, 0, 500_000, tzinfo=timezone.utc)
    assert _ms_between("2026-04-27T12:00:00", end) == 500.0


def test_ms_between_handles_missing_or_invalid() -> None:
    end = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    assert _ms_between(None, end) is None
    assert _ms_between("not-a-timestamp", end) is None


def test_execute_trade_signature_accepts_signal_fire_ts() -> None:
    import inspect
    from server.bot.main import TradingBot

    sig = inspect.signature(TradingBot.execute_trade)
    assert "signal_fire_ts" in sig.parameters
    assert sig.parameters["signal_fire_ts"].default is None


def test_resolve_combo_max_hold_5m_handles_5m_tf() -> None:
    from server.bot.main import resolve_combo_max_hold_5m

    assert resolve_combo_max_hold_5m({"max_hold_bars": 48,
                                      "max_hold_bars_tf": "5m"}) == 48
    assert resolve_combo_max_hold_5m({"max_hold_bars": 12,
                                      "max_hold_bars_tf": "1h"}) == 144
    assert resolve_combo_max_hold_5m({"max_hold_bars": 36}) == 36


def test_resolve_combo_max_hold_5m_returns_none_when_not_declared() -> None:
    from server.bot.main import resolve_combo_max_hold_5m

    assert resolve_combo_max_hold_5m({"action": "OPEN_LONG", "sl_pct": 0.005}) is None
    assert resolve_combo_max_hold_5m({}) is None
    assert resolve_combo_max_hold_5m(None) is None
