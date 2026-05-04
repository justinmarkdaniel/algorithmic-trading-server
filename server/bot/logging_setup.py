"""Logging — human-readable rotated text log + a structured JSON sidecar.

Outputs:
  - logs/trading.log     : human-readable, rotated daily, 14-day retention
  - logs/trading.jsonl   : one JSON line per record (for log-aggregator parsing)
  - stdout               : same human-readable format (container stdout)

The dual-format design lets you tail `trading.log` from a terminal while a
log shipper consumes `trading.jsonl` for structured queries — a single record
appears in both, formatted appropriately for each consumer.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class UTCFormatter(logging.Formatter):
    """ISO-8601 UTC timestamps."""

    def formatTime(self, record, datefmt=None):
        utc = datetime.fromtimestamp(record.created, timezone.utc)
        return utc.strftime("%Y-%m-%d %H:%M:%S UTC")


class JSONLineFormatter(logging.Formatter):
    """One JSON object per record — for downstream log-aggregator parsing."""

    def format(self, record):
        utc_iso = datetime.fromtimestamp(record.created, timezone.utc).isoformat()
        payload = {
            "ts": utc_iso,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Strategies / bot internals can attach structured fields via
        # logger.info("msg", extra={"event": "..."})
        for key in ("event", "tick_id", "bar", "action", "price", "why",
                    "trade_id", "side", "qty", "tp", "sl", "duration_ms"):
            v = getattr(record, key, None)
            if v is not None:
                payload[key] = v
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(log_dir: Path | None = None) -> logging.Logger:
    log_dir = log_dir or Path(os.environ.get("BOT_LOG_DIR", "/app/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()  # idempotent on repeated import

    human_fmt = UTCFormatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    json_fmt = JSONLineFormatter()

    fh_human = TimedRotatingFileHandler(
        log_dir / "trading.log", when="midnight", backupCount=14, utc=True
    )
    fh_human.setFormatter(human_fmt)

    fh_json = TimedRotatingFileHandler(
        log_dir / "trading.jsonl", when="midnight", backupCount=14, utc=True
    )
    fh_json.setFormatter(json_fmt)

    sh = logging.StreamHandler()
    sh.setFormatter(human_fmt)

    for h in (fh_human, fh_json, sh):
        root.addHandler(h)

    # Quieten chatty libs.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)

    return logging.getLogger("TradingBot")
