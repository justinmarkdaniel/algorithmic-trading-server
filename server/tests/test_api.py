"""FastAPI endpoint contract tests — uses TestClient (no real exchange)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from server.api.app import app


def test_health():
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code in (200, 503)
        body = r.json()
        assert body["ok"] is True
        assert "status" in body["data"]


def test_root_lists_endpoints():
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        body = r.json()
        assert "/state" in body["data"]["endpoints"]


def test_state_endpoint():
    with TestClient(app) as c:
        r = c.get("/state")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        d = r.json()["data"]
        assert d["position"] in ("flat", "long", "short")


def test_decisions_pagination():
    with TestClient(app) as c:
        r = c.get("/decisions?n=5")
        assert r.status_code == 200
        assert r.json()["n"] == 5


def test_trade_close_no_position():
    with TestClient(app) as c:
        r = c.post("/trade/close")
        assert r.status_code in (503, 200)


def test_trade_open_validation():
    with TestClient(app) as c:
        r = c.post("/trade/open", json={"side": "invalid", "sl_pct": 0.005, "tp_pct": 0.005})
        assert r.status_code in (400, 503)


def test_trade_close_limit_no_bot():
    with TestClient(app) as c:
        r = c.post("/trade/close-limit",
                   json={"max_attempts": 3, "wait_seconds": 5.0,
                         "offset_ticks": 0, "fallback_market": False})
        assert r.status_code in (503, 200)


def test_trade_close_limit_validation():
    with TestClient(app) as c:
        r = c.post("/trade/close-limit", json={"max_attempts": 0})
        assert r.status_code == 422

        r = c.post("/trade/close-limit", json={"offset_ticks": -1})
        assert r.status_code == 422

        r = c.post("/trade/close-limit", json={"wait_seconds": 0.1})
        assert r.status_code == 422

        r = c.post("/trade/close-limit", json={})
        assert r.status_code in (200, 503)
