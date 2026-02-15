#!/usr/bin/env python3
"""
Sanad Trader v3.0 — Policy Engine Gate Tests

Tests each of the 15 gates individually.
Run: python3 test_policy_engine.py
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Paths
TEST_BASE = Path("/data/.openclaw/workspace/trading")
STATE_DIR = TEST_BASE / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(TEST_BASE / "scripts"))
import policy_engine


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def fresh_ts(minutes_ago=0):
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat()


def write_state(filename, data):
    path = STATE_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f)


def make_clean_state():
    write_state("portfolio.json", {
        "mode": "PAPER",
        "starting_balance_usd": 10000.00,
        "current_balance_usd": 10000.00,
        "daily_pnl_pct": 0.0,
        "current_drawdown_pct": 0.0,
        "peak_balance_usd": 10000.00,
        "open_position_count": 0,
        "meme_allocation_pct": 0.0,
        "total_exposure_pct": 0.0,
        "token_exposure_pct": {},
        "updated_at": now_iso()
    })
    write_state("reconciliation.json", {
        "last_reconciliation_timestamp": fresh_ts(5),
        "has_mismatch": False,
        "mismatch_details": None
    })
    write_state("exchange_health.json", {
        "binance": {"error_rate_pct": 0.0, "websocket_connected": True},
        "mexc": {"error_rate_pct": 0.0, "websocket_connected": True}
    })
    write_state("circuit_breakers.json", {
        "binance_api": {"state": "closed", "failure_count": 0},
        "mexc_api": {"state": "closed", "failure_count": 0},
        "helius_rpc": {"state": "closed", "failure_count": 0},
        "bubblemaps_api": {"state": "closed", "failure_count": 0},
        "perplexity_api": {"state": "closed", "failure_count": 0},
        "debate_agents": {"state": "closed", "failure_count": 0},
        "anthropic_api": {"state": "closed", "failure_count": 0}
    })
    write_state("trade_history.json", {"trades": []})
    write_state("budget.json", {
        "daily_llm_spend_usd": 0.0,
        "monthly_llm_spend_usd": 0.0
    })
    kill_path = TEST_BASE / "config" / "kill_switch.flag"
    kill_path.parent.mkdir(parents=True, exist_ok=True)
    kill_path.write_text("FALSE")


def make_passing_packet():
    return {
        "correlation_id": "test-001",
        "token": {
            "symbol": "TESTCOIN",
            "chain": "ethereum",
            "contract_address": "0xabc123",
            "deployment_timestamp": fresh_ts(minutes_ago=120)
        },
        "venue": "CEX",
        "exchange": "binance",
        "strategy_name": "meme-momentum",
        "data_timestamps": {
            "price_timestamp": fresh_ts(1),
            "onchain_timestamp": fresh_ts(10)
        },
        "api_responses": {
            "binance": {"price": 0.05},
            "coingecko": {"price": 0.0501}
        },
        "sanad_verification": {
            "sanad_trust_score": 82,
            "sanad_grade": "Mashhur",
            "rugpull_flags": [],
            "sybil_risk": "low"
        },
        "market_data": {
            "estimated_slippage_bps": 50,
            "depth_sufficient": True,
            "spread_bps": 80,
            "price_change_pct_window": 0.08
        },
        "has_verified_catalyst": False,
        "trade_intent": {
            "direction": "BUY",
            "entry_price": 0.05,
            "stop_loss": 0.0425,
            "take_profit": 0.10,
            "position_size_pct": 0.02
        },
        "trade_confidence_score": 72,
        "almuhasbi_verdict": "APPROVE",
        "estimated_trade_cost_usd": 0.50,
        "preflight_simulation": {}
    }


passed = 0
failed = 0
total = 0


def test(name, expect_result, expect_gate_failed=None, modify_state=None, modify_packet=None):
    global passed, failed, total
    total += 1

    make_clean_state()
    if modify_state:
        modify_state()

    packet = make_passing_packet()
    if modify_packet:
        modify_packet(packet)

    result = policy_engine.evaluate_gates(packet)

    ok = result["result"] == expect_result
    if expect_gate_failed is not None:
        ok = ok and result["gate_failed"] == expect_gate_failed

    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1

    gate_info = ""
    if result["result"] == "BLOCK":
        gate_info = f" [Gate {result['gate_failed']}: {result['gate_failed_name']} — {result['gate_evidence']}]"

    print(f"  {status}: {name} -> {result['result']}{gate_info}")
    if not ok:
        print(f"    EXPECTED: result={expect_result}, gate_failed={expect_gate_failed}")
        print(f"    GOT:      result={result['result']}, gate_failed={result['gate_failed']}")


print("=" * 70)
print("SANAD TRADER v3.0 — POLICY ENGINE GATE TESTS")
print("=" * 70)

# FULL PASS
print("\n [1] FULL PASS TEST")
test("Clean state, valid packet -> PASS", "PASS")

# GATE 1
print("\n [2] GATE 1: Kill Switch")
def kill_on():
    (TEST_BASE / "config" / "kill_switch.flag").write_text("TRUE")
test("Kill switch TRUE -> BLOCK", "BLOCK", 1, modify_state=kill_on)
test("Kill switch FALSE -> PASS gate 1", "PASS")

# GATE 2
print("\n [3] GATE 2: Capital Preservation")
def daily_loss_hit():
    write_state("portfolio.json", {
        "daily_pnl_pct": -0.06,
        "current_drawdown_pct": 0.10,
        "open_position_count": 0,
        "meme_allocation_pct": 0.0,
        "token_exposure_pct": {}
    })
def max_dd_hit():
    write_state("portfolio.json", {
        "daily_pnl_pct": -0.01,
        "current_drawdown_pct": 0.16,
        "open_position_count": 0,
        "meme_allocation_pct": 0.0,
        "token_exposure_pct": {}
    })
test("Daily loss 6% -> BLOCK", "BLOCK", 2, modify_state=daily_loss_hit)
test("Max drawdown 16% -> BLOCK", "BLOCK", 2, modify_state=max_dd_hit)

# GATE 3
print("\n [4] GATE 3: Data Freshness")
def stale_price(pkt):
    pkt["data_timestamps"]["price_timestamp"] = fresh_ts(minutes_ago=10)
def missing_price(pkt):
    pkt["data_timestamps"]["price_timestamp"] = None
test("Price 10min old -> BLOCK", "BLOCK", 3, modify_packet=stale_price)
test("Price timestamp None -> BLOCK", "BLOCK", 3, modify_packet=missing_price)

# GATE 4
print("\n [5] GATE 4: Token Age")
def young_token(pkt):
    pkt["token"]["deployment_timestamp"] = fresh_ts(minutes_ago=30)
def young_token_early_launch(pkt):
    pkt["token"]["deployment_timestamp"] = fresh_ts(minutes_ago=30)
    pkt["strategy_name"] = "early-launch"
test("Token 30min old -> BLOCK", "BLOCK", 4, modify_packet=young_token)
test("Token 30min old + early-launch strategy -> PASS", "PASS", modify_packet=young_token_early_launch)

# GATE 5
print("\n [6] GATE 5: Rugpull Safety")
def rugpull_flags(pkt):
    pkt["sanad_verification"]["rugpull_flags"] = ["mint_authority_active", "freeze_authority_active"]
test("Rugpull flags -> BLOCK", "BLOCK", 5, modify_packet=rugpull_flags)

# GATE 6
print("\n [7] GATE 6: Liquidity Gate")
def high_slippage(pkt):
    pkt["market_data"]["estimated_slippage_bps"] = 400
test("Slippage 400bps -> BLOCK", "BLOCK", 6, modify_packet=high_slippage)

# GATE 7
print("\n [8] GATE 7: Spread Gate (CEX)")
def wide_spread(pkt):
    pkt["market_data"]["spread_bps"] = 250
def dex_trade(pkt):
    pkt["venue"] = "DEX"
    pkt["market_data"]["spread_bps"] = 250
    pkt["preflight_simulation"] = {"sell_simulation_success": True, "tokens_returned": 100}
test("Spread 250bps on CEX -> BLOCK", "BLOCK", 7, modify_packet=wide_spread)
test("Wide spread on DEX -> PASS (gate skipped)", "PASS", modify_packet=dex_trade)

# GATE 8
print("\n [9] GATE 8: Pre-Flight Simulation (DEX)")
def dex_honeypot(pkt):
    pkt["venue"] = "DEX"
    pkt["preflight_simulation"] = {"sell_simulation_success": False, "error": "tx reverted"}
def dex_clean(pkt):
    pkt["venue"] = "DEX"
    pkt["preflight_simulation"] = {"sell_simulation_success": True, "tokens_returned": 100}
test("DEX sim reverts -> BLOCK", "BLOCK", 8, modify_packet=dex_honeypot)
test("DEX sim passes -> PASS", "PASS", modify_packet=dex_clean)

# GATE 9
print("\n [10] GATE 9: Volatility Halt")
def extreme_vol(pkt):
    pkt["market_data"]["price_change_pct_window"] = 0.30
def extreme_vol_catalyst(pkt):
    pkt["market_data"]["price_change_pct_window"] = 0.30
    pkt["has_verified_catalyst"] = True
test("30% move, no catalyst -> BLOCK", "BLOCK", 9, modify_packet=extreme_vol)
test("30% move + verified catalyst -> PASS", "PASS", modify_packet=extreme_vol_catalyst)

# GATE 10
print("\n [11] GATE 10: Exchange Health")
def exchange_errors():
    write_state("exchange_health.json", {
        "binance": {"error_rate_pct": 0.08, "websocket_connected": True}
    })
def ws_dropped():
    write_state("exchange_health.json", {
        "binance": {"error_rate_pct": 0.01, "websocket_connected": False}
    })
test("Exchange error rate 8% -> BLOCK", "BLOCK", 10, modify_state=exchange_errors)
test("WebSocket dropped -> BLOCK", "BLOCK", 10, modify_state=ws_dropped)

# GATE 11
print("\n [12] GATE 11: Reconciliation")
def stale_recon():
    write_state("reconciliation.json", {
        "last_reconciliation_timestamp": fresh_ts(minutes_ago=20),
        "has_mismatch": False
    })
def recon_mismatch():
    write_state("reconciliation.json", {
        "last_reconciliation_timestamp": fresh_ts(5),
        "has_mismatch": True,
        "mismatch_details": "binance: expected 0 open, found 1"
    })
test("Reconciliation 20min stale -> BLOCK", "BLOCK", 11, modify_state=stale_recon)
test("Reconciliation mismatch -> BLOCK", "BLOCK", 11, modify_state=recon_mismatch)

# GATE 12
print("\n [13] GATE 12: Exposure Limits")
def max_positions():
    write_state("portfolio.json", {
        "daily_pnl_pct": 0.0,
        "current_drawdown_pct": 0.0,
        "open_position_count": 3,
        "meme_allocation_pct": 0.10,
        "token_exposure_pct": {}
    })
def max_meme():
    write_state("portfolio.json", {
        "daily_pnl_pct": 0.0,
        "current_drawdown_pct": 0.0,
        "open_position_count": 1,
        "meme_allocation_pct": 0.29,
        "token_exposure_pct": {}
    })
test("3 open positions -> BLOCK", "BLOCK", 12, modify_state=max_positions)
test("Meme at 29% + 2% new = 31% -> BLOCK", "BLOCK", 12, modify_state=max_meme)

# GATE 13
print("\n [14] GATE 13: Cooldown")
def recent_trade():
    write_state("trade_history.json", {
        "trades": [{"token": "TESTCOIN", "timestamp": fresh_ts(minutes_ago=60)}]
    })
def old_trade():
    write_state("trade_history.json", {
        "trades": [{"token": "TESTCOIN", "timestamp": fresh_ts(minutes_ago=150)}]
    })
test("Same token 60min ago -> BLOCK", "BLOCK", 13, modify_state=recent_trade)
test("Same token 150min ago -> PASS", "PASS", modify_state=old_trade)

# GATE 14
print("\n [15] GATE 14: Budget Gate")
def budget_blown():
    write_state("budget.json", {"daily_llm_spend_usd": 16.0, "monthly_llm_spend_usd": 100.0})
test("Daily LLM spend $16 -> BLOCK", "BLOCK", 14, modify_state=budget_blown)

# GATE 15
print("\n [16] GATE 15: Sanad + Audit")
def low_trust(pkt):
    pkt["sanad_verification"]["sanad_trust_score"] = 65
def low_confidence(pkt):
    pkt["trade_confidence_score"] = 55
def muhasbi_reject(pkt):
    pkt["almuhasbi_verdict"] = "REJECT"
test("Trust score 65 -> BLOCK", "BLOCK", 15, modify_packet=low_trust)
test("Confidence score 55 -> BLOCK", "BLOCK", 15, modify_packet=low_confidence)
test("Al-Muhasbi REJECT -> BLOCK", "BLOCK", 15, modify_packet=muhasbi_reject)

# CIRCUIT BREAKERS
print("\n [17] CIRCUIT BREAKERS")
def triple_trip():
    write_state("circuit_breakers.json", {
        "binance_api": {"state": "open", "failure_count": 5},
        "mexc_api": {"state": "open", "failure_count": 5},
        "helius_rpc": {"state": "open", "failure_count": 3},
        "bubblemaps_api": {"state": "closed", "failure_count": 0},
        "perplexity_api": {"state": "closed", "failure_count": 0},
        "debate_agents": {"state": "closed", "failure_count": 0},
        "anthropic_api": {"state": "closed", "failure_count": 0}
    })
test("3 circuit breakers tripped -> BLOCK", "BLOCK", 0, modify_state=triple_trip)

# SUMMARY
print("\n" + "=" * 70)
print(f"RESULTS: {passed}/{total} passed, {failed}/{total} failed")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print(f"WARNING: {failed} TESTS FAILED")
print("=" * 70)
