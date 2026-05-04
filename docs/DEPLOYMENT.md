# Deployment

The bot is designed to run in a single small container on a single instance.
The Dockerfile defaults to `linux/arm64` (cheap Graviton or Ampere instances)
but accepts `--platform=linux/amd64` for x86 hosts.

## Local

```bash
docker compose up --build
# API: http://localhost:8000
# Healthcheck: http://localhost:8000/health
```

The `docker-compose.yml` mounts `./logs` so JSONL ring buffers persist across
container restarts.

## Production — generic outline

Any path that ends with "container running on a single small instance with
persistent storage" works. The reference path the author used was AWS:

1. **Image registry** — push the multi-arch image to ECR (or GHCR / Docker
   Hub / GAR — anywhere your runtime can pull from).
2. **Compute** — a single small ARM instance (e.g. AWS t4g.nano, ~$3/mo) is
   sufficient for one trader on one symbol. The healthcheck in the Dockerfile
   means you can wire it up to ECS, plain Docker on EC2, or Fly.io with
   minimal config.
3. **Secrets** — the `entrypoint.sh` looks for `/tmp/ssm.env` at boot and
   sources it before launching uvicorn. Fill it however you like:
   - AWS SSM Parameter Store via `aws ssm get-parameters-by-path`
   - HashiCorp Vault via `vault kv get`
   - Doppler via `doppler secrets download`
   - A hand-rolled `init-container` writing the file
4. **Persistent volume** — mount one at `/app/logs`. JSONL ring-buffer hydration
   relies on these surviving restarts.
5. **Network** — put the API behind whatever fronts your other services
   (ALB / Cloudflare / Tailscale). The `BOT_IP_WHITELIST` env var gates the
   POST endpoints at the application layer; the network layer should gate
   GETs as well unless you're comfortable exposing the indicator/decision log
   publicly.

## Required env vars

| Variable | Purpose | Required |
|--|--|--|
| `ACCOUNT1_API_KEY`          | Whatever your `get_client()` reads             | yes |
| `ACCOUNT1_API_SECRET`       | Whatever your `get_client()` reads             | yes |
| `EXCHANGE_BASE`             | REST root the engine fetches klines from       | yes (or hardcode) |
| `DEFAULT_SYMBOL`            | Trading symbol (`BTC/USDT:USDT` shape)         | no |
| `DEFAULT_SYMBOL_NATIVE`     | Engine-side symbol (e.g. `BTCUSDT`)            | no |
| `BOT_IP_WHITELIST`          | Comma-separated IPs allowed through middleware | prod only |
| `BOT_IP_WHITELIST_DISABLED` | `1` to disable the whitelist (local dev)       | no |
| `BOT_LOG_DIR`               | Where to write logs / jsonls                   | no (default `/app/logs`) |
| `BOT_CONFIG_PATH`           | Path to `production.json`                      | no |
| `BOT_DISABLE_TICK`          | `1` to skip the tick loop (API-only mode)      | no |
| `WARMUP_BARS`               | Override engine warmup length (default 1500)   | no |
| `BOT_MAX_DAILY_TRADES`      | Daily trade cap per account (default 10)       | no |
| `BOT_MAX_HOLD_BARS`         | Default hold timeout in 5m bars (default 48)   | no |
| `BOT_LEVERAGE`              | Default leverage when accounts.json is absent  | no |
| `ALERT_WEBHOOK_URL`         | If set, the bot POSTs trade events here        | no |

## Multi-account

Drop a `server/config/accounts.json` to run multiple accounts in parallel —
each with its own credentials (resolved by your `get_client()`), leverage,
and strategy filter. See the docstring on `server/config/accounts.py` for the
schema. Single-account is the default and requires no JSON file.

## Going live

The shipped exchange client is a stub — it raises on every real call so that
forgetting to wire your venue produces a loud, clear failure. Replace it
in `server/exchange/client.py` before any live deployment.

The recommended sequence is:
1. Run on the venue's testnet / demo for >= 14 days.
2. Use `/per-strategy-stats?hours=336` to confirm live behaviour matches the
   `/backtest` replay over the same window. Variance > a few percentage
   points in WR is a red flag — investigate before going live.
3. Switch to production with a small notional cap (`BOT_MAX_DAILY_TRADES=2`,
   `notional_pct=0.05` in accounts.json). Watch for a week.
4. Scale up gradually, monitoring `/per-strategy-stats` daily.
