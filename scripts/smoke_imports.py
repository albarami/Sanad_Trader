#!/usr/bin/env python3
"""
Smoke test: Import every architectural symbol from v3.0 Knowledge Base.
Run after any refactor to catch import regressions immediately.

Usage: python3 smoke_imports.py
"""
import sys

PASS = 0
FAIL = 0

def check(label, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  ✅ {label}")
        PASS += 1
    except Exception as e:
        print(f"  ❌ {label}: {e}")
        FAIL += 1

def assert_len(lst, expected):
    assert len(lst) == expected, f"Expected {expected}, got {len(lst)}"

def assert_eq(actual, expected):
    assert actual == expected, f"Expected {expected}, got {actual}"

print("=== Sanad v3.0 Smoke Import Test ===\n")

# ── token_profile.py ──
print("token_profile.py:")
check("TokenProfile class", lambda: __import__('token_profile').TokenProfile)
check("classify_asset()", lambda: __import__('token_profile').classify_asset)
check("meme_safety_gate()", lambda: __import__('token_profile').meme_safety_gate)
check("get_eligible_strategies()", lambda: __import__('token_profile').get_eligible_strategies)
check("TIER_MAP", lambda: __import__('token_profile').TIER_MAP)
check("STRATEGY_CONSTRAINTS", lambda: __import__('token_profile').STRATEGY_CONSTRAINTS)
check("lint_prompt()", lambda: __import__('token_profile').lint_prompt)
check("validate_evidence()", lambda: __import__('token_profile').validate_evidence)
check("REQUIRED_EVIDENCE", lambda: __import__('token_profile').REQUIRED_EVIDENCE)
check("PRE_TRADE_MUHASABA", lambda: __import__('token_profile').PRE_TRADE_MUHASABA)
check("POST_TRADE_REASON_CODES", lambda: __import__('token_profile').POST_TRADE_REASON_CODES)
check("TokenProfile.market_cap_usd alias", lambda: __import__('token_profile').TokenProfile(symbol='X', market_cap=1).market_cap_usd)
check("TokenProfile.from_dict(market_cap_usd=...)", lambda: assert_eq(
    __import__('token_profile').TokenProfile.from_dict({'symbol': 'BTC', 'market_cap_usd': 100}).market_cap, 100))

# ── tier_prompts.py ──
print("\ntier_prompts.py:")
check("get_bull_prompt()", lambda: __import__('tier_prompts').get_bull_prompt)
check("get_bear_prompt()", lambda: __import__('tier_prompts').get_bear_prompt)
check("TIER_1_BULL_PROMPT", lambda: __import__('tier_prompts').TIER_1_BULL_PROMPT)
check("TIER_3_BEAR_PROMPT", lambda: __import__('tier_prompts').TIER_3_BEAR_PROMPT)
check("WHALE_BULL_PROMPT", lambda: __import__('tier_prompts').WHALE_BULL_PROMPT)
# Re-exports from token_profile
check("re-export: lint_prompt", lambda: __import__('tier_prompts').lint_prompt)
check("re-export: REQUIRED_EVIDENCE", lambda: __import__('tier_prompts').REQUIRED_EVIDENCE)
check("re-export: PRE_TRADE_MUHASABA", lambda: __import__('tier_prompts').PRE_TRADE_MUHASABA)
check("re-export: POST_TRADE_REASON_CODES", lambda: __import__('tier_prompts').POST_TRADE_REASON_CODES)

# ── vector_db.py ──
print("\nvector_db.py:")
check("get_collection()", lambda: __import__('vector_db').get_collection)
check("get_rag_context()", lambda: __import__('vector_db').get_rag_context)
check("load_expert_knowledge()", lambda: __import__('vector_db').load_expert_knowledge)
check("EXPERT_KNOWLEDGE (12 entries)", lambda: assert_len(__import__('vector_db').EXPERT_KNOWLEDGE, 12))
check("index_all_trades()", lambda: __import__('vector_db').index_all_trades)
check("index_post_mortems()", lambda: __import__('vector_db').index_post_mortems)
check("query_similar()", lambda: __import__('vector_db').query_similar)
check("query_regime_weighted()", lambda: __import__('vector_db').query_regime_weighted)

# ── thompson_sampler.py ──
print("\nthompson_sampler.py:")
check("select_strategy()", lambda: __import__('thompson_sampler').select_strategy)
check("THOMPSON_STRATEGIES", lambda: __import__('thompson_sampler').THOMPSON_STRATEGIES)
check("STRATEGY_REGISTRY (compat alias)", lambda: __import__('thompson_sampler').STRATEGY_REGISTRY)
check("record_outcome()", lambda: __import__('thompson_sampler').record_outcome)

# ── strategy_registry.py ──
print("\nstrategy_registry.py:")
check("STRATEGIES", lambda: __import__('strategy_registry').STRATEGIES)
check("get_active_strategies()", lambda: __import__('strategy_registry').get_active_strategies)
check("match_signal_to_strategies()", lambda: __import__('strategy_registry').match_signal_to_strategies)

# ── policy_engine.py ──
print("\npolicy_engine.py:")
check("evaluate_gates()", lambda: __import__('policy_engine').evaluate_gates)
check("check_circuit_breakers()", lambda: __import__('policy_engine').check_circuit_breakers)

# ── sanad_pipeline.py ──
print("\nsanad_pipeline.py:")
check("module imports cleanly", lambda: __import__('sanad_pipeline'))

# ── Classification correctness ──
print("\nClassification tests:")
from token_profile import TokenProfile, classify_asset

def test_btc():
    p = TokenProfile(symbol='BTC', market_cap=1_300_000_000_000, coingecko_categories=['Layer 1'], cex_listed=True, cex_names=['Binance'])
    assert classify_asset(p) == "TIER_1_MACRO", f"Got {classify_asset(p)}"
check("BTC ($1.3T) → TIER_1_MACRO", test_btc)

def test_pepe():
    p = TokenProfile(symbol='PEPE', market_cap=3_000_000_000, coingecko_categories=['Meme'], cex_listed=True, cex_names=['Binance'])
    assert classify_asset(p) == "TIER_3_MEME_CEX", f"Got {classify_asset(p)}"
check("PEPE ($3B, Meme, CEX) → TIER_3_MEME_CEX", test_pepe)

def test_link():
    p = TokenProfile(symbol='LINK', market_cap=8_000_000_000, coingecko_categories=['Oracle'], cex_listed=True, cex_names=['Binance'])
    assert classify_asset(p) == "TIER_2_ALT_LARGE", f"Got {classify_asset(p)}"
check("LINK ($8B, Oracle) → TIER_2_ALT_LARGE", test_link)

def test_scam():
    from token_profile import meme_safety_gate
    p = TokenProfile(symbol='SCAMDOG', market_cap=500_000, security_flags=['mint_active'], dex_only=True, liquidity_usd=10_000)
    p.asset_tier = classify_asset(p)
    safe, _ = meme_safety_gate(p)
    assert not safe, "Should be blocked by safety gate"
check("SCAMDOG (mint_active) → safety gate BLOCKED", test_scam)

def test_doge_tier1():
    p = TokenProfile(symbol='DOGE', market_cap=25_000_000_000, coingecko_categories=['Meme'], cex_listed=True, cex_names=['Binance'])
    assert classify_asset(p) == "TIER_1_MACRO", f"Got {classify_asset(p)}"
check("DOGE ($25B, Meme) → TIER_1_MACRO (MC overrides meme)", test_doge_tier1)




# ── Summary ──
print(f"\n{'='*40}")
print(f"PASSED: {PASS}  |  FAILED: {FAIL}")
if FAIL > 0:
    print("⛔ SMOKE TEST FAILED")
    sys.exit(1)
else:
    print("✅ ALL IMPORTS AND CLASSIFICATIONS VERIFIED")
    sys.exit(0)
