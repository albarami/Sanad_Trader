#!/usr/bin/env python3
"""
Daily PnL Reset — Sprint 4.6.4 (hardened)
Runs at 00:00 UTC via cron.
Derives yesterday's PnL from trade_history.json, archives it, resets daily counters.
"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
PORTFOLIO_PATH = STATE_DIR / "portfolio.json"
TRADE_HISTORY_PATH = STATE_DIR / "trade_history.json"
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
    starting = portfolio.get("starting_balance_usd", 10000.0)
    balance = portfolio.get("current_balance_usd", starting)
    prev_reset_at = portfolio.get("daily_reset_at", "1970-01-01T00:00:00")

    # Derive yesterday's PnL from trade_history (source of truth)
    trade_history = _load_json(TRADE_HISTORY_PATH, {})
    trades = trade_history.get("trades", trade_history) if isinstance(trade_history, dict) else trade_history
    daily_pnl_usd = sum(
        float(t.get("pnl_usd", t.get("net_pnl_usd", 0)) or 0)
        for t in trades if isinstance(t, dict)
        and (t.get("closed_at", t.get("timestamp", "")) >= prev_reset_at)
    )
    daily_pnl_pct = round(daily_pnl_usd / starting, 6) if starting > 0 else 0

    # Archive yesterday's P&L
    record = {
        "date": yesterday,
        "daily_pnl_pct": daily_pnl_pct,
        "daily_pnl_usd": round(daily_pnl_usd, 2),
        "closing_balance": balance,
        "daily_reset_at": prev_reset_at,
        "archived_at": now.isoformat(),
    }

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
        _log(f"Archived {yesterday}: PnL={daily_pnl_pct:+.4f}% (${daily_pnl_usd:+.2f}), balance=${balance:.2f}")
    except Exception as e:
        _log(f"Error archiving: {e}")

    # Reset daily counters — SQLite is SSOT, JSON is cache
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR / "scripts"))
        import state_store
        state_store.update_portfolio({
            "daily_pnl_pct": 0.0,
            "daily_pnl_usd": 0.0,
            "daily_trades": 0,
            "starting_balance_usd": balance,  # Today's starting balance = yesterday's closing
        })
        state_store.sync_json_cache()
        _log(f"Daily PnL reset to 0 (SQLite + JSON). New day: {now.strftime('%Y-%m-%d')}")
    except Exception as e:
        _log(f"SQLite reset failed ({e}), falling back to JSON-only")
        portfolio["daily_pnl_pct"] = 0.0
        portfolio["daily_pnl_usd"] = 0.0
        portfolio["daily_trades"] = 0
        portfolio["daily_reset_at"] = now.isoformat()
        _save_json(PORTFOLIO_PATH, portfolio)
        _log(f"Daily PnL reset to 0 (JSON only). New day: {now.strftime('%Y-%m-%d')}")


if __name__ == "__main__":
    reset()
