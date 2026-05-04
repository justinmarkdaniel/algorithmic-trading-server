# algorithmic-trading-server

A high-frequency trading server in Python. FastAPI for introspection and trade
management; a daemon-thread tick loop on the 5-minute boundary executes against
any number of strategies that conform to a single, narrow `decide()` contract.
Designed to run in a small container (~300 MB image) on a single ARM-class
instance, with strategy code, TA features, and ML-model inference all sharing
one process.

The repo is intentionally a working chassis: clean exchange abstraction, real
backtests against the same indicator pipeline the live bot uses, multi-account
dispatch, four-stage adaptive entry chaser — but with the actual production
strategies and models swapped for two minimal examples (a MACD crossover and
an RSI mean-reversion). Drop your own strategy files into
`server/strategies/active/` and wire your venue inside
`server/exchange/client.py`; the rest of the system picks them up automatically.

```
                        ┌──────────────────────────────────────────────┐
                        │   FastAPI process (single Python interpreter)│
                        │                                              │
                ┌───────┤  ┌────────────────┐    ┌─────────────────┐  │
   /health      │       │  │  HTTP routes   │    │  Bot tick loop  │  │
   /state       │  ──── │  │  (read-only +  │◀──▶│  (daemon thread,│  │
   /trade/...   │       │  │  trade mgmt)   │    │  wakes on 5m    │  │
   /backtest    │       │  └───────┬────────┘    │  boundary)      │  │
                │       │          │             └────────┬────────┘  │
                └───────┤          │                      │           │
                        │          │       ┌──────────────▼────────┐  │
                        │          │       │  Strategy registry    │  │
                        │          │       │  (active / monitoring)│  │
                        │          │       └──────────────┬────────┘  │
                        │          │                      │           │
                        │          │       ┌──────────────▼────────┐  │
                        │          └──────▶│  Live data engine     │  │
                        │                  │  (klines + TA + flow) │  │
                        │                  └──────────────┬────────┘  │
                        │                                 │           │
                        └─────────────────────────────────┼───────────┘
                                                          ▼
                                              Exchange (REST / WS — wire your venue)
```

## What this repo demonstrates

- **Single-process, multi-tenant architecture.** One Python interpreter runs
  both the FastAPI surface and the trading loop. State (positions, equity,
  ring-buffered indicator/decision history) is shared via a thread-safe
  `StateStore`, so the API serves live data without IPC, queues, or a
  database.
- **Strategy + model contracts that scale.** Adding a strategy is dropping a
  file with a `decide(df) -> dict` function. Adding a model is dropping
  a `predict(features) -> dict` function and importing it from a strategy.
  No registry hand-edits, no framework lock-in — see
  [`docs/STRATEGIES.md`](docs/STRATEGIES.md) and [`docs/MODELS.md`](docs/MODELS.md).
- **Backtest = live, by construction.** `POST /backtest` calls the same
  registry, the same kline fetcher, and the same indicator builder the live
  bot uses. There is no second implementation of strategy logic — change a
  strategy and the next backtest reflects it. See
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for why this matters.
- **Pluggable exchange layer.** `server/exchange/client.py` defines a small
  `Exchange` protocol (load_markets, fetch_balance, create_order, …) and a
  default stub. Wire your venue (ccxt, vendor SDK, hand-rolled REST/WS)
  behind that protocol and the rest of the codebase doesn't change.
- **A multi-stage, ARM-targeted Docker image.** TA-Lib compiled in the builder
  layer, dependency wheels cached on `pyproject.toml` hash, runtime image
  installed via BuildKit bind-mount so wheel artefacts never bloat the final
  layer. Cold builds ~25-40 min on QEMU emulation; warm rebuilds under 30 s.
- **Adaptive entry chasing.** A four-stage order chaser fills ~95%+ of signals
  while preserving maker rebate when the book cooperates: GTX post-only at
  bid → GTC limit inside spread → IOC limit at ask + 3 bps → market failsafe
  with `priceProtect`. See `server/bot/main.py:execute_trade`.
- **Multi-account dispatch.** Each `accounts.json` entry runs its own
  exchange client, isolated state, and an explicit allow-list of strategies
  it can fire. The tick loop walks accounts in registration order; monitoring
  strategies remain account-agnostic.
- **Restart-safe state.** On boot, the FastAPI lifespan hook hydrates the
  ring buffers from on-disk JSONL so `/trade-history` and `/decisions` survive
  container restarts without a database. Logs are append-only and never
  rewritten.

## Quickstart

```bash
git clone <this-repo>
cd algorithmic-trading-server

# 1. Install (Python 3.12 recommended)
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure exchange credentials
cp .env.example .env
$EDITOR .env

# 3. Tests (no exchange access required — uses TestClient + synthetic data)
pytest server/tests/ -v

# 4. API-only mode (skips the live tick loop)
BOT_DISABLE_TICK=1 uvicorn server.api.app:app --port 8765

# 5. Full live run (after wiring your venue inside server/exchange/client.py)
uvicorn server.api.app:app --port 8765
```

Pick an obscure local port — never 3000 / 5000 / 8000 / 8080 — to avoid
collisions with whatever else you have running.

## API surface

| Method | Endpoint | Purpose |
|--|--|--|
| `GET`  | `/health`                  | Liveness/readiness probe |
| `GET`  | `/state`                   | Current bot state (position, equity, last tick) |
| `GET`  | `/strategies`              | Every registered strategy, grouped by tier |
| `GET`  | `/live-strategy`           | What's currently driving live trades |
| `GET`  | `/accounts`                | Configured accounts + their strategy filter |
| `GET`  | `/trade-history?n=100`     | Recent trade events (entries / fills / closes) |
| `GET`  | `/signal-history?n=200`    | Strategy fires from the active and monitoring tiers |
| `GET`  | `/decisions?n=200`         | Per-tick decision records |
| `GET`  | `/indicators?n=200`        | Per-tick TA / orderbook / flow snapshots |
| `GET`  | `/model-predictions`       | Rolling buffer of model `predict()` outputs |
| `GET`  | `/per-strategy-stats`      | Per-strategy WR / fires / PnL replay |
| `GET`  | `/open-trades`             | Live positions + open orders from the exchange |
| `POST` | `/trade/close`             | Force-close any open position |
| `POST` | `/trade/close-limit`       | Maker-only close: postOnly LIMIT reduceOnly with retry |
| `POST` | `/trade/open`              | Open a manual trade |
| `POST` | `/backtest`                | Replay any registered strategy over a historical window |

POST endpoints are gated by an IP whitelist (configure via `BOT_IP_WHITELIST`).
There is no API key — the IP whitelist plus whatever network gate you put in
front of the host is the only auth.

## Repository layout

```
server/
  api/                    FastAPI app + the /backtest router
  bot/                    Tick loop, state store, structured logging
  engine/                 Live data engine — klines + TA + orderbook + flow
  exchange/               Exchange client protocol + a stub implementation
  config/                 production.json + multi-account loader
  strategies/             Registry + active/, monitoring/, archived/ tiers
    active/               Two example strategies — see docs/STRATEGIES.md
  models/                 ML model registry (template — see docs/MODELS.md)
  tests/                  pytest suite — runs without an exchange connection
infra/
  docker/entrypoint.sh    Sources optional secret file, then uvicorn
docs/
  ARCHITECTURE.md         Process model, state sharing, registry pattern
  STRATEGIES.md           How to add a strategy from scratch
  MODELS.md               How to add an ML model — including the pure-NumPy pattern
  DEPLOYMENT.md           Containerisation + a generic AWS path
Dockerfile                Multi-stage ARM64 build (override platform for x86)
docker-compose.yml        Local dev stack with healthcheck
pyproject.toml            Single-source dependencies + ruff/pytest config
```

## Contributing & extending

The intended flow is:
1. Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 5 minutes.
2. Wire your exchange of choice inside `server/exchange/client.py` (the
   `Exchange` protocol + the credential resolution in `get_client()`).
3. Copy `server/strategies/active/macd_crossover.py` to a new file, rewrite
   the body, add a registry entry. See [`docs/STRATEGIES.md`](docs/STRATEGIES.md).
4. (Optional) Drop a model into `server/models/` and import it from your
   strategy. See [`docs/MODELS.md`](docs/MODELS.md).
5. Backtest via `POST /backtest`, watch live via `/state` + `/decisions`.

## License

MIT. See [LICENSE](LICENSE).

## Disclaimer

This is engineering scaffolding, not financial advice. Trading derivatives is
risky and you can lose more than you put in. Verify every line of code, every
config value, and every strategy on demo capital before pointing it at real
money. The author accepts no liability for losses incurred running this
software.
