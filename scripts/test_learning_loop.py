#!/usr/bin/env python3
"""
Test Learning Loop — Ticket 5 Validation

ALL tests run against an ISOLATED temp SQLite DB. Never touches production.

Tests:
1. WIN updates bandit (alpha+1) and source (reward+1)
2. LOSS updates bandit (beta+1) and source (reward unchanged)
3. Multiple closures accumulate correctly (3W/2L)
4. Exactly-once: second call returns skipped, stats not doubled
5. OPEN/NULL pnl positions rejected by scan + claim
6. Concurrency: two threads race on same position → only one increments
7. learning_status=DONE persists in DB after processing
"""

import os
import sys
import uuid
import json
import sqlite3
import tempfile
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

from state_store import init_db
import learning_loop


def assert_eq(label, expected, actual):
    if expected != actual:
        print(f"❌ FAIL: {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"✓ {label}: {actual!r}")


class IsolatedDB:
    """Creates a temp directory with its own SQLite DB for testing."""

    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sanad_test_")
        self.db_path = Path(self.tmpdir) / "state" / "sanad_trader.db"
        init_db(self.db_path)

    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def create_position(self, status="CLOSED", pnl_pct=0.05, pnl_usd=50.0,
                         strategy_id="test_strat", regime_tag="test_regime",
                         source_primary="test_source", token="TEST_TOKEN",
                         learning_status="PENDING"):
        """Create a test position. ALL identifiers use test_ prefix."""
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            now_iso if status == 'CLOSED' else None,
            learning_status if status == 'CLOSED' else 'PENDING'
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
# TEST 1: WIN updates both tables
# ─────────────────────────────────────────────

def test_win():
    print("=" * 60)
    print("TEST 1: WIN Position Updates Both Tables")
    print("=" * 60)

    db = IsolatedDB()
    pid = db.create_position(pnl_pct=0.12, strategy_id="test_strat_a",
                              source_primary="test_source_x", regime_tag="test_trending")

    result = learning_loop.process_closed_position(pid, db.db_path)

    assert_eq("is_win", True, result["is_win"])
    assert_eq("bandit alpha", 2.0, result["bandit"]["alpha"])
    assert_eq("bandit beta", 1.0, result["bandit"]["beta"])
    assert_eq("bandit n", 1, result["bandit"]["n"])
    assert_eq("source n", 1, result["source"]["n"])
    assert_eq("source reward_sum", 1.0, result["source"]["reward_sum"])

    print("\n✅ TEST 1 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 2: LOSS updates both tables
# ─────────────────────────────────────────────

def test_loss():
    print("\n" + "=" * 60)
    print("TEST 2: LOSS Position Updates Both Tables")
    print("=" * 60)

    db = IsolatedDB()
    pid = db.create_position(pnl_pct=-0.08, pnl_usd=-80.0, strategy_id="test_strat_b",
                              source_primary="test_source_y", regime_tag="test_choppy")

    result = learning_loop.process_closed_position(pid, db.db_path)

    assert_eq("is_win", False, result["is_win"])
    assert_eq("bandit alpha", 1.0, result["bandit"]["alpha"])
    assert_eq("bandit beta", 2.0, result["bandit"]["beta"])
    assert_eq("source reward_sum", 0.0, result["source"]["reward_sum"])

    print("\n✅ TEST 2 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 3: Multiple closures accumulate (3W/2L)
# ─────────────────────────────────────────────

def test_accumulation():
    print("\n" + "=" * 60)
    print("TEST 3: 3W/2L Accumulate Correctly")
    print("=" * 60)

    db = IsolatedDB()
    for pnl in [0.05, 0.10, -0.03, 0.08, -0.06]:
        db.create_position(pnl_pct=pnl, pnl_usd=pnl*1000,
                           strategy_id="test_strat_accum",
                           source_primary="test_source_accum",
                           regime_tag="test_trending")

    results = learning_loop.run(db.db_path)
    assert_eq("Processed count", 5, len(results))

    row = db.query_one("""
        SELECT alpha, beta, n FROM bandit_strategy_stats
        WHERE strategy_id='test_strat_accum' AND regime_tag='test_trending'
    """)
    assert_eq("bandit alpha (1+3)", 4.0, row["alpha"])
    assert_eq("bandit beta (1+2)", 3.0, row["beta"])
    assert_eq("bandit n", 5, row["n"])

    # Source is canonicalized — query whatever key ended up in the table
    row = db.query_one("SELECT SUM(n) as n, SUM(reward_sum) as reward_sum FROM source_ucb_stats")
    assert_eq("source n", 5, row["n"])
    assert_eq("source reward_sum", 3.0, row["reward_sum"])

    print("\n✅ TEST 3 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 4: Exactly-once (idempotent)
# ─────────────────────────────────────────────

def test_exactly_once():
    print("\n" + "=" * 60)
    print("TEST 4: Exactly-Once — Second Call Skipped")
    print("=" * 60)

    db = IsolatedDB()
    pid = db.create_position(pnl_pct=0.05, strategy_id="test_strat_idem",
                              source_primary="test_source_idem")

    r1 = learning_loop.process_closed_position(pid, db.db_path)
    assert_eq("First call processed", False, r1.get("skipped", False))

    r2 = learning_loop.process_closed_position(pid, db.db_path)
    assert_eq("Second call skipped", True, r2.get("skipped"))

    # Stats not doubled
    row = db.query_one("SELECT n FROM bandit_strategy_stats WHERE strategy_id='test_strat_idem'")
    assert_eq("bandit n still 1", 1, row["n"])

    # Not in scan
    unprocessed = learning_loop.scan_unprocessed_closures(db.db_path)
    assert_eq("Not in scan", 0, len(unprocessed))

    print("\n✅ TEST 4 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 5: Guards — OPEN rejected, claim rejects non-PENDING
# ─────────────────────────────────────────────

def test_guards():
    print("\n" + "=" * 60)
    print("TEST 5: OPEN Positions Not In Scan, Non-PENDING Skipped")
    print("=" * 60)

    db = IsolatedDB()

    # OPEN position
    pid_open = db.create_position(status="OPEN", strategy_id="test_strat_guard",
                                   source_primary="test_source_guard")
    unprocessed = learning_loop.scan_unprocessed_closures(db.db_path)
    found = [u for u in unprocessed if u["position_id"] == pid_open]
    assert_eq("OPEN not in scan", 0, len(found))

    # Claim rejects OPEN (claim will fail since status != CLOSED or learning != PENDING for OPEN)
    result = learning_loop.process_closed_position(pid_open, db.db_path)
    assert_eq("OPEN position skipped by claim", True, result.get("skipped"))

    print("\n✅ TEST 5 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 6: Concurrency — two threads, one wins
# ─────────────────────────────────────────────

def test_concurrency():
    print("\n" + "=" * 60)
    print("TEST 6: Concurrency — Two Threads Race, Only One Increments")
    print("=" * 60)

    db = IsolatedDB()
    pid = db.create_position(pnl_pct=0.10, strategy_id="test_strat_race",
                              source_primary="test_source_race", regime_tag="test_trending")

    results = [None, None]
    errors = [None, None]
    barrier = threading.Barrier(2)

    def worker(idx):
        try:
            barrier.wait(timeout=5)
            results[idx] = learning_loop.process_closed_position(pid, db.db_path)
        except Exception as e:
            errors[idx] = e

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Exactly one should process, one should skip
    processed = sum(1 for r in results if r and not r.get("skipped"))
    skipped = sum(1 for r in results if r and r.get("skipped"))
    errored = sum(1 for e in errors if e is not None)

    # One processes + one skips, OR one processes + one errors (busy) — both acceptable
    assert_eq("Exactly one processed", 1, processed)
    assert_eq("Other skipped or errored", 1, skipped + errored)

    # Stats incremented only once
    row = db.query_one("SELECT n FROM bandit_strategy_stats WHERE strategy_id='test_strat_race'")
    assert_eq("bandit n exactly 1", 1, row["n"])

    row = db.query_one("SELECT SUM(n) as n FROM source_ucb_stats")
    assert_eq("source n exactly 1", 1, row["n"])

    print("\n✅ TEST 6 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 7: learning_status=DONE persists in DB
# ─────────────────────────────────────────────

def test_done_persists():
    print("\n" + "=" * 60)
    print("TEST 7: learning_status=DONE Persists In DB")
    print("=" * 60)

    db = IsolatedDB()
    pid = db.create_position(pnl_pct=0.05, strategy_id="test_strat_done",
                              source_primary="test_source_done")

    learning_loop.process_closed_position(pid, db.db_path)

    row = db.query_one("SELECT learning_status, learning_error FROM positions WHERE position_id=?", (pid,))
    assert_eq("learning_status", "DONE", row["learning_status"])
    assert_eq("learning_error", None, row["learning_error"])

    print("\n✅ TEST 7 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        test_win()
        test_loss()
        test_accumulation()
        test_exactly_once()
        test_guards()
        test_concurrency()
        test_done_persists()

        print("\n" + "=" * 60)
        print("✅ ALL 7 TESTS PASSED (isolated temp DB, no production data touched)")
        print("=" * 60)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
