#!/usr/bin/env python3
"""
Test Ticket 8 — End-to-End Router → Position → Async → Close → Learn

ALL tests run against an ISOLATED temp SQLite DB. No real APIs, no real LLMs.

Tests:
1. Signal → Decision (SKIP path): low score → decision in DB, no position, no task
2. Signal → Decision → Position → Async task (EXECUTE path): full lifecycle
3. Idempotency: same signal twice → still one position + one task
4. Cold path wiring (stubbed LLMs): task → DONE, analysis_json populated
5. Close → SQLite → learning_status=PENDING → learning_loop → DONE
"""

import os
import sys
import uuid
import json
import sqlite3
import tempfile
import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

import state_store
from state_store import init_db, insert_decision, try_open_position_atomic, ensure_and_close_position
import learning_loop


def assert_eq(label, expected, actual):
    if expected != actual:
        print(f"❌ FAIL: {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"✓ {label}: {actual!r}")


def assert_true(label, val):
    if not val:
        print(f"❌ FAIL: {label}: expected truthy, got {val!r}")
        sys.exit(1)
    print(f"✓ {label}")


def assert_in(label, key, d):
    if key not in d:
        print(f"❌ FAIL: {label}: {key!r} not in {list(d.keys())!r}")
        sys.exit(1)
    print(f"✓ {label}: has key {key!r}")


import contextlib

class IsolatedDB:
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sanad_t8_")
        self.db_path = Path(self.tmpdir) / "state" / "sanad_trader.db"
        init_db(self.db_path)
        # Monkey-patch state_store to use isolated DB
        self._old_db = state_store.DB_PATH
        self._old_get_conn = state_store.get_connection
        state_store.DB_PATH = self.db_path
        db_path_ref = self.db_path

        @contextlib.contextmanager
        def patched_get_connection(db_path=None, timeout_s=0.25, busy_timeout_ms=250):
            _p = db_path or db_path_ref
            conn = sqlite3.connect(_p, timeout=timeout_s)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        state_store.get_connection = patched_get_connection

    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def query_one(self, sql, params=()):
        c = self.conn()
        row = c.execute(sql, params).fetchone()
        c.close()
        return dict(row) if row else None

    def query_all(self, sql, params=()):
        c = self.conn()
        rows = c.execute(sql, params).fetchall()
        c.close()
        return [dict(r) for r in rows]

    def cleanup(self):
        state_store.DB_PATH = self._old_db
        state_store.get_connection = self._old_get_conn
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def make_signal(score_boost=0, token="TEST_TOKEN_E2E", source="telegram:alpha_group"):
    """Build a canonical signal dict."""
    return {
        "token_address": token,
        "token": token,
        "chain": "sol",
        "source_primary": source,
        "source": source,
        "signal_type": "whale_accumulation",
        "rugcheck_score": 80 + score_boost,
        "cross_source_count": 3,
        "volume_24h": 5_000_000,
        "price": 1.50,
        "onchain_evidence": {},
        "regime_tag": "BULLISH",
    }


def make_decision_record(signal, result="SKIP", stage="STAGE_2_SCORE",
                          reason_code="SKIP_SCORE_LOW", strategy_id=None, position_usd=None):
    """Build a decision record dict matching DB schema."""
    import ids
    signal_id = ids.make_signal_id(signal)
    decision_id = ids.make_decision_id(signal_id, "v3.1.0")
    return {
        "decision_id": decision_id,
        "signal_id": signal_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy_version": "v3.1.0",
        "result": result,
        "stage": stage,
        "reason_code": reason_code,
        "token_address": signal["token_address"],
        "chain": signal["chain"],
        "source_primary": signal.get("source_primary"),
        "signal_type": signal.get("signal_type"),
        "score_total": 75,
        "score_breakdown_json": json.dumps({"total": 75}),
        "strategy_id": strategy_id,
        "position_usd": position_usd,
        "gate_failed": None,
        "evidence_json": "{}",
        "timings_json": "{}",
        "decision_packet_json": "{}",
    }


# ─────────────────────────────────────────────
# TEST 1: Signal → Decision (SKIP/BLOCK path)
# ─────────────────────────────────────────────

def test_skip_path():
    print("=" * 60)
    print("TEST 1: Signal → Decision (SKIP path) — no position, no task")
    print("=" * 60)

    db = IsolatedDB()
    try:
        signal = make_signal()
        decision = make_decision_record(signal, result="SKIP", stage="STAGE_2_SCORE",
                                         reason_code="SKIP_SCORE_LOW")
        decision_id = decision["decision_id"]

        # Insert decision (same as router does for SKIP/BLOCK)
        insert_decision(decision)

        # Verify: decision exists
        row = db.query_one("SELECT * FROM decisions WHERE decision_id=?", (decision_id,))
        assert_true("Decision exists in DB", row is not None)
        assert_eq("Decision result", "SKIP", row["result"])

        # Verify: NO position created
        pos = db.query_one("SELECT * FROM positions WHERE decision_id=?", (decision_id,))
        assert_eq("No position for SKIP", None, pos)

        # Verify: NO async task created
        task = db.query_one("SELECT * FROM async_tasks WHERE entity_id=?", (decision_id,))
        assert_eq("No task for SKIP", None, task)

        print("\n✅ TEST 1 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 2: Signal → Decision → Position → Async task (EXECUTE path)
# ─────────────────────────────────────────────

def test_execute_path():
    print("\n" + "=" * 60)
    print("TEST 2: Signal → Decision → Position → Async task (EXECUTE path)")
    print("=" * 60)

    db = IsolatedDB()
    try:
        signal = make_signal(token="EXEC_TOKEN")
        decision = make_decision_record(signal, result="EXECUTE", stage="STAGE_5_EXECUTE",
                                         reason_code="EXECUTE", strategy_id="momentum_breakout",
                                         position_usd=500.0)

        import ids
        position_id = ids.make_position_id(decision["decision_id"], execution_ordinal=1)

        position_payload = {
            "position_id": position_id,
            "size_token": 500.0 / 1.50,
            "regime_tag": "BULLISH",
            "features": {"entry_signal": signal, "strategy_id": "momentum_breakout"},
        }

        # Execute atomically (same as stage_5_execute does)
        pos, meta = try_open_position_atomic(decision, 1.50, position_payload)

        assert_eq("Not already existed", False, meta["already_existed"])
        assert_true("Position returned", pos is not None)

        # DB: decision exists
        row = db.query_one("SELECT result FROM decisions WHERE decision_id=?", (decision["decision_id"],))
        assert_eq("Decision result", "EXECUTE", row["result"])

        # DB: position exists, OPEN
        row = db.query_one("SELECT status, strategy_id, entry_price FROM positions WHERE position_id=?", (position_id,))
        assert_eq("Position status", "OPEN", row["status"])
        assert_eq("Strategy", "momentum_breakout", row["strategy_id"])
        assert_eq("Entry price", 1.5, row["entry_price"])

        # DB: async task exists
        task = db.query_one("SELECT status, attempts, task_type FROM async_tasks WHERE entity_id=?", (position_id,))
        assert_eq("Task type", "ANALYZE_EXECUTED", task["task_type"])
        assert_eq("Task status", "PENDING", task["status"])
        assert_eq("Task attempts", 0, task["attempts"])

        # Store for later tests
        test_execute_path.decision = decision
        test_execute_path.position_id = position_id
        test_execute_path.db = db

        print("\n✅ TEST 2 PASSED")
    except:
        db.cleanup()
        raise


# ─────────────────────────────────────────────
# TEST 3: Idempotency — same signal twice
# ─────────────────────────────────────────────

def test_idempotency():
    print("\n" + "=" * 60)
    print("TEST 3: Idempotency — same signal twice → one position, one task")
    print("=" * 60)

    # Reuse DB from test 2
    db = test_execute_path.db
    decision = test_execute_path.decision
    position_id = test_execute_path.position_id

    try:
        import ids
        position_payload = {
            "position_id": position_id,
            "size_token": 333.33,
            "regime_tag": "BULLISH",
            "features": {},
        }

        # Call again with same decision
        pos2, meta2 = try_open_position_atomic(decision, 1.50, position_payload)

        assert_eq("Already existed", True, meta2["already_existed"])

        # Verify: still exactly one position
        rows = db.query_all("SELECT * FROM positions WHERE decision_id=?", (decision["decision_id"],))
        assert_eq("Exactly 1 position", 1, len(rows))

        # Verify: still exactly one task
        tasks = db.query_all("SELECT * FROM async_tasks WHERE entity_id=?", (position_id,))
        assert_eq("Exactly 1 task", 1, len(tasks))

        print("\n✅ TEST 3 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 4: Cold path wiring — stubbed LLMs → task DONE
# ─────────────────────────────────────────────

def test_cold_path_stubbed():
    print("\n" + "=" * 60)
    print("TEST 4: Cold path — stubbed LLMs → task DONE, analysis_json populated")
    print("=" * 60)

    db = IsolatedDB()
    try:
        # Create a position + task
        signal = make_signal(token="COLD_PATH_TOKEN")
        decision = make_decision_record(signal, result="EXECUTE", stage="STAGE_5_EXECUTE",
                                         reason_code="EXECUTE", strategy_id="mean_reversion",
                                         position_usd=200.0)

        import ids
        position_id = ids.make_position_id(decision["decision_id"], execution_ordinal=1)
        position_payload = {
            "position_id": position_id,
            "size_token": 200.0 / 1.50,
            "regime_tag": "NEUTRAL",
            "features": {"strategy_id": "mean_reversion"},
        }
        try_open_position_atomic(decision, 1.50, position_payload)

        # Verify task is PENDING
        task = db.query_one("SELECT task_id, status FROM async_tasks WHERE entity_id=?", (position_id,))
        assert_eq("Task PENDING", "PENDING", task["status"])
        task_id = task["task_id"]

        # Stub LLM responses
        def stub_claude(system_prompt, user_message, model="claude-haiku-4-5-20251001",
                        max_tokens=2000, stage="unknown", token_symbol=""):
            return json.dumps({
                "trust_score": 72,
                "rugpull_risk": "LOW",
                "sybil_risk": "LOW",
                "verdict": "PROCEED",
                "confidence": 80,
                "reasoning": "Test stub: looks safe"
            })

        def stub_openai(system_prompt, user_message, model="gpt-5.2",
                         max_tokens=2000, stage="unknown", token_symbol=""):
            return json.dumps({
                "verdict": "APPROVE",
                "confidence": 75,
                "reasoning": "Test stub: approved",
                "risk_flags": [],
                "bias_detected": False
            })

        import async_analysis_queue as aaq

        # Patch get_connection in aaq to use isolated DB
        aaq.get_connection = lambda db_path=None: state_store.get_connection(db.db_path)

        with patch.object(aaq.llm_client, 'call_claude', side_effect=stub_claude), \
             patch.object(aaq.llm_client, 'call_openai', side_effect=stub_openai):

            # Run worker
            task_ids = aaq.poll_pending_tasks()
            assert_eq("Found 1 task", 1, len(task_ids))

            claimed = aaq.claim_task(task_id)
            assert_true("Claimed", claimed is not None)

            aaq.process_task(
                task_id=claimed["task_id"],
                entity_id=claimed["entity_id"],
                task_type=claimed["task_type"],
                attempts_now=claimed["attempts"]
            )

        # Restore
        from state_store import get_connection as _gc
        aaq.get_connection = _gc

        # Verify: task is DONE
        task = db.query_one("SELECT status FROM async_tasks WHERE task_id=?", (task_id,))
        assert_eq("Task DONE", "DONE", task["status"])

        # Verify: position has analysis
        pos = db.query_one("SELECT async_analysis_complete, async_analysis_json FROM positions WHERE position_id=?", (position_id,))
        assert_eq("Analysis complete", 1, pos["async_analysis_complete"])

        analysis = json.loads(pos["async_analysis_json"])
        for key in ("sanad", "bull", "bear", "judge", "meta"):
            assert_in(f"analysis_json", key, analysis)

        print("\n✅ TEST 4 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 5: Close → SQLite → learning PENDING → learning_loop → DONE
# ─────────────────────────────────────────────

def test_close_to_learn():
    print("\n" + "=" * 60)
    print("TEST 5: Close → SQLite → learning_status=PENDING → learning_loop → DONE")
    print("=" * 60)

    db = IsolatedDB()
    try:
        # Create a position via the full EXECUTE path
        signal = make_signal(token="CLOSE_LEARN_TOKEN")
        decision = make_decision_record(signal, result="EXECUTE", stage="STAGE_5_EXECUTE",
                                         reason_code="EXECUTE", strategy_id="breakout_sol",
                                         position_usd=300.0)

        import ids
        position_id = ids.make_position_id(decision["decision_id"], execution_ordinal=1)
        position_payload = {
            "position_id": position_id,
            "size_token": 200.0,
            "regime_tag": "BULLISH",
            "features": {"strategy_id": "breakout_sol"},
        }
        try_open_position_atomic(decision, 1.50, position_payload)

        # Verify OPEN
        row = db.query_one("SELECT status FROM positions WHERE position_id=?", (position_id,))
        assert_eq("Position OPEN", "OPEN", row["status"])

        # Close via ensure_and_close_position (bridge path, same as position_monitor)
        pos_dict = {
            "id": position_id,
            "token": "CLOSE_LEARN_TOKEN",
            "strategy_name": "breakout_sol",
            "signal_source_canonical": "telegram:alpha_group",
            "regime_tag": "BULLISH",
            "entry_price": 1.50,
        }
        returned_pid = ensure_and_close_position(pos_dict, {
            "exit_price": 1.80,
            "exit_reason": "TAKE_PROFIT",
            "pnl_usd": 60.0,
            "pnl_pct": 0.20,
        }, db_path=db.db_path)
        assert_eq("Returned pid", position_id, returned_pid)

        # Verify CLOSED + PENDING
        row = db.query_one("SELECT status, learning_status, pnl_pct FROM positions WHERE position_id=?", (position_id,))
        assert_eq("Status CLOSED", "CLOSED", row["status"])
        assert_eq("Learning PENDING", "PENDING", row["learning_status"])
        assert_eq("PnL", 0.2, row["pnl_pct"])

        # Run learning loop
        result = learning_loop.process_closed_position(position_id, db.db_path)
        assert_eq("is_win", True, result["is_win"])

        # Verify DONE
        row = db.query_one("SELECT learning_status FROM positions WHERE position_id=?", (position_id,))
        assert_eq("Learning DONE", "DONE", row["learning_status"])

        # Verify bandit stats incremented exactly once
        bandit = db.query_one("SELECT alpha, beta, n FROM bandit_strategy_stats WHERE strategy_id='breakout_sol'")
        assert_eq("Bandit n", 1, bandit["n"])
        assert_eq("Bandit alpha (win)", 2, bandit["alpha"])  # prior 1 + 1 win
        assert_eq("Bandit beta (no loss)", 1, bandit["beta"])

        # Verify source stats
        src = db.query_one("SELECT SUM(n) as n FROM source_ucb_stats")
        assert_eq("Source n", 1, src["n"])

        print("\n✅ TEST 5 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        test_skip_path()
        test_execute_path()
        test_idempotency()
        test_cold_path_stubbed()
        test_close_to_learn()

        print("\n" + "=" * 60)
        print("✅ ALL 5 TESTS PASSED — Full E2E lifecycle validated")
        print("  Signal → Decision → Position → Async → Close → Learn")
        print("  (isolated temp DB, no real APIs, no real LLMs)")
        print("=" * 60)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
