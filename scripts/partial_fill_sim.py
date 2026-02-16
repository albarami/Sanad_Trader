#!/usr/bin/env python3
"""
Partial Fill Simulation — Sprint 11.1.4
Simulates realistic partial fills for paper trading.

In real markets, limit orders don't always fill completely.
This module simulates fill probability based on:
- Order size vs available liquidity
- Time in force
- Market volatility
- Order book depth
"""

import json
import random
import math
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"


def simulate_fill(order_size_usd: float, liquidity_usd: float = 0,
                  volatility: str = "NORMAL", order_type: str = "LIMIT") -> dict:
    """
    Simulate fill for a paper trade order.

    Returns:
        {
            "fill_pct": 0.0-1.0,
            "filled_qty_pct": float,
            "fill_price_impact_bps": float,
            "partial": bool,
            "reason": str,
        }
    """
    # Base fill probability by order type
    if order_type == "MARKET":
        base_fill = 1.0  # Markets always fill (with slippage)
    else:
        base_fill = 0.92  # Limits fill ~92% of the time

    # Liquidity adjustment
    if liquidity_usd > 0:
        size_ratio = order_size_usd / liquidity_usd
        if size_ratio > 0.1:
            # Large order relative to liquidity — lower fill probability
            base_fill *= max(0.3, 1.0 - size_ratio * 2)
        elif size_ratio > 0.01:
            base_fill *= 0.95
    else:
        # No liquidity data — assume moderate fill
        base_fill *= 0.85

    # Volatility adjustment
    vol_mult = {
        "LOW": 1.05,      # Calm market — better fills
        "NORMAL": 1.0,
        "HIGH": 0.85,     # Volatile — worse fills
        "EXTREME": 0.65,  # Flash crash — poor fills
    }
    base_fill *= vol_mult.get(volatility, 1.0)

    # Clamp
    base_fill = min(1.0, max(0.0, base_fill))

    # Roll the dice
    roll = random.random()

    if roll <= base_fill:
        # Full fill
        fill_pct = 1.0
        reason = "full_fill"
    elif roll <= base_fill + 0.05:
        # Partial fill (random 30-90%)
        fill_pct = random.uniform(0.3, 0.9)
        reason = "partial_fill"
    else:
        # No fill (order expired or moved away)
        fill_pct = 0.0
        reason = "no_fill"

    # Price impact (slippage from order book depth)
    if fill_pct > 0:
        base_impact = 5  # 5 bps baseline
        if liquidity_usd > 0:
            size_impact = (order_size_usd / liquidity_usd) * 100  # bps
        else:
            size_impact = 10  # Unknown liquidity
        price_impact = base_impact + size_impact * fill_pct
    else:
        price_impact = 0

    return {
        "fill_pct": round(fill_pct, 4),
        "filled_qty_pct": round(fill_pct * 100, 1),
        "fill_price_impact_bps": round(price_impact, 1),
        "partial": 0 < fill_pct < 1,
        "reason": reason,
        "order_size_usd": order_size_usd,
        "liquidity_usd": liquidity_usd,
        "volatility": volatility,
    }


if __name__ == "__main__":
    print("=== Partial Fill Simulation Test ===\n")

    scenarios = [
        {"name": "Small order, good liquidity", "size": 100, "liq": 1000000, "vol": "NORMAL"},
        {"name": "Medium order, moderate liq", "size": 500, "liq": 50000, "vol": "NORMAL"},
        {"name": "Large order, thin liq", "size": 1000, "liq": 5000, "vol": "HIGH"},
        {"name": "Market order, volatile", "size": 200, "liq": 100000, "vol": "EXTREME"},
    ]

    for s in scenarios:
        # Run 100 simulations
        fills = [simulate_fill(s["size"], s["liq"], s["vol"]) for _ in range(100)]
        full = sum(1 for f in fills if f["fill_pct"] == 1.0)
        partial = sum(1 for f in fills if f["partial"])
        none = sum(1 for f in fills if f["fill_pct"] == 0.0)
        avg_impact = sum(f["fill_price_impact_bps"] for f in fills) / 100

        print(f"  {s['name']}:")
        print(f"    Full: {full}% | Partial: {partial}% | None: {none}% | Avg impact: {avg_impact:.1f}bps")

    print("\n✅ Partial fill simulation working")
