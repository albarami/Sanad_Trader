#!/usr/bin/env python3
"""
Test Ticket 8 v2 — True E2E: evaluate_signal_fast() → DB → Async → Close → Learn

ALL tests run against an ISOLATED temp SQLite DB. No real APIs, no real LLMs.

Tests:
1. SKIP path: evaluate_signal_fast() with low score → decision in DB, no position, no task
2. EXECUTE path: evaluate_signal_fast() with high score → decision + position + task (full schema)
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
import contextlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

import state_store
from state_store import init_db, ensure_and_close_position
import learning_loop
import fast_decision_engine as fde

# Stub binance_client.get_price — proves real binance path is exercised
_get_price_calls = []

def _stub_get_price(symbol, timeout=10):
    """Deterministic stub: returns price based on symbol, records calls."""
    _get_price_calls.append({"symbol": symbol, "timeout": timeout})
    prices = {
        "EXEC_TOKEN_E2E": 2.50,
        "COLD_TOKEN_E2E": 1.25,
        "LEARN_TOKEN_E2E": 3.00,
    }
    for token, price in prices.items():
        if token in symbol:
            return price
    return 1.0

fde.HAS_BINANCE = True
fde.binance_client = type('BinanceStub', (), {'get_price': staticmethod(_stub_get_price)})()

# Stub policy engine to always PASS for E2E test isolation.
# Policy gates have their own test suite (test_policy_engine.py).
# This E2E test validates: signal scoring → strategy → DB schema → async → close → learn.
_original_stage_4 = fde.stage_4_policy_engine

def _stub_stage_4(decision_packet, portfolio, timings, start_time):
    import time as _time
    timings["stage_4_policy"] = round((_time.perf_counter() - start_time) * 1000, 2)
    return True, None, {}

fde.stage_4_policy_engine = _stub_stage_4


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


def assert_not_none(label, val):
    if val is None:
        print(f"❌ FAIL: {label}: expected non-None")
        sys.exit(1)
    print(f"✓ {label}: non-null")


def assert_in(label, key, d):
    if key not in d:
        print(f"❌ FAIL: {label}: {key!r} not in {list(d.keys())!r}")
        sys.exit(1)
    print(f"✓ {label}: has key {key!r}")


class IsolatedDB:
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sanad_t8_")
        self.db_path = Path(self.tmpdir) / "state" / "sanad_trader.db"
        init_db(self.db_path)
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


# Token deployed 48h ago — passes gate 4 (token age)
DEPLOY_TS = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()


def make_portfolio():
    return {
        "cash_balance_usd": 10000,
        "open_position_count": 0,
        "total_exposure_pct": 0,
        "meme_allocation_pct": 0,
        "current_drawdown_pct": 0,
        "daily_pnl_pct": 0,
    }


def make_runtime_state(min_score=40):
    return {
        "min_score": min_score,
        "regime_tag": "NEUTRAL",
    }


# ─────────────────────────────────────────────
# TEST 1: SKIP path via evaluate_signal_fast
# ─────────────────────────────────────────────

def test_skip_path():
    print("=" * 60)
    print("TEST 1: evaluate_signal_fast() → SKIP (low score) → decision only")
    print("=" * 60)

    db = IsolatedDB()
    try:
        # Build signal that will score LOW (rugcheck=10, no volume, 1 source)
        signal = {
            "token_address": "SKIP_TOKEN",
            "token": "SKIP_TOKEN",
            "chain": "sol",
            "source_primary": "test:skip",
            "source": "test:skip",
            "signal_type": "generic",
            "rugcheck_score": 10,
            "cross_source_count": 1,
            "volume_24h": 1000,
            "price": 0.50,
            "onchain_evidence": {},
        }

        portfolio = make_portfolio()
        runtime = make_runtime_state(min_score=50)  # High threshold → SKIP

        decision = fde.evaluate_signal_fast(signal, portfolio, runtime)
        assert_eq("Result", "SKIP", decision["result"])

        # Insert decision to DB (same as router does for SKIP)
        state_store.insert_decision(decision)

        # Verify: decision exists with full schema
        row = db.query_one("SELECT * FROM decisions WHERE decision_id=?", (decision["decision_id"],))
        assert_true("Decision in DB", row is not None)
        assert_eq("Decision result", "SKIP", row["result"])
        assert_not_none("score_breakdown_json", row["score_breakdown_json"])
        assert_not_none("timings_json", row["timings_json"])
        assert_not_none("decision_packet_json", row["decision_packet_json"])

        # Verify: NO position
        pos = db.query_one("SELECT * FROM positions WHERE decision_id=?", (decision["decision_id"],))
        assert_eq("No position for SKIP", None, pos)

        # Verify: NO task
        tasks = db.query_all("SELECT * FROM async_tasks")
        assert_eq("No tasks for SKIP", 0, len(tasks))

        print("\n✅ TEST 1 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 2: EXECUTE path via evaluate_signal_fast
# ─────────────────────────────────────────────

def test_execute_path():
    print("\n" + "=" * 60)
    print("TEST 2: evaluate_signal_fast() → EXECUTE → decision + position + task")
    print("=" * 60)

    db = IsolatedDB()
    try:
        # Build signal that scores HIGH
        signal = {
            "token_address": "EXEC_TOKEN_E2E",
            "token": "EXEC_TOKEN_E2E",
            "chain": "sol",
            "source_primary": "telegram:alpha",
            "source": "telegram:alpha",
            "signal_type": "whale_accumulation",
            "rugcheck_score": 85,
            "cross_source_count": 3,
            "volume_24h": 10_000_000,
            "price": 2.50,
            "onchain_evidence": {},
            "regime_tag": "BULLISH",
            "deployment_timestamp": DEPLOY_TS,
        }

        portfolio = make_portfolio()
        runtime = make_runtime_state(min_score=40)

        decision = fde.evaluate_signal_fast(signal, portfolio, runtime)
        assert_eq("Result", "EXECUTE", decision["result"])

        decision_id = decision["decision_id"]

        # Verify: decision exists with FULL schema fields populated
        row = db.query_one("SELECT * FROM decisions WHERE decision_id=?", (decision_id,))
        assert_true("Decision in DB", row is not None)
        assert_eq("Decision result", "EXECUTE", row["result"])
        assert_not_none("score_breakdown_json populated", row["score_breakdown_json"])
        assert_not_none("evidence_json populated", row["evidence_json"])
        assert_not_none("timings_json populated", row["timings_json"])
        assert_not_none("decision_packet_json populated", row["decision_packet_json"])
        assert_eq("strategy_id non-null", True, row["strategy_id"] is not None)
        assert_eq("position_usd non-null", True, row["position_usd"] is not None)

        # Verify JSON fields are valid JSON
        json.loads(row["score_breakdown_json"])
        json.loads(row["evidence_json"])
        json.loads(row["timings_json"])
        json.loads(row["decision_packet_json"])
        print("✓ All JSON fields parse correctly")

        # Verify: position exists (OPEN)
        positions = db.query_all("SELECT * FROM positions WHERE decision_id=?", (decision_id,))
        assert_eq("Exactly 1 position", 1, len(positions))
        pos = positions[0]
        assert_eq("Position status", "OPEN", pos["status"])
        assert_eq("Position token", "EXEC_TOKEN_E2E", pos["token_address"])
        assert_eq("Entry price", 2.5, pos["entry_price"])

        # Verify: async task exists
        task = db.query_one("SELECT * FROM async_tasks WHERE entity_id=?", (pos["position_id"],))
        assert_true("Task exists", task is not None)
        assert_eq("Task type", "ANALYZE_EXECUTED", task["task_type"])
        assert_eq("Task status", "PENDING", task["status"])
        assert_eq("Task attempts", 0, task["attempts"])

        # Verify: binance stub was called with timeout=0.5
        binance_calls = [c for c in _get_price_calls if "EXEC_TOKEN_E2E" in c["symbol"]]
        assert_true("Binance stub called", len(binance_calls) > 0)
        assert_eq("Binance timeout", 0.5, binance_calls[-1]["timeout"])

        # Store for subsequent tests
        test_execute_path.decision = decision
        test_execute_path.position_id = pos["position_id"]
        test_execute_path.db = db

        print("\n✅ TEST 2 PASSED")
    except:
        db.cleanup()
        raise


# ─────────────────────────────────────────────
# TEST 3: Idempotency
# ─────────────────────────────────────────────

def test_idempotency():
    print("\n" + "=" * 60)
    print("TEST 3: Idempotency — same signal twice → one position, one task")
    print("=" * 60)

    db = test_execute_path.db
    decision = test_execute_path.decision
    position_id = test_execute_path.position_id
    decision_id = decision["decision_id"]

    try:
        # Run evaluate_signal_fast again with same signal
        signal = {
            "token_address": "EXEC_TOKEN_E2E",
            "token": "EXEC_TOKEN_E2E",
            "chain": "sol",
            "source_primary": "telegram:alpha",
            "source": "telegram:alpha",
            "signal_type": "whale_accumulation",
            "rugcheck_score": 85,
            "cross_source_count": 3,
            "volume_24h": 10_000_000,
            "price": 2.50,
            "onchain_evidence": {},
            "regime_tag": "BULLISH",
            "deployment_timestamp": DEPLOY_TS,
        }
        portfolio = make_portfolio()
        runtime = make_runtime_state(min_score=40)

        decision2 = fde.evaluate_signal_fast(signal, portfolio, runtime)
        # Same deterministic IDs → same decision_id
        assert_eq("Same decision_id", decision_id, decision2["decision_id"])

        # Still exactly 1 position
        positions = db.query_all("SELECT * FROM positions WHERE decision_id=?", (decision_id,))
        assert_eq("Still 1 position", 1, len(positions))

        # Still exactly 1 task
        tasks = db.query_all("SELECT * FROM async_tasks WHERE entity_id=?", (position_id,))
        assert_eq("Still 1 task", 1, len(tasks))

        print("\n✅ TEST 3 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 4: Cold path — stubbed LLMs → DONE
# ─────────────────────────────────────────────

def test_cold_path_stubbed():
    print("\n" + "=" * 60)
    print("TEST 4: Cold path — stubbed LLMs → task DONE, analysis_json populated")
    print("=" * 60)

    db = IsolatedDB()
    try:
        # Create position via real EXECUTE path
        signal = {
            "token_address": "COLD_TOKEN_E2E",
            "token": "COLD_TOKEN_E2E",
            "chain": "sol",
            "source_primary": "twitter:whale",
            "source": "twitter:whale",
            "signal_type": "whale_accumulation",
            "rugcheck_score": 90,
            "cross_source_count": 3,
            "volume_24h": 8_000_000,
            "price": 1.25,
            "onchain_evidence": {},
            "regime_tag": "NEUTRAL",
            "deployment_timestamp": DEPLOY_TS,
        }
        portfolio = make_portfolio()
        runtime = make_runtime_state(min_score=40)

        decision = fde.evaluate_signal_fast(signal, portfolio, runtime)
        assert_eq("Result", "EXECUTE", decision["result"])

        pos = db.query_one("SELECT position_id FROM positions WHERE decision_id=?", (decision["decision_id"],))
        position_id = pos["position_id"]

        task = db.query_one("SELECT task_id, status FROM async_tasks WHERE entity_id=?", (position_id,))
        assert_eq("Task PENDING", "PENDING", task["status"])
        task_id = task["task_id"]

        # Stub LLMs
        def stub_claude(system_prompt, user_message, model="claude-haiku-4-5-20251001",
                        max_tokens=2000, stage="unknown", token_symbol=""):
            return json.dumps({
                "trust_score": 72, "rugpull_risk": "LOW", "sybil_risk": "LOW",
                "verdict": "PROCEED", "confidence": 80, "reasoning": "Stub: safe"
            })

        def stub_openai(system_prompt, user_message, model="gpt-5.2",
                         max_tokens=2000, stage="unknown", token_symbol=""):
            return json.dumps({
                "verdict": "APPROVE", "confidence": 75, "reasoning": "Stub: approved",
                "risk_flags": [], "bias_detected": False
            })

        import async_analysis_queue as aaq
        aaq.get_connection = state_store.get_connection

        with patch.object(aaq.llm_client, 'call_claude', side_effect=stub_claude), \
             patch.object(aaq.llm_client, 'call_openai', side_effect=stub_openai):
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

        # Verify DONE
        task = db.query_one("SELECT status FROM async_tasks WHERE task_id=?", (task_id,))
        assert_eq("Task DONE", "DONE", task["status"])

        pos = db.query_one("SELECT async_analysis_complete, async_analysis_json FROM positions WHERE position_id=?", (position_id,))
        assert_eq("Analysis complete", 1, pos["async_analysis_complete"])

        analysis = json.loads(pos["async_analysis_json"])
        for key in ("sanad", "bull", "bear", "judge", "meta"):
            assert_in("analysis_json", key, analysis)

        print("\n✅ TEST 4 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 5: Close → learning PENDING → DONE
# ─────────────────────────────────────────────

def test_close_to_learn():
    print("\n" + "=" * 60)
    print("TEST 5: Close → SQLite → learning_status=PENDING → learning_loop → DONE")
    print("=" * 60)

    db = IsolatedDB()
    try:
        # Create via real EXECUTE path
        signal = {
            "token_address": "LEARN_TOKEN_E2E",
            "token": "LEARN_TOKEN_E2E",
            "chain": "sol",
            "source_primary": "telegram:signal_group",
            "source": "telegram:signal_group",
            "signal_type": "breakout",
            "rugcheck_score": 80,
            "cross_source_count": 3,
            "volume_24h": 6_000_000,
            "price": 3.00,
            "onchain_evidence": {},
            "regime_tag": "BULLISH",
            "deployment_timestamp": DEPLOY_TS,
        }
        portfolio = make_portfolio()
        runtime = make_runtime_state(min_score=40)

        decision = fde.evaluate_signal_fast(signal, portfolio, runtime)
        assert_eq("Result", "EXECUTE", decision["result"])

        pos = db.query_one("SELECT position_id, strategy_id FROM positions WHERE decision_id=?", (decision["decision_id"],))
        position_id = pos["position_id"]
        strategy_id = pos["strategy_id"]

        # Close via ensure_and_close_position (bridge path)
        pos_dict = {
            "id": position_id,
            "token": "LEARN_TOKEN_E2E",
            "strategy_name": strategy_id,
            "signal_source_canonical": "telegram:signal_group",
            "regime_tag": "BULLISH",
            "deployment_timestamp": DEPLOY_TS,
            "entry_price": 3.00,
        }
        returned_pid = ensure_and_close_position(pos_dict, {
            "exit_price": 3.60,
            "exit_reason": "TAKE_PROFIT",
            "pnl_usd": 60.0,
            "pnl_pct": 0.20,
        }, db_path=db.db_path)
        assert_eq("Returned pid", position_id, returned_pid)

        row = db.query_one("SELECT status, learning_status FROM positions WHERE position_id=?", (position_id,))
        assert_eq("Status CLOSED", "CLOSED", row["status"])
        assert_eq("Learning PENDING", "PENDING", row["learning_status"])

        # Run learning
        result = learning_loop.process_closed_position(position_id, db.db_path)
        assert_eq("is_win", True, result["is_win"])

        row = db.query_one("SELECT learning_status FROM positions WHERE position_id=?", (position_id,))
        assert_eq("Learning DONE", "DONE", row["learning_status"])

        # Bandit stats
        bandit = db.query_one("SELECT n FROM bandit_strategy_stats WHERE strategy_id=?", (strategy_id,))
        assert_eq("Bandit n", 1, bandit["n"])

        # Source stats
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
        print("✅ ALL 5 TESTS PASSED — True E2E lifecycle validated")
        print("  evaluate_signal_fast() → Decision → Position → Async → Close → Learn")
        print("  (isolated temp DB, no real APIs, no real LLMs)")
        print("=" * 60)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
