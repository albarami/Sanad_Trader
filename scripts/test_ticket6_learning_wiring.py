#!/usr/bin/env python3
"""
Test Ticket 6 — Learning Loop Production Wiring

ALL tests run against an ISOLATED temp SQLite DB. Never touches production.

Tests:
1. Close-flow triggers learning → DONE + stats updated
2. Cron fallback processes PENDING positions
3. No backlog after processing
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

from state_store import init_db, get_connection
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

    def create_closed_position(self, pnl_pct=0.05, strategy_id="test_strat_t6",
                                source_primary="test_source_t6", regime_tag="test_regime",
                                learning_status="PENDING"):
        position_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        c = self.conn()
        c.execute('''
            INSERT INTO positions (
                position_id, signal_id, token_address, entry_price,
                size_usd, chain, strategy_id, decision_id, status,
                created_at, updated_at, pnl_usd, pnl_pct,
                regime_tag, source_primary, exit_price, exit_reason,
                closed_at, learning_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?, ?, 'take_profit', ?, ?)
        ''', (
            position_id, 'test_sig_' + position_id[:8], 'TEST_TOKEN', 100.0,
            1000.0, 'sol', strategy_id, 'test_dec_' + position_id[:8],
            now_iso, now_iso, pnl_pct * 1000, pnl_pct,
            regime_tag, source_primary, 100.0 * (1 + pnl_pct),
            now_iso, learning_status
        ))
        c.commit()
        c.close()
        return position_id

    def query_one(self, sql, params=()):
        c = self.conn()
        row = c.execute(sql, params).fetchone()
        c.close()
        return dict(row) if row else None

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ─────────────────────────────────────────────
# TEST 1: Close-flow triggers learning → DONE
# ─────────────────────────────────────────────

def test_close_triggers_learning():
    print("=" * 60)
    print("TEST 1: Close-Flow Triggers Learning → DONE + Stats Updated")
    print("=" * 60)

    db = IsolatedDB()
    pid = db.create_closed_position(pnl_pct=0.08, strategy_id="test_strat_close",
                                     source_primary="test_source_close")

    # Simulate what position_monitor does after close:
    # call process_closed_position directly
    result = learning_loop.process_closed_position(pid, db.db_path)

    assert_eq("Not skipped", False, result.get("skipped", False))
    assert_eq("is_win", True, result["is_win"])

    # DB evidence: learning_status=DONE
    row = db.query_one("SELECT learning_status FROM positions WHERE position_id=?", (pid,))
    assert_eq("learning_status", "DONE", row["learning_status"])

    # DB evidence: bandit_strategy_stats updated
    row = db.query_one("SELECT alpha, beta, n FROM bandit_strategy_stats WHERE strategy_id='test_strat_close'")
    assert_eq("bandit alpha (1+1 win)", 2.0, row["alpha"])
    assert_eq("bandit n", 1, row["n"])

    # DB evidence: source_ucb_stats updated
    row = db.query_one("SELECT SUM(n) as n, SUM(reward_sum) as reward_sum FROM source_ucb_stats")
    assert_eq("source n", 1, row["n"])
    assert_eq("source reward_sum", 1.0, row["reward_sum"])

    print("\n✅ TEST 1 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 2: Cron fallback processes PENDING
# ─────────────────────────────────────────────

def test_cron_fallback():
    print("\n" + "=" * 60)
    print("TEST 2: Cron Fallback Processes PENDING Positions")
    print("=" * 60)

    db = IsolatedDB()
    # Create 3 PENDING positions (simulating close-flow failure)
    pids = []
    for pnl in [0.05, -0.03, 0.10]:
        pid = db.create_closed_position(pnl_pct=pnl, strategy_id="test_strat_cron",
                                         source_primary="test_source_cron",
                                         learning_status="PENDING")
        pids.append(pid)

    # Run learning_loop.run() (simulates cron)
    results = learning_loop.run(db.db_path)
    assert_eq("Processed count", 3, len(results))

    # All should be DONE
    for pid in pids:
        row = db.query_one("SELECT learning_status FROM positions WHERE position_id=?", (pid,))
        assert_eq(f"{pid[:8]} status", "DONE", row["learning_status"])

    # Stats accumulated: 2 wins, 1 loss
    row = db.query_one("SELECT alpha, beta, n FROM bandit_strategy_stats WHERE strategy_id='test_strat_cron'")
    assert_eq("bandit alpha (1+2 wins)", 3.0, row["alpha"])
    assert_eq("bandit beta (1+1 loss)", 2.0, row["beta"])
    assert_eq("bandit n", 3, row["n"])

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
    pid = db.create_closed_position(pnl_pct=0.05, strategy_id="test_strat_backlog",
                                     source_primary="test_source_backlog")

    # Before: 1 PENDING
    row = db.query_one("""
        SELECT COUNT(*) as cnt FROM positions
        WHERE status='CLOSED' AND learning_status='PENDING'
    """)
    assert_eq("Backlog before", 1, row["cnt"])

    # Process
    learning_loop.run(db.db_path)

    # After: 0 PENDING
    row = db.query_one("""
        SELECT COUNT(*) as cnt FROM positions
        WHERE status='CLOSED' AND learning_status='PENDING'
    """)
    assert_eq("Backlog after", 0, row["cnt"])

    print("\n✅ TEST 3 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        test_close_triggers_learning()
        test_cron_fallback()
        test_no_backlog()

        print("\n" + "=" * 60)
        print("✅ ALL 3 TESTS PASSED (isolated temp DB, no production data touched)")
        print("=" * 60)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
