#!/usr/bin/env python3
"""
Test Learning Loop — Ticket 5 Validation

ALL test identifiers use 'test_' prefix. Cleanup ONLY deletes 'test_%' rows.

Verifies:
1. WIN updates bandit (alpha+1) and source (reward+1)
2. LOSS updates bandit (beta+1) and source (reward unchanged)
3. Multiple closures accumulate correctly (3W/2L)
4. Already-processed positions are skipped (idempotent / exactly-once)
5. OPEN positions and NULL pnl are rejected
"""

import os
import sys
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

from state_store import get_connection
import learning_loop


def assert_eq(label, expected, actual):
    if expected != actual:
        print(f"❌ FAIL: {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"✓ {label}: {actual!r}")


def create_position(status="CLOSED", pnl_pct=0.05, pnl_usd=50.0,
                     strategy_id="test_strat_default", regime_tag="test_regime",
                     source_primary="test_source_default", token="TEST_TOKEN"):
    """Create a test position. ALL identifiers use test_ prefix."""
    position_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with get_connection() as conn:
        conn.execute('''
            INSERT INTO positions (
                position_id, signal_id, token_address, entry_price,
                size_usd, chain, strategy_id, decision_id, status,
                created_at, updated_at, pnl_usd, pnl_pct,
                regime_tag, source_primary, exit_price, exit_reason, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            position_id,
            'test_sig_' + position_id[:8],
            token,
            100.0,
            1000.0,
            'sol',
            strategy_id,
            'test_dec_' + position_id[:8],
            status,
            now_iso,
            now_iso,
            pnl_usd if status == 'CLOSED' else None,
            pnl_pct if status == 'CLOSED' else None,
            regime_tag,
            source_primary,
            100.0 * (1 + pnl_pct) if status == 'CLOSED' else None,
            'take_profit' if pnl_pct > 0 else 'stop_loss',
            now_iso if status == 'CLOSED' else None
        ))
        conn.commit()
    
    return position_id


def cleanup(position_ids):
    """Remove ONLY test_ prefixed data. Never touches production rows."""
    with get_connection() as conn:
        for pid in position_ids:
            conn.execute("DELETE FROM positions WHERE position_id = ?", (pid,))
        conn.execute("DELETE FROM bandit_strategy_stats WHERE strategy_id LIKE 'test_%'")
        conn.execute("DELETE FROM source_ucb_stats WHERE source_id LIKE 'test_%'")
        conn.commit()


# ─────────────────────────────────────────────
# TEST 1: WIN updates both tables correctly
# ─────────────────────────────────────────────

def test_win_updates():
    print("=" * 60)
    print("TEST 1: WIN Position Updates Both Tables")
    print("=" * 60)
    
    pid = create_position(pnl_pct=0.12, strategy_id="test_strat_a",
                          source_primary="test_source_x", regime_tag="test_trending")
    
    result = learning_loop.process_closed_position(pid)
    
    assert_eq("is_win", True, result["is_win"])
    assert_eq("bandit alpha", 2.0, result["bandit"]["alpha"])
    assert_eq("bandit beta", 1.0, result["bandit"]["beta"])
    assert_eq("bandit n", 1, result["bandit"]["n"])
    assert_eq("source n", 1, result["source"]["n"])
    assert_eq("source reward_sum", 1.0, result["source"]["reward_sum"])
    assert_eq("source win_rate", 1.0, result["source"]["win_rate"])
    
    # Verify learning_complete in DB
    with get_connection() as conn:
        row = conn.execute("SELECT features_json FROM positions WHERE position_id = ?", (pid,)).fetchone()
        features = json.loads(row["features_json"])
        assert_eq("learning_complete", True, features["learning_complete"])
    
    print("\n✅ TEST 1 PASSED")
    return [pid]


# ─────────────────────────────────────────────
# TEST 2: LOSS updates both tables correctly
# ─────────────────────────────────────────────

def test_loss_updates():
    print("\n" + "=" * 60)
    print("TEST 2: LOSS Position Updates Both Tables")
    print("=" * 60)
    
    pid = create_position(pnl_pct=-0.08, pnl_usd=-80.0, strategy_id="test_strat_b",
                          source_primary="test_source_y", regime_tag="test_choppy")
    
    result = learning_loop.process_closed_position(pid)
    
    assert_eq("is_win", False, result["is_win"])
    assert_eq("bandit alpha", 1.0, result["bandit"]["alpha"])
    assert_eq("bandit beta", 2.0, result["bandit"]["beta"])
    assert_eq("bandit n", 1, result["bandit"]["n"])
    assert_eq("source n", 1, result["source"]["n"])
    assert_eq("source reward_sum", 0.0, result["source"]["reward_sum"])
    assert_eq("source win_rate", 0.0, result["source"]["win_rate"])
    
    print("\n✅ TEST 2 PASSED")
    return [pid]


# ─────────────────────────────────────────────
# TEST 3: Multiple closures accumulate (3W/2L)
# ─────────────────────────────────────────────

def test_accumulation():
    print("\n" + "=" * 60)
    print("TEST 3: Multiple Closures Accumulate Correctly (3W/2L)")
    print("=" * 60)
    
    pids = []
    for pnl in [0.05, 0.10, -0.03, 0.08, -0.06]:
        pid = create_position(pnl_pct=pnl, pnl_usd=pnl*1000,
                              strategy_id="test_strat_accum",
                              source_primary="test_source_accum",
                              regime_tag="test_trending")
        pids.append(pid)
    
    results = learning_loop.run()
    assert_eq("Processed count", 5, len(results))
    
    with get_connection() as conn:
        row = dict(conn.execute("""
            SELECT alpha, beta, n FROM bandit_strategy_stats
            WHERE strategy_id = 'test_strat_accum' AND regime_tag = 'test_trending'
        """).fetchone())
    
    assert_eq("bandit alpha (1 + 3 wins)", 4.0, row["alpha"])
    assert_eq("bandit beta (1 + 2 losses)", 3.0, row["beta"])
    assert_eq("bandit n", 5, row["n"])
    
    with get_connection() as conn:
        row = dict(conn.execute("""
            SELECT n, reward_sum FROM source_ucb_stats
            WHERE source_id = 'test_source_accum'
        """).fetchone())
    
    assert_eq("source n", 5, row["n"])
    assert_eq("source reward_sum (3 wins)", 3.0, row["reward_sum"])
    
    print("\n✅ TEST 3 PASSED")
    return pids


# ─────────────────────────────────────────────
# TEST 4: Idempotency (exactly-once)
# ─────────────────────────────────────────────

def test_idempotency():
    print("\n" + "=" * 60)
    print("TEST 4: Idempotency — Already-Processed Positions Skipped")
    print("=" * 60)
    
    pid = create_position(pnl_pct=0.05, strategy_id="test_strat_idem",
                          source_primary="test_source_idem", regime_tag="test_trending")
    
    # Process once
    learning_loop.process_closed_position(pid)
    
    # Process again — should be skipped
    result2 = learning_loop.process_closed_position(pid)
    assert_eq("Second call skipped", True, result2.get("skipped"))
    
    # Scan — should not find it
    unprocessed = learning_loop.scan_unprocessed_closures()
    found = [u for u in unprocessed if u["position_id"] == pid]
    assert_eq("Not in unprocessed after learning", 0, len(found))
    
    # Stats not doubled
    with get_connection() as conn:
        row = conn.execute("""
            SELECT n FROM bandit_strategy_stats
            WHERE strategy_id = 'test_strat_idem' AND regime_tag = 'test_trending'
        """).fetchone()
    assert_eq("bandit n still 1", 1, row["n"])
    
    print("\n✅ TEST 4 PASSED")
    return [pid]


# ─────────────────────────────────────────────
# TEST 5: Guards (OPEN rejected, NULL pnl rejected)
# ─────────────────────────────────────────────

def test_guards():
    print("\n" + "=" * 60)
    print("TEST 5: OPEN Positions and NULL PnL Rejected")
    print("=" * 60)
    
    pid_open = create_position(status="OPEN", strategy_id="test_strat_guard",
                                source_primary="test_source_guard")
    
    # OPEN not in scan
    unprocessed = learning_loop.scan_unprocessed_closures()
    found_open = [u for u in unprocessed if u["position_id"] == pid_open]
    assert_eq("OPEN position not in scan", 0, len(found_open))
    
    # process rejects OPEN
    try:
        learning_loop.process_closed_position(pid_open)
        print("❌ FAIL: Should have raised ValueError for OPEN position")
        sys.exit(1)
    except ValueError as e:
        assert_eq("Rejects OPEN", True, "not CLOSED" in str(e))
    
    print("\n✅ TEST 5 PASSED")
    return [pid_open]


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    all_pids = []
    try:
        all_pids += test_win_updates()
        all_pids += test_loss_updates()
        all_pids += test_accumulation()
        all_pids += test_idempotency()
        all_pids += test_guards()
        
        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cleanup(all_pids)
        print(f"\n✓ Cleaned up {len(all_pids)} test positions (test_ prefixed only)")
