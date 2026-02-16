#!/usr/bin/env python3
"""
Execution Quality Tracking — Sprint 4.3.1 through 4.3.5

Deterministic Python. No LLMs.

Tracks:
4.3.1 — Expected vs realized slippage
4.3.2 — Fill latency (p50/p95)
4.3.3 — Fill rate (% fully filled)
4.3.4 — Events → Supabase (execution_quality table)
4.3.5 — Cost per trade (fees + slippage + gas)

Reads from: state/oms_orders.json, execution-logs/oms_paper_fills.jsonl
Writes to: state/execution_quality.json, Supabase
"""

import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
OMS_ORDERS_PATH = STATE_DIR / "oms_orders.json"
EQ_STATE_PATH = STATE_DIR / "execution_quality.json"
FILLS_LOG = BASE_DIR / "execution-logs" / "oms_paper_fills.jsonl"


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[EQ] {ts} {msg}", flush=True)


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


# ─────────────────────────────────────────────────────────
# 4.3.1 — Slippage Analysis
# ─────────────────────────────────────────────────────────

def calc_slippage(expected_price: float, realized_price: float, side: str) -> float:
    """Calculate slippage in basis points.

    Positive = unfavorable, Negative = favorable.
    BUY: realized > expected = unfavorable
    SELL: realized < expected = unfavorable
    """
    if expected_price <= 0:
        return 0.0
    if side.upper() == "BUY":
        return ((realized_price - expected_price) / expected_price) * 10000
    else:
        return ((expected_price - realized_price) / expected_price) * 10000


# ─────────────────────────────────────────────────────────
# 4.3.2 — Fill Latency
# ─────────────────────────────────────────────────────────

def calc_latency_ms(submitted_at: str, filled_at: str) -> float | None:
    """Calculate fill latency in milliseconds."""
    try:
        sub = datetime.fromisoformat(submitted_at)
        fill = datetime.fromisoformat(filled_at)
        return (fill - sub).total_seconds() * 1000
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────
# 4.3.5 — Cost Per Trade
# ─────────────────────────────────────────────────────────

# Fee schedules (maker/taker)
EXCHANGE_FEES = {
    "binance": {"maker": 0.001, "taker": 0.001},  # 0.1% standard
    "mexc": {"maker": 0.0, "taker": 0.001},        # 0% maker, 0.1% taker
    "dex": {"maker": 0.003, "taker": 0.003},        # ~0.3% AMM fee
}


def calc_trade_cost(order: dict) -> dict:
    """Calculate total cost of a trade: fees + slippage."""
    exchange = order.get("exchange", "binance")
    fees = EXCHANGE_FEES.get(exchange, EXCHANGE_FEES["binance"])

    quantity = order.get("filled_quantity", order.get("quantity", 0))
    fill_price = order.get("avg_fill_price", order.get("price", 0))
    expected_price = order.get("price", fill_price)
    side = order.get("side", "BUY")
    order_type = order.get("order_type", "LIMIT")

    notional = quantity * fill_price

    # Fee based on order type
    fee_rate = fees["maker"] if order_type == "LIMIT" else fees["taker"]
    fee_usd = notional * fee_rate

    # Slippage cost
    slippage_bps = calc_slippage(expected_price, fill_price, side)
    slippage_usd = notional * abs(slippage_bps) / 10000

    # Gas (only for DEX)
    gas_usd = 0.01 if exchange == "dex" else 0.0

    return {
        "notional_usd": round(notional, 2),
        "fee_rate": fee_rate,
        "fee_usd": round(fee_usd, 4),
        "slippage_bps": round(slippage_bps, 2),
        "slippage_usd": round(slippage_usd, 4),
        "gas_usd": gas_usd,
        "total_cost_usd": round(fee_usd + slippage_usd + gas_usd, 4),
        "total_cost_bps": round((fee_usd + slippage_usd + gas_usd) / notional * 10000, 2) if notional > 0 else 0,
    }


# ─────────────────────────────────────────────────────────
# 4.3.3 — Fill Rate & Aggregate Stats
# ─────────────────────────────────────────────────────────

def analyze_all_orders() -> dict:
    """Analyze all OMS orders for execution quality metrics."""
    orders = _load_json(OMS_ORDERS_PATH, {})
    if not orders:
        _log("No orders to analyze")
        return {"total_orders": 0}

    total = len(orders)
    filled = 0
    partially_filled = 0
    rejected = 0
    canceled = 0
    failed = 0

    slippages = []
    latencies = []
    costs = []

    for oid, order in orders.items():
        state = order.get("state", "")

        if state == "FILLED":
            filled += 1
        elif state == "PARTIALLY_FILLED":
            partially_filled += 1
        elif state == "REJECTED":
            rejected += 1
        elif state == "CANCELED":
            canceled += 1
        elif state == "FAILED":
            failed += 1

        # Slippage (4.3.1)
        expected = order.get("price", 0)
        realized = order.get("avg_fill_price", 0)
        side = order.get("side", "BUY")
        if expected > 0 and realized > 0:
            slip = calc_slippage(expected, realized, side)
            slippages.append(slip)

        # Latency (4.3.2)
        submitted = order.get("submitted_at")
        filled_at = order.get("filled_at")
        if submitted and filled_at:
            lat = calc_latency_ms(submitted, filled_at)
            if lat is not None:
                latencies.append(lat)

        # Cost (4.3.5)
        if state in ("FILLED", "PARTIALLY_FILLED") and realized > 0:
            cost = calc_trade_cost(order)
            costs.append(cost)

    # Fill rate (4.3.3)
    fill_rate = (filled / total * 100) if total > 0 else 0

    # Slippage stats
    slip_stats = {}
    if slippages:
        slip_stats = {
            "mean_bps": round(statistics.mean(slippages), 2),
            "median_bps": round(statistics.median(slippages), 2),
            "max_bps": round(max(slippages), 2),
            "min_bps": round(min(slippages), 2),
            "stddev_bps": round(statistics.stdev(slippages), 2) if len(slippages) > 1 else 0,
            "samples": len(slippages),
        }

    # Latency stats
    lat_stats = {}
    if latencies:
        sorted_lat = sorted(latencies)
        p50_idx = len(sorted_lat) // 2
        p95_idx = int(len(sorted_lat) * 0.95)
        lat_stats = {
            "p50_ms": round(sorted_lat[p50_idx], 1),
            "p95_ms": round(sorted_lat[min(p95_idx, len(sorted_lat) - 1)], 1),
            "mean_ms": round(statistics.mean(latencies), 1),
            "max_ms": round(max(latencies), 1),
            "samples": len(latencies),
        }

    # Cost stats
    cost_stats = {}
    if costs:
        total_fees = sum(c["fee_usd"] for c in costs)
        total_slippage = sum(c["slippage_usd"] for c in costs)
        total_gas = sum(c["gas_usd"] for c in costs)
        total_notional = sum(c["notional_usd"] for c in costs)
        cost_stats = {
            "total_fee_usd": round(total_fees, 4),
            "total_slippage_usd": round(total_slippage, 4),
            "total_gas_usd": round(total_gas, 4),
            "total_cost_usd": round(total_fees + total_slippage + total_gas, 4),
            "avg_cost_bps": round(statistics.mean([c["total_cost_bps"] for c in costs]), 2),
            "total_notional_usd": round(total_notional, 2),
            "trades_analyzed": len(costs),
        }

    result = {
        "total_orders": total,
        "filled": filled,
        "partially_filled": partially_filled,
        "rejected": rejected,
        "canceled": canceled,
        "failed": failed,
        "fill_rate_pct": round(fill_rate, 1),
        "slippage": slip_stats,
        "latency": lat_stats,
        "costs": cost_stats,
        "analyzed_at": _now().isoformat(),
    }

    # Save state
    _save_json(EQ_STATE_PATH, result)
    return result


# ─────────────────────────────────────────────────────────
# 4.3.4 — Push to Supabase
# ─────────────────────────────────────────────────────────

def push_to_supabase(eq_data: dict):
    """Push execution quality metrics to Supabase."""
    try:
        import sys
        sys.path.insert(0, str(SCRIPT_DIR))
        from supabase_client import get_client

        client = get_client()
        if not client:
            _log("Supabase client not available — skipping push")
            return False

        record = {
            "total_orders": eq_data.get("total_orders", 0),
            "fill_rate_pct": eq_data.get("fill_rate_pct", 0),
            "avg_slippage_bps": eq_data.get("slippage", {}).get("mean_bps", 0),
            "p50_latency_ms": eq_data.get("latency", {}).get("p50_ms", 0),
            "p95_latency_ms": eq_data.get("latency", {}).get("p95_ms", 0),
            "total_cost_usd": eq_data.get("costs", {}).get("total_cost_usd", 0),
            "analyzed_at": eq_data.get("analyzed_at"),
        }

        result = client.table("execution_quality").insert(record).execute()
        _log(f"Pushed to Supabase: {result.data}")
        return True

    except Exception as e:
        _log(f"Supabase push failed (non-blocking): {e}")
        return False


# ─────────────────────────────────────────────────────────
# Single-order quality record
# ─────────────────────────────────────────────────────────

def record_execution(order: dict) -> dict:
    """Record execution quality for a single completed order.

    Called by OMS after each fill.
    """
    expected = order.get("price", 0)
    realized = order.get("avg_fill_price", 0)
    side = order.get("side", "BUY")

    eq = {
        "client_order_id": order.get("client_order_id"),
        "symbol": order.get("symbol"),
        "side": side,
        "strategy": order.get("strategy"),
        "exchange": order.get("exchange"),
        "expected_price": expected,
        "realized_price": realized,
        "slippage_bps": calc_slippage(expected, realized, side) if expected > 0 and realized > 0 else 0,
        "latency_ms": calc_latency_ms(
            order.get("submitted_at", ""),
            order.get("filled_at", ""),
        ),
        "cost": calc_trade_cost(order),
        "state": order.get("state"),
        "recorded_at": _now().isoformat(),
    }

    return eq


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def run():
    _log("=== EXECUTION QUALITY ANALYSIS ===")

    result = analyze_all_orders()

    print(f"  Orders: {result['total_orders']} total, {result['filled']} filled, "
          f"{result.get('rejected', 0)} rejected, {result.get('canceled', 0)} canceled")
    print(f"  Fill rate: {result['fill_rate_pct']}%")

    if result.get("slippage"):
        s = result["slippage"]
        print(f"  Slippage: mean={s['mean_bps']}bps, median={s['median_bps']}bps, max={s['max_bps']}bps")

    if result.get("latency"):
        l = result["latency"]
        print(f"  Latency: p50={l['p50_ms']}ms, p95={l['p95_ms']}ms")

    if result.get("costs"):
        c = result["costs"]
        print(f"  Costs: ${c['total_cost_usd']} total ({c['avg_cost_bps']}bps avg) on ${c['total_notional_usd']} notional")

    # Push to Supabase (4.3.4)
    push_to_supabase(result)

    _log("=== ANALYSIS COMPLETE ===")
    return result


if __name__ == "__main__":
    run()
