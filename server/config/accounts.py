"""Accounts configuration loader.

The bot can trade multiple exchange accounts in parallel. Each account has
its own:
  - exchange client (built per-account by `server.exchange.get_client`)
  - state (position, entry, SL, TP, equity) — namespaced in StateStore
  - strategy subset (which active strategies are allowed to fire on it)
  - leverage + notional sizing

If `accounts.json` is absent OR contains only one account, the bot runs in
single-account mode. When `accounts.json` exists with multiple accounts, the
bot's tick loop fans out per account (each account is its own slot, each
picks from its own assigned strategies in registry priority order). Monitoring
strategies remain account-agnostic.

Default config (when no file present):
  account1 = "all" strategies (i.e. the entire active registry tier)
  leverage = $BOT_LEVERAGE env or 7
  notional_pct = 0.97

Example multi-account config (write to `server/config/accounts.json`):

    {
      "account1": {
        "strategies": ["macd_crossover"],
        "leverage": 7,
        "notional_pct": 0.97
      },
      "account2": {
        "strategies": ["rsi_meanreversion"],
        "leverage": 5,
        "notional_pct": 0.97
      }
    }

How credentials are sourced is entirely up to your `server.exchange.get_client`
implementation — accounts.json names which strategies fire on which account,
nothing more.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Union

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _PROJECT_ROOT / "server" / "config" / "accounts.json"

CONFIG_PATH = Path(os.environ.get("ACCOUNTS_CONFIG_PATH", str(_DEFAULT_PATH)))

DEFAULT_ACCOUNT_ID = "account1"


def _default_config() -> dict:
    """Single-account default. account1 runs every active strategy."""
    return {
        DEFAULT_ACCOUNT_ID: {
            "strategies": "all",
            "leverage": int(os.environ.get("BOT_LEVERAGE", "7")),
            "notional_pct": 0.97,
        }
    }


_cache: dict | None = None


def _load() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict) or not cfg:
                return _default_config()
            return cfg
        except Exception:
            return _default_config()
    return _default_config()


def get_accounts() -> dict:
    """Return the parsed accounts config. Cached after first call.

    Tests / runtime can call `reset_cache()` to force a re-read.
    """
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def reset_cache() -> None:
    """Test hook — drops the cached config so the next get_accounts()
    re-reads from disk. Used by unit tests; safe to call at any time."""
    global _cache
    _cache = None


def get_account_ids() -> list[str]:
    """List of configured account IDs in declaration order."""
    return list(get_accounts().keys())


def get_account_config(account_id: str) -> dict:
    accs = get_accounts()
    if account_id not in accs:
        raise KeyError(
            f"unknown account_id '{account_id}' "
            f"(configured: {list(accs.keys())})"
        )
    return accs[account_id]


def get_strategies_for_account(account_id: str) -> Union[str, list[str]]:
    """Returns either the literal string 'all' (no filter — every active
    strategy is allowed on this account) or a list of strategy names that
    this account is permitted to fire."""
    cfg = get_account_config(account_id)
    return cfg.get("strategies", "all")


def is_strategy_allowed(account_id: str, strategy_name: str) -> bool:
    """True if `strategy_name` is permitted to fire on `account_id`."""
    allowed = get_strategies_for_account(account_id)
    if allowed == "all":
        return True
    return strategy_name in allowed


def get_leverage(account_id: str) -> int:
    return int(get_account_config(account_id).get("leverage", 7))


def get_notional_pct(account_id: str) -> float:
    return float(get_account_config(account_id).get("notional_pct", 0.97))


def is_multi_account_mode() -> bool:
    """True if more than one account is configured. Used by the bot tick
    loop to decide between the single-account fast path and the per-account
    fan-out path."""
    return len(get_account_ids()) > 1
