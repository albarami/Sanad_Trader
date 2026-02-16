#!/usr/bin/env python3
"""
Daily PnL Reset — Sprint 4.6.4
Runs at 00:00 UTC via cron.
Resets daily_pnl_pct in portfolio.json to 0.
Archives previous day's P&L to genius-memory.
"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
PORTFOLIO_PATH = STATE_DIR / "portfolio.json"
HISTORY_PATH = BASE_DIR / "genius-memory" / "regime-data" / "daily_pnl_history.jsonl"


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[PNL-RESET] {ts} {msg}", flush=True)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def reset():
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    portfolio = _load_json(PORTFOLIO_PATH, {})
    daily_pnl = portfolio.get("daily_pnl_pct", 0)
    daily_pnl_usd = portfolio.get("daily_pnl_usd", 0)
    balance = portfolio.get("balance_usd", 10000)

    # Archive yesterday's P&L
    record = {
        "date": yesterday,
        "daily_pnl_pct": daily_pnl,
        "daily_pnl_usd": daily_pnl_usd,
        "closing_balance": balance,
        "archived_at": now.isoformat(),
    }

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
        _log(f"Archived {yesterday}: PnL={daily_pnl:+.2f}% (${daily_pnl_usd:+.2f}), balance=${balance:.2f}")
    except Exception as e:
        _log(f"Error archiving: {e}")

    # Reset daily counters
    portfolio["daily_pnl_pct"] = 0.0
    portfolio["daily_pnl_usd"] = 0.0
    portfolio["daily_trades"] = 0
    portfolio["daily_reset_at"] = now.isoformat()
    _save_json(PORTFOLIO_PATH, portfolio)

    _log(f"Daily PnL reset to 0. New day: {now.strftime('%Y-%m-%d')}")


if __name__ == "__main__":
    reset()
