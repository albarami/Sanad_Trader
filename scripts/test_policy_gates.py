#!/usr/bin/env python3
"""Test that policy_engine can pass Gates 1-14 with proper packet."""

import sys
sys.path.insert(0, '.')

from scripts import policy_engine
from scripts.fast_decision_engine import build_policy_packet
from datetime import datetime, timezone, timedelta

# Minimal signal
signal = {
    "signal_id": "test123",
    "token_address": "SOL",
    "token": "SOL",
    "symbol": "SOL",
    "chain": "solana",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "deployment_timestamp": "2020-03-16T00:00:00Z",  # SOL launch date
    "rugcheck_score": 95,
    "volume_24h": 50000000,
    "liquidity_usd": 10000000,
    "cross_sources": ["binance", "coinbase"],
    "onchain_evidence": {
        "rugpull_scan": {"flags": []},
        "holder_analysis": {"sybil_risk": "LOW"}
    }
}

strategy_data = {"strategy_id": "test_strategy"}
runtime_state = {"regime_tag": "NEUTRAL"}
now_iso = datetime.now(timezone.utc).isoformat()

# Build packet
packet = build_policy_packet(signal, strategy_data, 100.0, runtime_state, now_iso)

# Override state to avoid file dependencies
state_override = {
    "portfolio": {
        "cash_balance_usd": 10000,
        "open_position_count": 0,
        "total_exposure_pct": 0,
        "mode": "paper",
        "daily_pnl_pct": 0,
        "current_drawdown_pct": 0,
        "daily_trades": 0
    },
    "trade_history": [],
    "reconciliation": {
        "last_reconciliation_timestamp": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        "deviations": []
    },
    "budget": {
        "daily_budget_usd": 1000,
        "daily_spent_usd": 0
    }
}

print("Testing policy_engine.evaluate_gates() with complete packet...")
print("=" * 60)

result = policy_engine.evaluate_gates(
    packet,
    gate_range=(1, 14),
    state_override=state_override
)

print(f"Result: {result['result']}")
print(f"Gates passed: {result['gates_passed']}")

if result["result"] != "PASS":
    print(f"\n❌ Gate {result['gate_failed']}: {result['gate_failed_name']}")
    print(f"Evidence: {result['gate_evidence']}")
    sys.exit(1)
else:
    print("\n✅ All gates 1-14 PASSED")
    sys.exit(0)
