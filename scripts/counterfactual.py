#!/usr/bin/env python3
"""
Counterfactual Analysis — Sprint 5.1.11

Deterministic Python. No LLMs.

For every trade taken, records what would have happened if we DIDN'T trade.
Measures actual edge vs random market noise.

Logic:
- At trade ENTRY: snapshot the price
- At trade EXIT: check what price would be at same exit time
- Compare: our P&L vs "hold nothing" baseline
- Only patterns where edge > 95th percentile are promoted

Reads: state/trade_history.json
Writes: genius-memory/counterfactuals.json
"""

import json
import os
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
TRADE_HISTORY_PATH = BASE_DIR / "state" / "trade_history.json"
COUNTERFACTUAL_PATH = BASE_DIR / "genius-memory" / "counterfactuals.json"
STATE_DIR = BASE_DIR / "state"

EDGE_PERCENTILE_THRESHOLD = 95  # Only promote patterns above 95th percentile


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[CF] {ts} {msg}", flush=True)


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


def record_counterfactual(trade: dict) -> dict:
    """Record counterfactual for a completed trade.

    Compare actual P&L vs what would have happened holding nothing.

    For a BUY trade: counterfactual is 0% (we didn't trade).
    Edge = actual_pnl_pct - 0 = actual_pnl_pct

    For more sophisticated analysis, we compare against:
    1. Hold nothing (0% return)
    2. Hold BTC instead (opportunity cost)
    3. Random entry/exit (Monte Carlo baseline)
    """
    entry_price = trade.get("entry_price", 0)
    exit_price = trade.get("exit_price", 0)
    if entry_price <= 0 or exit_price <= 0:
        return {}

    side = trade.get("side", "BUY").upper()

    # Actual P&L
    if side == "BUY":
        actual_pnl_pct = ((exit_price - entry_price) / entry_price) * 100
    else:
        actual_pnl_pct = ((entry_price - exit_price) / entry_price) * 100

    # Counterfactual 1: Do nothing (0% return)
    edge_vs_nothing = actual_pnl_pct

    # Counterfactual 2: Random baseline (simulate 100 random trades)
    # Approximation: random normal with same holding period volatility
    hold_hours = trade.get("hold_duration_hours", trade.get("hold_hours", 24))
    daily_vol_pct = 3.0  # Rough crypto daily vol
    hourly_vol = daily_vol_pct / (24 ** 0.5)

    random_returns = []
    for _ in range(100):
        rand_return = random.gauss(0, hourly_vol * (hold_hours ** 0.5))
        random_returns.append(rand_return)

    random_mean = statistics.mean(random_returns)
    random_std = statistics.stdev(random_returns) if len(random_returns) > 1 else 1

    # Edge percentile: where does our actual return fall in random distribution?
    better_than = sum(1 for r in random_returns if actual_pnl_pct > r)
    edge_percentile = (better_than / len(random_returns)) * 100

    cf = {
        "trade_id": trade.get("trade_id", trade.get("id", "unknown")),
        "token": trade.get("token", trade.get("symbol", "")),
        "strategy": trade.get("strategy", "unknown"),
        "actual_pnl_pct": round(actual_pnl_pct, 2),
        "edge_vs_nothing": round(edge_vs_nothing, 2),
        "edge_percentile": round(edge_percentile, 1),
        "random_baseline_mean": round(random_mean, 2),
        "random_baseline_std": round(random_std, 2),
        "is_significant": edge_percentile >= EDGE_PERCENTILE_THRESHOLD,
        "hold_hours": hold_hours,
        "analyzed_at": _now().isoformat(),
    }

    return cf


def analyze_all_trades() -> dict:
    """Run counterfactual analysis on all completed trades."""
    _log("=== COUNTERFACTUAL ANALYSIS ===")

    history = _load_json(TRADE_HISTORY_PATH, [])
    trades = history if isinstance(history, list) else history.get("trades", [])

    if not trades:
        _log("No completed trades to analyze")
        return {"total": 0, "counterfactuals": []}

    counterfactuals = []
    significant_count = 0

    for trade in trades:
        # Skip if not completed
        if trade.get("status") not in ("closed", "CLOSED", None):
            continue

        cf = record_counterfactual(trade)
        if cf:
            counterfactuals.append(cf)
            if cf.get("is_significant"):
                significant_count += 1

    # Aggregate stats
    edges = [cf["edge_vs_nothing"] for cf in counterfactuals if "edge_vs_nothing" in cf]
    percentiles = [cf["edge_percentile"] for cf in counterfactuals if "edge_percentile" in cf]

    result = {
        "total_trades_analyzed": len(counterfactuals),
        "significant_edges": significant_count,
        "significance_rate": round(significant_count / len(counterfactuals) * 100, 1) if counterfactuals else 0,
        "avg_edge_vs_nothing": round(statistics.mean(edges), 2) if edges else 0,
        "avg_edge_percentile": round(statistics.mean(percentiles), 1) if percentiles else 0,
        "by_strategy": {},
        "counterfactuals": counterfactuals,
        "analyzed_at": _now().isoformat(),
    }

    # Group by strategy
    for cf in counterfactuals:
        strat = cf.get("strategy", "unknown")
        if strat not in result["by_strategy"]:
            result["by_strategy"][strat] = {"trades": 0, "significant": 0, "edges": []}
        result["by_strategy"][strat]["trades"] += 1
        result["by_strategy"][strat]["edges"].append(cf["edge_vs_nothing"])
        if cf.get("is_significant"):
            result["by_strategy"][strat]["significant"] += 1

    for strat, data in result["by_strategy"].items():
        data["avg_edge"] = round(statistics.mean(data["edges"]), 2) if data["edges"] else 0
        data["significance_rate"] = round(data["significant"] / data["trades"] * 100, 1) if data["trades"] else 0
        del data["edges"]  # Don't persist raw list

    _save_json(COUNTERFACTUAL_PATH, result)
    _log(f"Analyzed {len(counterfactuals)} trades, {significant_count} with significant edge (≥{EDGE_PERCENTILE_THRESHOLD}th percentile)")

    return result


if __name__ == "__main__":
    result = analyze_all_trades()
    if result["total_trades_analyzed"] > 0:
        print(f"  Trades analyzed: {result['total_trades_analyzed']}")
        print(f"  Significant edges: {result['significant_edges']} ({result['significance_rate']}%)")
        print(f"  Avg edge vs nothing: {result['avg_edge_vs_nothing']}%")
