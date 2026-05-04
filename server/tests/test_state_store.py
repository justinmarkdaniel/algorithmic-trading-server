"""Hydration tests — restart-survival of /trade-history and /decisions."""
from __future__ import annotations

import json
from pathlib import Path

from server.bot.state_store import StateStore


def test_hydrate_trades_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "timestamp,side,intended_price,qty,sl_pct,tp_pct,strategy,venue\n"
        "2026-04-26T20:35:06.882555+00:00,buy,78167.8,0.0178,0.005,0.005,macd_crossover,exchange\n"
        "2026-04-26T21:55:07.878449+00:00,buy,78208.5,0.0178,0.005,0.005,rsi_meanreversion,exchange\n"
    )
    store = StateStore()
    result = store.hydrate_from_disk(tmp_path)

    assert result["trades_loaded"] == 2
    trades = store.get_recent_trades(10)
    assert len(trades) == 2
    assert trades[0]["side"] == "buy"
    assert trades[0]["price"] == 78167.8
    assert trades[0]["qty"] == 0.0178
    assert trades[0]["source"] == "hydrated"
    assert trades[1]["ts"] == "2026-04-26T21:55:07.878449+00:00"


def test_hydrate_decisions_jsonl(tmp_path: Path) -> None:
    decisions_path = tmp_path / "decisions.jsonl"
    rows = [
        {"ts": "2026-04-27T11:35:00", "action": "HOLD", "price": 77754.3, "why": ""},
        {"ts": "2026-04-27T11:40:00", "action": "HOLD", "price": 77768.0, "why": ""},
    ]
    decisions_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    store = StateStore()
    result = store.hydrate_from_disk(tmp_path)

    assert result["decisions_loaded"] == 2
    decisions = store.get_recent_decisions(10)
    assert len(decisions) == 2
    assert decisions[0]["action"] == "HOLD"
    assert decisions[1]["price"] == 77768.0


def test_hydrate_signal_hits_jsonl(tmp_path: Path) -> None:
    signals_path = tmp_path / "signal_hits.jsonl"
    rows = [
        {"ts": "2026-04-27T15:00:00", "engine": "macd_crossover", "tier": "active",
         "action": "OPEN_SHORT", "why": "MACD bear cross"},
        {"ts": "2026-04-27T16:00:00", "engine": "rsi_meanreversion", "tier": "active",
         "action": "OPEN_LONG", "why": "RSI exit oversold"},
    ]
    signals_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    store = StateStore()
    result = store.hydrate_from_disk(tmp_path)

    assert result["signal_hits_loaded"] == 2
    sigs = store.get_signal_hits(10)
    assert len(sigs) == 2
    assert sigs[0]["engine"] == "macd_crossover"
    assert sigs[1]["tier"] == "active"


def test_hydrate_indicators_jsonl(tmp_path: Path) -> None:
    ind_path = tmp_path / "indicators.jsonl"
    rows = [
        {"ts": "2026-04-27T15:00:00", "price": 78100.0,
         "trend": {"adx": 25.0}, "orderbook": {"imb1pct": -0.04}},
        {"ts": "2026-04-27T15:05:00", "price": 78150.0,
         "trend": {"adx": 26.0}, "orderbook": {"imb1pct": -0.05}},
    ]
    ind_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    store = StateStore()
    result = store.hydrate_from_disk(tmp_path)

    assert result["indicators_loaded"] == 2
    inds = store.get_recent_indicators(10)
    assert len(inds) == 2
    assert inds[0]["price"] == 78100.0
    assert inds[1]["trend"]["adx"] == 26.0


def test_hydrate_missing_files_is_noop(tmp_path: Path) -> None:
    store = StateStore()
    result = store.hydrate_from_disk(tmp_path)
    assert result == {"trades_loaded": 0, "decisions_loaded": 0,
                      "signal_hits_loaded": 0, "indicators_loaded": 0,
                      "model_predictions_loaded": 0}
    assert store.get_recent_trades(10) == []
    assert store.get_recent_decisions(10) == []
    assert store.get_signal_hits(10) == []
    assert store.get_recent_indicators(10) == []
    assert store.get_model_predictions(10) == []


def test_hydrate_respects_ring_buffer_cap(tmp_path: Path) -> None:
    decisions_path = tmp_path / "decisions.jsonl"
    rows = [{"ts": f"t{i}", "action": "HOLD", "price": float(i)} for i in range(50)]
    decisions_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    store = StateStore(max_decisions=10)
    result = store.hydrate_from_disk(tmp_path)

    assert result["decisions_loaded"] == 10
    decisions = store.get_recent_decisions(20)
    assert len(decisions) == 10
    assert decisions[0]["price"] == 40.0
    assert decisions[-1]["price"] == 49.0


def test_hydrate_skips_corrupt_jsonl_lines(tmp_path: Path) -> None:
    decisions_path = tmp_path / "decisions.jsonl"
    decisions_path.write_text(
        json.dumps({"ts": "ok1", "action": "HOLD"}) + "\n"
        + "this is not json\n"
        + json.dumps({"ts": "ok2", "action": "HOLD"}) + "\n"
    )
    store = StateStore()
    result = store.hydrate_from_disk(tmp_path)
    assert result["decisions_loaded"] == 2
