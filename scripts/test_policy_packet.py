#!/usr/bin/env python3
"""Test that build_policy_packet produces correct schema."""

import sys
sys.path.insert(0, '.')

from scripts.fast_decision_engine import build_policy_packet
from datetime import datetime, timezone
import json

# Minimal signal
signal = {
    "signal_id": "test123",
    "token_address": "TEST_TOKEN",
    "token": "TEST",
    "symbol": "TEST",
    "chain": "solana",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "deployment_timestamp": "2024-01-01T00:00:00Z",
    "rugcheck_score": 75,
    "volume_24h": 5000000,
    "liquidity_usd": 1000000,
    "cross_sources": ["birdeye", "dexscreener"],
    "onchain_evidence": {
        "rugpull_scan": {"flags": []},
        "holder_analysis": {"sybil_risk": "LOW"}
    }
}

strategy_data = {"strategy_id": "test_strategy"}
runtime_state = {"regime_tag": "NEUTRAL"}
now_iso = datetime.now(timezone.utc).isoformat()

# Build packet
packet = build_policy_packet(signal, strategy_data, 1.23, runtime_state, now_iso)

print("✅ Policy Packet Schema Check")
print("=" * 60)
print(f"Top-level keys: {list(packet.keys())}")
print()

# Check required fields for Gates 3-5
print("Required fields:")
print(f"  token: {packet.get('token')}")
print(f"  data_timestamps: {packet.get('data_timestamps')}")
print(f"  api_responses: {list(packet.get('api_responses', {}).keys())}")
print(f"  sanad_verification: {list(packet.get('sanad_verification', {}).keys())}")
print(f"  market_data: {list(packet.get('market_data', {}).keys())}")
print()

# Check Gate 3 requirements
dt = packet.get("data_timestamps", {})
api = packet.get("api_responses", {})
print("Gate 3 Requirements:")
print(f"  ✓ price_timestamp present: {bool(dt.get('price_timestamp'))}")
print(f"  ✓ api_responses non-empty: {len(api) > 0}")
print()

# Check Gate 5 requirements
sanad = packet.get("sanad_verification", {})
print("Gate 5 Requirements:")
print(f"  ✓ rugpull_flags present: {'rugpull_flags' in sanad}")
print(f"  ✓ sybil_risk present: {'sybil_risk' in sanad}")
print()

print("Full packet:")
print(json.dumps(packet, indent=2, default=str))
