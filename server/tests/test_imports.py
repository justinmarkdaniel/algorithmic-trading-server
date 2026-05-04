"""Smoke test — every package surface imports cleanly."""


def test_engine_imports():
    from server.engine import LiveEngine, EngineConfig
    cfg = EngineConfig(warmup_bars=100, initial_bar_idx=10)
    assert cfg.warmup_bars == 100
    assert LiveEngine is not None


def test_strategies_import():
    from server.strategies import (
        REGISTRY, get_active, get_live_decider,
        decide_macd_crossover, decide_rsi_meanreversion,
    )
    assert "macd_crossover" in REGISTRY
    assert "rsi_meanreversion" in REGISTRY
    assert callable(decide_macd_crossover)
    assert callable(decide_rsi_meanreversion)
    assert callable(get_live_decider())
    assert all(s.tier == "active" for s in get_active())


def test_state_store():
    from server.bot.state_store import STATE
    h = STATE.get_health()
    assert "status" in h


def test_api_app_import():
    from server.api.app import app
    assert app.title == "TradingBot API"


def test_exchange_factory_returns_stub():
    from server.exchange import get_client
    c = get_client()
    assert c.id == "stub"
    markets = c.load_markets()
    assert markets   # at least one placeholder symbol
