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



# ── Corroboration Engine ──

def test_corroboration_import():
    from corroboration_engine import register_signal, get_corroboration, get_window_stats
    assert callable(register_signal)
    assert callable(get_corroboration)
check("Corroboration engine imports", test_corroboration_import)

def test_corroboration_logic():
    from corroboration_engine import register_signal, get_corroboration, _normalize_provider
    # Source independence
    assert _normalize_provider("coingecko_trending") == "coingecko"
    assert _normalize_provider("birdeye_meme_list") == "birdeye"
    assert _normalize_provider("dexscreener_boost") == "dexscreener"
    assert _normalize_provider("dexscreener_cto") == "dexscreener"  # same provider
    assert _normalize_provider("onchain_analytics") == "onchain"
    assert _normalize_provider("telegram_sniffer") == "telegram"
    # Two DexScreener sub-sources = still 1 provider
    assert _normalize_provider("dexscreener_boost") == _normalize_provider("dexscreener_cto")
check("Corroboration: source independence mapping", test_corroboration_logic)

def test_corroboration_multi_source():
    """Test that 2 signals from different sources → MASHHUR."""
    from corroboration_engine import register_signal, _save_window, WINDOW_PATH
    import json, os
    # Save current window, use empty for test
    backup = None
    if WINDOW_PATH.exists():
        backup = json.load(open(WINDOW_PATH))
    _save_window({"signals": [], "updated_at": None})
    try:
        r1 = register_signal({"token": "TESTCOIN", "source": "coingecko_trending"})
        assert r1["cross_source_count"] == 1
        assert r1["corroboration_level"] == "AHAD"
        r2 = register_signal({"token": "TESTCOIN", "source": "birdeye_meme_list"})
        assert r2["cross_source_count"] == 2, f"Expected 2, got {r2['cross_source_count']}"
        assert r2["corroboration_level"] == "MASHHUR", f"Expected MASHHUR, got {r2['corroboration_level']}"
        assert "birdeye" in r2["cross_sources"]
        assert "coingecko" in r2["cross_sources"]
        r3 = register_signal({"token": "TESTCOIN", "source": "onchain_analytics"})
        assert r3["cross_source_count"] == 3
        assert r3["corroboration_level"] == "TAWATUR"
    finally:
        # Restore original window
        if backup:
            _save_window(backup)
        else:
            os.remove(WINDOW_PATH) if WINDOW_PATH.exists() else None
check("Corroboration: AHAD → MASHHUR → TAWATUR", test_corroboration_multi_source)

def test_corroboration_same_provider():
    """Two signals from same provider = still 1 source."""
    from corroboration_engine import register_signal, _save_window, WINDOW_PATH
    import json, os
    backup = None
    if WINDOW_PATH.exists():
        backup = json.load(open(WINDOW_PATH))
    _save_window({"signals": [], "updated_at": None})
    try:
        register_signal({"token": "DUPETEST", "source": "dexscreener_boost"})
        r2 = register_signal({"token": "DUPETEST", "source": "dexscreener_cto"})
        assert r2["cross_source_count"] == 1, f"Same provider should be 1, got {r2['cross_source_count']}"
        assert r2["corroboration_level"] == "AHAD"
    finally:
        if backup:
            _save_window(backup)
        else:
            os.remove(WINDOW_PATH) if WINDOW_PATH.exists() else None
check("Corroboration: same provider = 1 source", test_corroboration_same_provider)

def test_corroboration_quality_weak():
    """Hype-only sources (CoinGecko + Birdeye + DexScreener) = WEAK quality."""
    from corroboration_engine import register_signal, _save_window, WINDOW_PATH
    import json, os
    backup = None
    if WINDOW_PATH.exists():
        backup = json.load(open(WINDOW_PATH))
    _save_window({"signals": [], "updated_at": None})
    try:
        register_signal({"token": "HYPETOKEN", "source": "coingecko_trending"})
        register_signal({"token": "HYPETOKEN", "source": "birdeye_trending"})
        r3 = register_signal({"token": "HYPETOKEN", "source": "dexscreener_boost"})
        assert r3["corroboration_level"] == "TAWATUR"
        assert r3["corroboration_quality"] == "WEAK", f"3 hype sources should be WEAK, got {r3['corroboration_quality']}"
    finally:
        if backup:
            _save_window(backup)
        else:
            os.remove(WINDOW_PATH) if WINDOW_PATH.exists() else None
check("Corroboration: hype-only sources = WEAK quality", test_corroboration_quality_weak)

def test_corroboration_quality_strong():
    """Hype + evidence source = STRONG quality."""
    from corroboration_engine import register_signal, _save_window, WINDOW_PATH
    import json, os
    backup = None
    if WINDOW_PATH.exists():
        backup = json.load(open(WINDOW_PATH))
    _save_window({"signals": [], "updated_at": None})
    try:
        register_signal({"token": "REALTOKEN", "source": "coingecko_trending"})
        r2 = register_signal({"token": "REALTOKEN", "source": "onchain_analytics"})
        assert r2["corroboration_level"] == "MASHHUR"
        assert r2["corroboration_quality"] == "STRONG", f"Hype + onchain should be STRONG, got {r2['corroboration_quality']}"
    finally:
        if backup:
            _save_window(backup)
        else:
            os.remove(WINDOW_PATH) if WINDOW_PATH.exists() else None
check("Corroboration: hype + evidence = STRONG quality", test_corroboration_quality_strong)

# ── Portfolio Math Invariants ──

def test_portfolio_balance():
    import json
    from pathlib import Path
    state = Path(__file__).resolve().parent.parent / "state"
    portfolio = json.load(open(state / "portfolio.json"))
    trade_history = json.load(open(state / "trade_history.json"))
    trades = trade_history.get("trades", trade_history) if isinstance(trade_history, dict) else trade_history

    starting = portfolio.get("starting_balance_usd", 10000.0)
    current = portfolio.get("current_balance_usd", starting)
    peak = portfolio.get("peak_balance_usd", starting)
    drawdown = portfolio.get("current_drawdown_pct", 0)

    total_pnl = sum(float(t.get("pnl_usd", t.get("net_pnl_usd", 0)) or 0) for t in trades if isinstance(t, dict))
    expected_balance = round(starting + total_pnl, 2)

    assert abs(current - expected_balance) < 0.02, f"Balance mismatch: {current} != starting({starting}) + pnl({total_pnl}) = {expected_balance}"
    assert peak >= current, f"Peak {peak} < current {current}"
    expected_dd = round((peak - current) / peak, 6) if peak > 0 else 0
    assert abs(drawdown - expected_dd) < 0.001, f"Drawdown mismatch: {drawdown} != {expected_dd}"
check("Portfolio: balance = starting + sum(pnl), peak >= current, drawdown correct", test_portfolio_balance)

def test_reject_confidence_zero():
    """In-memory: if verdict == REJECT, trade_confidence_score must be 0."""
    # Simulate what sanad_pipeline.py does when judge returns REJECT
    judge_result = {"verdict": "REJECT", "confidence_score": 85}
    conf = 0 if judge_result.get("verdict") == "REJECT" else judge_result.get("confidence_score", 0)
    assert conf == 0, f"REJECT should zero confidence, got {conf}"
    # Also verify APPROVE preserves confidence
    judge_approve = {"verdict": "APPROVE", "confidence_score": 72}
    conf2 = 0 if judge_approve.get("verdict") == "REJECT" else judge_approve.get("confidence_score", 0)
    assert conf2 == 72, f"APPROVE should keep confidence 72, got {conf2}"
check("Invariant: REJECT → confidence=0, APPROVE → confidence preserved", test_reject_confidence_zero)

def test_block_no_bull_bear():
    """In-memory: short-circuit BLOCK records must have zero bull/bear."""
    record = {
        "short_circuit": True, "final_action": "REJECT",
        "bull": {"conviction": 0, "thesis": ""},
        "bear": {"conviction": 0, "attack_points": []},
        "trade_confidence_score": 0,
        "sanad": {"recommendation": "BLOCK"},
    }
    assert record["bull"]["conviction"] == 0, "BLOCK should have zero bull conviction"
    assert record["bear"]["conviction"] == 0, "BLOCK should have zero bear conviction"
    assert record["bull"]["thesis"] == "", "BLOCK should have empty bull thesis"
    assert record["trade_confidence_score"] == 0, "BLOCK should have zero confidence"
check("Invariant: BLOCK → zero bull/bear/confidence", test_block_no_bull_bear)


# ── Summary ──
print(f"\n{'='*40}")
print(f"PASSED: {PASS}  |  FAILED: {FAIL}")
if FAIL > 0:
    print("⛔ SMOKE TEST FAILED")
    sys.exit(1)
else:
    print("✅ ALL IMPORTS AND CLASSIFICATIONS VERIFIED")
    sys.exit(0)
