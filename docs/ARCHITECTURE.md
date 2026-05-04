# Architecture

The bot is a single Python process. Inside that process there are two cooperating
loops:

1. **The FastAPI HTTP server**, handling reads (`/state`, `/decisions`,
   `/trade-history`, `/backtest`) and writes (`/trade/open`, `/trade/close`).
2. **The bot tick loop**, a daemon thread that wakes on the 5-minute boundary,
   asks the engine for an indicator-augmented dataframe, walks the active
   strategy registry, and dispatches a trade if any strategy returns a
   non-HOLD response.

Both loops share state through a thread-safe in-memory `StateStore`. There is
no message queue, no Redis, no external broker — the API doesn't ask the bot
"what's the current position?", it reads it directly from `STATE`. The cost
is that the API and bot must run in the same process; the benefit is that
the API serves data with a single dictionary lookup, no IPC overhead.

```
       FastAPI lifespan startup
                │
                ▼
    ┌────────────────────────┐
    │ STATE.hydrate_from_disk│   ◀── replays JSONL ring buffers from prior boot
    └───────────┬────────────┘
                │
                ▼
    ┌────────────────────────┐
    │ start_bot_thread()     │   ◀── spawns daemon thread; FastAPI keeps serving
    └───────────┬────────────┘
                │
                ▼
        ┌───────────────┐         ┌─────────────────┐
        │  Bot tick     │  reads  │  Strategy       │
        │  loop (5m)    │ ──────▶ │  registry       │
        └───────┬───────┘         │  (active +      │
                │                 │   monitoring)   │
                │ writes          └─────────────────┘
                ▼
        ┌───────────────────┐
        │  StateStore       │   ◀──── reads ──── FastAPI HTTP routes
        │  (thread-safe,    │
        │   ring-buffered)  │
        └───────────────────┘
```

## Why a single process

The cheap-AMD/cheap-ARM-instance constraint dominated the design.
A two-service architecture would mean a queue (Redis or otherwise), a sidecar
process, and serialization at every state read. On a 1 vCPU instance running a
single trader, each of those is a tax that buys nothing.

The trade-off: you can't scale the bot horizontally without rethinking state
ownership. For a single-instrument trader on a single venue, that's the
correct trade.

## State sharing

`StateStore` (in `server/bot/state_store.py`) is the single source of truth
for in-memory data. It owns:

- per-account `BotState` (position, entry, SL, TP, equity, trades_today, …)
- ring buffers: recent decisions, recent trades, recent indicator snapshots,
  recent signal hits, recent model predictions
- per-account open-orders snapshots
- a heartbeat timestamp the API uses for `/health`

Every method takes a lock. Reads and writes are short and never block on I/O.
The bot tick is the only writer for tick-derived state; the API writes only on
manual trade actions. This makes lock contention effectively zero.

## Restart safety

The on-disk JSONL logs (`logs/decisions.jsonl`, `logs/trades.csv`,
`logs/signal_hits.jsonl`, `logs/indicators.jsonl`,
`logs/model_predictions.jsonl`) are append-only. The container's volume mount
persists them across restarts. At boot, `STATE.hydrate_from_disk()` replays
the tail of each file (capped to the relevant ring buffer's `maxlen`) so the
API serves intelligible data within milliseconds of the new container starting
— no warmup window where `/trade-history` returns empty arrays.

## The strategy registry

Every strategy lives in `server/strategies/{active,monitoring,archived}/<name>.py`
and exports a `decide(df, exchange_metrics?) -> dict` function. They're
collected in `server/strategies/__init__.py` as `StrategyMeta` entries:

```python
@dataclass
class StrategyMeta:
    name: str           # short identifier; key in REGISTRY
    tier: str           # 'active' | 'monitoring' | 'archived'
    decide: Callable    # (df, exchange_metrics?) -> response dict
    description: str
    file: str
    needs_exchange_metrics: bool = False  # True if decide() uses orderbook/flow
    priority: int = 100                    # lower runs first when multiple actives
```

The bot tick loop calls `get_active()` (sorted by priority) and walks until
something returns a non-HOLD response. Monitoring strategies are evaluated and
logged but never trade. Archived strategies are kept on disk for reference
but skipped at runtime.

The same registry is consulted by:
- `/strategies` (list)
- `/live-strategy` (active subset, in priority order)
- `/backtest` (any registered strategy can be replayed)
- the bot tick (active dispatch + monitoring observation)

This is the core invariant of the codebase: there is exactly one place that
names strategies. Move a file between folders, flip its `tier=`, and the
behaviour change propagates to every consumer in the next request.

## The exchange layer

`server/exchange/client.py` defines a small `Exchange` protocol — the methods
the bot tick loop calls (`load_markets`, `fetch_balance`, `fetch_positions`,
`create_order`, …). A default `_StubExchange` raises on any real call so that
bringing up the server without wiring a venue is a clear, loud failure rather
than silent breakage.

To wire a venue:
- ccxt-supported: import ccxt, instantiate with your keys + options, return
  the client. ccxt already satisfies the protocol.
- Vendor SDK: write a thin adapter class with the same method signatures.
- Hand-rolled REST/WS: implement the protocol directly.

Per-account credential resolution lives entirely inside `get_client()` —
nothing else in the codebase knows or cares how keys are sourced.

## The live engine

`server/engine/live_engine.py` is a pure feature engine. It owns:

- the rolling 5m kline DataFrame (warmup-primed, periodically caught-up)
- the `pandas_ta.AllStudy` indicator computation (~140 columns)
- a workaround for pandas-ta's `dpo` lookahead bug (forces `centered=False`
  to keep the indicator causal at the live tail — see the docstring on
  `build_indicators()` for the full story)
- on-demand fetches of orderbook depth, taker ratio, open interest, top-trader
  long/short ratios, and funding rate (URLs are env-overridable; wire your
  venue once and the rest of the codebase is venue-agnostic)

It does not know about trades, positions, or orders. The bot tick loop calls
`engine.build_packet()`, hands the resulting DataFrame to strategies, and
takes responsibility for whatever they decide.

## Backtest = live, by construction

`POST /backtest` is built around one rule: never re-implement strategy logic
or feature computation. It calls `server.strategies.REGISTRY[name].decide()`
on a slice of the same dataframe `engine.build_indicators()` produces, against
klines fetched by the same `fetch_klines()` the live engine uses every tick.

For strategies that need exchange-side metrics (orderbook, flow), the
backtest fetches historical futures data from the same endpoints — the
only gap is orderbook depth, for which most venues have no historical
endpoint.

The position-management semantics in the backtest also mirror the live
bot: once a fire opens a trade, no further fires are accepted until that
trade resolves. Without this rule, signal clusters that hold for many
adjacent bars get over-counted as separate trades.

The result: a 14-day backtest WR is genuinely predictive of the next 7 days'
live WR, modulo regime drift. There is no "backtest looked good but live
behaves differently because the code paths differ" failure mode here.

## What lives outside the bot

- The exchange itself.
- Optional secret store at `/tmp/ssm.env` — sourced at container boot if
  present (AWS SSM, Vault, Doppler, or a hand-rolled drop-file). Local dev
  reads from `.env` via `python-dotenv`.
- Log shipper (optional). `trading.jsonl` is structured for any aggregator —
  CloudWatch Logs Insights, Loki, Splunk, etc.
