#!/usr/bin/env python3
"""
Daily Performance Report — Sprint 6.1.13
Runs daily at 23:00 QAT (20:00 UTC).
Deterministic Python. Sends summary via notifier.

Reads: state/portfolio.json, state/trade_history.json, state/oms_orders.json
Writes: reports/daily/YYYYMMDD.json
"""

import json
import os
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
REPORTS_DIR = BASE_DIR / "reports" / "daily"
PORTFOLIO_PATH = STATE_DIR / "portfolio.json"
TRADE_HISTORY = STATE_DIR / "trade_history.json"
OMS_ORDERS = STATE_DIR / "oms_orders.json"
SIGNALS_DIR = BASE_DIR / "signals"

import sys
sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[DAILY] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _get_today_trades() -> list:
    history = _load_json(TRADE_HISTORY, [])
    trades = history if isinstance(history, list) else history.get("trades", [])
    today = _now().strftime("%Y-%m-%d")
    return [t for t in trades
            if t.get("closed_at", t.get("exit_time", ""))[:10] == today
            or t.get("opened_at", t.get("entry_time", ""))[:10] == today]


def _count_signals_today() -> tuple:
    """Count signals processed and rejected today."""
    today = _now().strftime("%Y%m%d")
    processed = 0
    rejected = 0
    if SIGNALS_DIR.exists():
        for subdir in SIGNALS_DIR.iterdir():
            if subdir.is_dir():
                for f in subdir.iterdir():
                    if today in f.name:
                        processed += 1
                        data = _load_json(f)
                        if data.get("rejected") or data.get("status") == "rejected":
                            rejected += 1
    return processed, rejected


def generate_report() -> dict:
    _log("=== DAILY PERFORMANCE REPORT ===")

    portfolio = _load_json(PORTFOLIO_PATH, {})
    today_trades = _get_today_trades()
    signals_processed, signals_rejected = _count_signals_today()

    # Trade stats
    wins = sum(1 for t in today_trades if t.get("pnl_pct", 0) > 0)
    losses = sum(1 for t in today_trades if t.get("pnl_pct", 0) < 0)
    pnls = [t.get("pnl_pct", 0) for t in today_trades if t.get("pnl_pct")]
    daily_pnl_pct = sum(pnls) if pnls else 0
    daily_pnl_usd = sum(t.get("pnl_usd", 0) for t in today_trades)
    win_rate = wins / len(today_trades) if today_trades else 0

    # Portfolio
    balance = portfolio.get("balance", portfolio.get("total_equity", 0))
    drawdown = portfolio.get("max_drawdown_pct", portfolio.get("drawdown_pct", 0))
    open_positions = portfolio.get("open_positions", 0)
    exposure = portfolio.get("exposure_pct", 0)

    # Active orders
    oms = _load_json(OMS_ORDERS, {})
    active_orders = len([o for o in oms.get("orders", {}).values()
                        if isinstance(o, dict) and o.get("state") not in
                        ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED")])

    report = {
        "date": _now().strftime("%Y-%m-%d"),
        "generated_at": _now().isoformat(),
        "trades_today": len(today_trades),
        "wins": wins,
        "losses": losses,
        "daily_win_rate": round(win_rate, 4),
        "daily_pnl_pct": round(daily_pnl_pct, 2),
        "daily_pnl_usd": round(daily_pnl_usd, 2),
        "portfolio_balance": balance,
        "max_drawdown_pct": drawdown,
        "open_positions": open_positions,
        "exposure_pct": exposure,
        "active_orders": active_orders,
        "signals_processed": signals_processed,
        "signals_rejected": signals_rejected,
        "best_trade": max(pnls) if pnls else 0,
        "worst_trade": min(pnls) if pnls else 0,
    }

    # Save report
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = _now().strftime("%Y%m%d")
    _save_json(REPORTS_DIR / f"{date_str}.json", report)

    # Send notification
    try:
        import notifier
        notifier.notify_daily_summary(report)
        _log("Daily summary notification sent")
    except Exception as e:
        _log(f"Notification failed: {e}")

    _log(f"Trades: {len(today_trades)}, P&L: {daily_pnl_pct:+.2f}%, Balance: ${balance:,.2f}")
    _log("=== REPORT COMPLETE ===")
    return report


if __name__ == "__main__":
    generate_report()
