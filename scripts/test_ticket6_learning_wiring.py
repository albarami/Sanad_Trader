#!/usr/bin/env python3
"""
Test Ticket 6 — Learning Loop Production Wiring

ALL tests run against an ISOLATED temp SQLite DB. Never touches production.

Tests:
1. ensure_and_close_position() creates missing position + closes + triggers learning → DONE
2. Cron fallback processes PENDING positions
3. No backlog after processing
4. ensure_and_close_position() works for position already in SQLite
"""

import os
import sys
import uuid
import json
import sqlite3
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

from state_store import init_db, ensure_and_close_position
import learning_loop


def assert_eq(label, expected, actual):
    if expected != actual:
        print(f"❌ FAIL: {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"✓ {label}: {actual!r}")


class IsolatedDB:
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sanad_t6_")
        self.db_path = Path(self.tmpdir) / "state" / "sanad_trader.db"
        init_db(self.db_path)

    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def query_one(self, sql, params=()):
        c = self.conn()
        row = c.execute(sql, params).fetchone()
        c.close()
        return dict(row) if row else None

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def make_file_position(position_id=None, token="TEST_SOL", entry_price=100.0,
                        strategy_name="test_strat_t6", source="test_source_t6"):
    """Create a position dict as it would appear in positions.json (file-based v3.0)."""
    pid = position_id or str(uuid.uuid4())
    return {
        "id": pid,
        "token": token,
        "token_address": token,
        "symbol": f"{token}USDT",
        "entry_price": entry_price,
        "position_usd": 1000.0,
        "chain": "sol",
        "strategy_name": strategy_name,
        "signal_source_canonical": source,
        "regime_tag": "test_regime",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "OPEN",
        "quantity": 10.0,
    }


# ─────────────────────────────────────────────
# TEST 1: Full close path — position NOT in SQLite (v3.0 bridge)
# ─────────────────────────────────────────────

def test_close_bridge():
    print("=" * 60)
    print("TEST 1: ensure_and_close_position (v3.0→v3.1 bridge) + Learning")
    print("=" * 60)

    db = IsolatedDB()
    pos = make_file_position()
    pid = pos["id"]

    # Verify NOT in SQLite
    row = db.query_one("SELECT * FROM positions WHERE position_id=?", (pid,))
    assert_eq("Not in SQLite before close", None, row)

    # Close via ensure_and_close_position (same code path as position_monitor)
    returned_pid = ensure_and_close_position(pos, {
        "exit_price": 108.0,
        "exit_reason": "TAKE_PROFIT",
        "pnl_usd": 80.0,
        "pnl_pct": 0.08,
    }, db_path=db.db_path)
    assert_eq("Returned position_id", pid, returned_pid)

    # DB evidence: position created + closed
    row = db.query_one("SELECT status, learning_status, pnl_pct, exit_reason FROM positions WHERE position_id=?", (pid,))
    assert_eq("status", "CLOSED", row["status"])
    assert_eq("learning_status", "PENDING", row["learning_status"])
    assert_eq("pnl_pct", 0.08, row["pnl_pct"])
    assert_eq("exit_reason", "TAKE_PROFIT", row["exit_reason"])

    # Trigger learning (same as position_monitor does)
    result = learning_loop.process_closed_position(pid, db.db_path)
    assert_eq("Not skipped", False, result.get("skipped", False))
    assert_eq("is_win", True, result["is_win"])

    # DB evidence: learning_status=DONE
    row = db.query_one("SELECT learning_status FROM positions WHERE position_id=?", (pid,))
    assert_eq("learning_status after learning", "DONE", row["learning_status"])

    # DB evidence: bandit stats updated
    row = db.query_one("SELECT alpha, beta, n FROM bandit_strategy_stats WHERE strategy_id='test_strat_t6'")
    assert_eq("bandit n", 1, row["n"])

    # DB evidence: source stats updated
    row = db.query_one("SELECT SUM(n) as n FROM source_ucb_stats")
    assert_eq("source n", 1, row["n"])

    print("\n✅ TEST 1 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 2: Cron fallback
# ─────────────────────────────────────────────

def test_cron_fallback():
    print("\n" + "=" * 60)
    print("TEST 2: Cron Fallback Processes PENDING Positions")
    print("=" * 60)

    db = IsolatedDB()

    # Close 3 positions via ensure_and_close (but don't run learning)
    pids = []
    for pnl in [0.05, -0.03, 0.10]:
        pos = make_file_position(strategy_name="test_strat_cron", source="test_source_cron")
        pid = ensure_and_close_position(pos, {
            "exit_price": 100.0 * (1 + pnl),
            "exit_reason": "take_profit" if pnl > 0 else "stop_loss",
            "pnl_usd": pnl * 1000,
            "pnl_pct": pnl,
        }, db_path=db.db_path)
        pids.append(pid)

    # Verify all PENDING
    for pid in pids:
        row = db.query_one("SELECT learning_status FROM positions WHERE position_id=?", (pid,))
        assert_eq(f"{pid[:8]} before cron", "PENDING", row["learning_status"])

    # Run cron (learning_loop.run)
    results = learning_loop.run(db.db_path)
    assert_eq("Processed count", 3, len(results))

    # All should be DONE
    for pid in pids:
        row = db.query_one("SELECT learning_status FROM positions WHERE position_id=?", (pid,))
        assert_eq(f"{pid[:8]} after cron", "DONE", row["learning_status"])

    print("\n✅ TEST 2 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 3: No backlog after processing
# ─────────────────────────────────────────────

def test_no_backlog():
    print("\n" + "=" * 60)
    print("TEST 3: No Backlog After Processing")
    print("=" * 60)

    db = IsolatedDB()

    pos = make_file_position(strategy_name="test_strat_backlog")
    ensure_and_close_position(pos, {
        "exit_price": 105.0, "exit_reason": "take_profit",
        "pnl_usd": 50.0, "pnl_pct": 0.05
    }, db_path=db.db_path)

    row = db.query_one("SELECT COUNT(*) as cnt FROM positions WHERE status='CLOSED' AND learning_status='PENDING'")
    assert_eq("Backlog before", 1, row["cnt"])

    learning_loop.run(db.db_path)

    row = db.query_one("SELECT COUNT(*) as cnt FROM positions WHERE status='CLOSED' AND learning_status='PENDING'")
    assert_eq("Backlog after", 0, row["cnt"])

    print("\n✅ TEST 3 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 4: Position already in SQLite (v3.1 native)
# ─────────────────────────────────────────────

def test_existing_position():
    print("\n" + "=" * 60)
    print("TEST 4: Position Already In SQLite (v3.1 native close)")
    print("=" * 60)

    db = IsolatedDB()
    pid = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    # Pre-create position in SQLite (as v3.1 would)
    c = db.conn()
    c.execute("""
        INSERT INTO positions (
            position_id, signal_id, token_address, entry_price, size_usd,
            chain, strategy_id, decision_id, status, created_at, updated_at,
            regime_tag, source_primary, learning_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, 'PENDING')
    """, (pid, 'sig_'+pid[:8], 'TEST_TOKEN', 100.0, 1000.0, 'sol',
          'test_strat_native', 'dec_'+pid[:8], now_iso, now_iso,
          'test_regime', 'test_source_native'))
    c.commit()
    c.close()

    # Close via ensure_and_close (should update existing, not create new)
    pos_dict = {"id": pid, "token": "TEST_TOKEN", "entry_price": 100.0}
    returned_pid = ensure_and_close_position(pos_dict, {
        "exit_price": 112.0, "exit_reason": "TAKE_PROFIT",
        "pnl_usd": 120.0, "pnl_pct": 0.12
    }, db_path=db.db_path)
    assert_eq("Returned pid", pid, returned_pid)

    row = db.query_one("SELECT status, pnl_pct, learning_status FROM positions WHERE position_id=?", (pid,))
    assert_eq("status", "CLOSED", row["status"])
    assert_eq("pnl_pct", 0.12, row["pnl_pct"])
    assert_eq("learning_status", "PENDING", row["learning_status"])

    # Trigger learning
    result = learning_loop.process_closed_position(pid, db.db_path)
    assert_eq("is_win", True, result["is_win"])

    row = db.query_one("SELECT learning_status FROM positions WHERE position_id=?", (pid,))
    assert_eq("learning_status", "DONE", row["learning_status"])

    print("\n✅ TEST 4 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 5: Collision — insert ignored, close fails closed
# ─────────────────────────────────────────────

def test_collision_fails_closed():
    print("\n" + "=" * 60)
    print("TEST 5: Collision — Insert Ignored → RuntimeError (fail-closed)")
    print("=" * 60)

    db = IsolatedDB()
    
    # Create an existing position with a specific decision_id
    collision_decision_id = "legacy_decision_COLLISION"
    existing_pid = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    c = db.conn()
    c.execute("""
        INSERT INTO positions (
            position_id, signal_id, token_address, entry_price, size_usd,
            chain, strategy_id, decision_id, status, created_at, updated_at,
            regime_tag, source_primary, learning_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, 'PENDING')
    """, (existing_pid, 'sig_existing', 'EXISTING_TOKEN', 100.0, 1000.0, 'sol',
          'test_strat', collision_decision_id, now_iso, now_iso,
          'test_regime', 'test_source'))
    c.commit()
    c.close()

    # Now try to close a DIFFERENT position whose decision_id collides
    new_pid = str(uuid.uuid4())
    pos_dict = {
        "id": new_pid,
        "token": "NEW_TOKEN",
        "decision_id": collision_decision_id,  # COLLISION!
        "entry_price": 100.0,
    }

    try:
        ensure_and_close_position(pos_dict, {
            "exit_price": 110.0, "exit_reason": "TAKE_PROFIT",
            "pnl_usd": 100.0, "pnl_pct": 0.10
        }, db_path=db.db_path)
        print("❌ FAIL: Should have raised RuntimeError")
        sys.exit(1)
    except RuntimeError as e:
        assert_eq("Error mentions collision", True, "still missing" in str(e))
        print(f"  Caught expected error: {e}")

    # Verify new_pid NOT in DB
    row = db.query_one("SELECT * FROM positions WHERE position_id=?", (new_pid,))
    assert_eq("Colliding position not created", None, row)

    # Verify existing position untouched
    row = db.query_one("SELECT status FROM positions WHERE position_id=?", (existing_pid,))
    assert_eq("Existing position still OPEN", "OPEN", row["status"])

    print("\n✅ TEST 5 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        test_close_bridge()
        test_cron_fallback()
        test_no_backlog()
        test_existing_position()
        test_collision_fails_closed()

        print("\n" + "=" * 60)
        print("✅ ALL 5 TESTS PASSED (isolated temp DB, no production data touched)")
        print("=" * 60)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
