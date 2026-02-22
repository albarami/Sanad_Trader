#!/usr/bin/env python3
"""
Test Ticket 11 — Dynamic Kelly Criterion Position Sizing

Tests:
1. Cold start (<30 trades): uses default flat sizing (7.5% of cash)
2. Kelly active (>30 trades, positive edge): half-Kelly sizing
3. Kelly negative (win_rate < 50%): minimum sizing (half default)
4. Kelly cap: position capped at max_position_pct (10%)
5. E2E: evaluate_signal_fast uses Kelly sizing from DB stats

All tests use isolated temp DB. No production data touched.
"""

import os
import sys
import json
import sqlite3
import tempfile
import shutil
import contextlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

import state_store
from state_store import init_db, get_bandit_stats
import fast_decision_engine as fde


def assert_eq(label, expected, actual):
    if expected != actual:
        print(f"❌ FAIL: {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"✓ {label}: {actual!r}")


def assert_close(label, expected, actual, tol=0.5):
    if abs(expected - actual) > tol:
        print(f"❌ FAIL: {label}: expected ~{expected}, got {actual}")
        sys.exit(1)
    print(f"✓ {label}: {actual}")


def assert_true(label, val):
    if not val:
        print(f"❌ FAIL: {label}: expected truthy, got {val!r}")
        sys.exit(1)
    print(f"✓ {label}")


class IsolatedDB:
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sanad_t11_")
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

    def seed_bandit(self, strategy_id, regime_tag, alpha, beta, n):
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO bandit_strategy_stats (strategy_id, regime_tag, alpha, beta, n, last_updated) VALUES (?, ?, ?, ?, ?, ?)",
            (strategy_id, regime_tag, alpha, beta, n, now)
        )
        conn.commit()
        conn.close()

    def cleanup(self):
        state_store.DB_PATH = self._old_db
        state_store.get_connection = self._old_get_conn
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def make_portfolio(cash=10000):
    return {
        "cash_balance_usd": cash,
        "open_position_count": 0,
        "total_exposure_pct": 0,
        "meme_allocation_pct": 0,
        "current_drawdown_pct": 0,
        "daily_pnl_pct": 0,
    }


def make_runtime(thompson_state=None):
    return {
        "min_score": 40,
        "regime_tag": "NEUTRAL",
        "kill_switch": False,
        "ucb1_grades": {},
        "thompson_state": thompson_state or {},
    }


# ─────────────────────────────────────────────
# TEST 1: Cold start → default flat sizing
# ─────────────────────────────────────────────

def test_cold_start():
    print("=" * 60)
    print("TEST 1: Cold start (<30 trades) → default 7.5% sizing")
    print("=" * 60)

    portfolio = make_portfolio(10000)
    runtime = make_runtime({})  # No stats

    pos_usd, info = fde.kelly_position_size("default", "NEUTRAL", portfolio, runtime)
    assert_eq("Method", "kelly_default", info["method"])
    assert_close("Position USD", 750.0, pos_usd)  # 10000 * 0.075
    assert_eq("n", 0, info["n"])

    # Also test with some trades but below threshold
    runtime2 = make_runtime({"default": {"NEUTRAL": {"alpha": 10, "beta": 5, "n": 14}}})
    pos_usd2, info2 = fde.kelly_position_size("default", "NEUTRAL", portfolio, runtime2)
    assert_eq("Method (14 trades)", "kelly_default", info2["method"])
    assert_close("Position USD (14 trades)", 750.0, pos_usd2)

    print("\n✅ TEST 1 PASSED")


# ─────────────────────────────────────────────
# TEST 2: Kelly active — positive edge
# ─────────────────────────────────────────────

def test_kelly_active():
    print("\n" + "=" * 60)
    print("TEST 2: Kelly active (70% win, 35 trades) → half-Kelly sizing")
    print("=" * 60)

    portfolio = make_portfolio(10000)
    # 70% win rate: alpha=25 (1+24 wins), beta=12 (1+11 losses), n=35
    # win_rate = 25/37 ≈ 0.6757
    # kelly_full = 2*0.6757 - 1 = 0.3514
    # half_kelly = 0.3514 * 0.5 = 0.1757
    # capped at max_position_pct=0.10
    # position = 10000 * 0.10 = 1000
    runtime = make_runtime({"default": {"NEUTRAL": {"alpha": 25.0, "beta": 12.0, "n": 35}}})

    pos_usd, info = fde.kelly_position_size("default", "NEUTRAL", portfolio, runtime)
    assert_eq("Method", "kelly_active", info["method"])
    assert_true("Win rate > 0.6", info["win_rate"] > 0.6)
    assert_true("Kelly full > 0.3", info["kelly_full"] > 0.3)
    # half-Kelly = 0.175 > max 0.10 → capped
    assert_close("Kelly pct (capped)", 0.10, info["kelly_pct"], tol=0.001)
    assert_close("Position USD (capped)", 1000.0, pos_usd)

    print("\n✅ TEST 2 PASSED")


# ─────────────────────────────────────────────
# TEST 3: Kelly active — moderate edge (not capped)
# ─────────────────────────────────────────────

def test_kelly_moderate():
    print("\n" + "=" * 60)
    print("TEST 3: Kelly active (58% win, 40 trades) → not capped")
    print("=" * 60)

    portfolio = make_portfolio(10000)
    # 58% win rate: alpha=24 (1+23 wins), beta=18 (1+17 losses), n=40
    # win_rate = 24/42 ≈ 0.5714
    # kelly_full = 2*0.5714 - 1 = 0.1429
    # half_kelly = 0.1429 * 0.5 = 0.0714
    # below max 0.10 → not capped
    # position = 10000 * 0.0714 = 714
    runtime = make_runtime({"default": {"NEUTRAL": {"alpha": 24.0, "beta": 18.0, "n": 40}}})

    pos_usd, info = fde.kelly_position_size("default", "NEUTRAL", portfolio, runtime)
    assert_eq("Method", "kelly_active", info["method"])
    assert_close("Win rate", 0.5714, info["win_rate"], tol=0.01)
    assert_close("Kelly full", 0.1429, info["kelly_full"], tol=0.01)
    assert_true("Not capped (kelly_pct < max)", info["kelly_pct"] < info["max_position_pct"])
    assert_close("Position USD", 714.0, pos_usd, tol=20)

    print("\n✅ TEST 3 PASSED")


# ─────────────────────────────────────────────
# TEST 4: Kelly negative — losing strategy
# ─────────────────────────────────────────────

def test_kelly_negative():
    print("\n" + "=" * 60)
    print("TEST 4: Kelly negative (40% win, 35 trades) → minimum sizing")
    print("=" * 60)

    portfolio = make_portfolio(10000)
    # 40% win rate: alpha=15, beta=22, n=35
    # win_rate = 15/37 ≈ 0.4054
    # kelly_full = 2*0.4054 - 1 = -0.189 → negative
    # position = cash * default * 0.5 = 10000 * 0.075 * 0.5 = 375
    runtime = make_runtime({"default": {"NEUTRAL": {"alpha": 15.0, "beta": 22.0, "n": 35}}})

    pos_usd, info = fde.kelly_position_size("default", "NEUTRAL", portfolio, runtime)
    assert_eq("Method", "kelly_negative", info["method"])
    assert_true("Kelly full < 0", info["kelly_full"] < 0)
    assert_close("Position USD (half default)", 375.0, pos_usd)

    print("\n✅ TEST 4 PASSED")


# ─────────────────────────────────────────────
# TEST 5: E2E — evaluate_signal_fast uses Kelly from DB
# ─────────────────────────────────────────────

def test_e2e_kelly():
    print("\n" + "=" * 60)
    print("TEST 5: E2E — evaluate_signal_fast uses Kelly sizing from DB")
    print("=" * 60)

    db = IsolatedDB()
    try:
        # Stub binance + policy
        fde.HAS_BINANCE = True
        fde.binance_client = type('S', (), {
            'get_price': staticmethod(lambda symbol, timeout=10: 2.0)
        })()
        import time as _time
        _orig = fde.stage_4_policy_engine
        def _stub(dp, portfolio, timings, start_time):
            timings["stage_4_policy"] = round((_time.perf_counter() - start_time) * 1000, 2)
            return True, None, {}
        fde.stage_4_policy_engine = _stub

        # Seed DB: 60% win rate, 40 trades → Kelly active
        # alpha=25, beta=17, n=40 → win_rate=25/42≈0.595
        # kelly_full = 2*0.595-1 = 0.190, half = 0.095
        # position = 10000 * 0.095 = 952
        db.seed_bandit("default", "NEUTRAL", 25.0, 17.0, 40)

        # Build runtime from DB
        bandit_raw = get_bandit_stats()
        thompson_state = {}
        for (strat_id, regime), stats in bandit_raw.items():
            thompson_state.setdefault(strat_id, {})[regime] = {
                "alpha": stats["alpha"], "beta": stats["beta"], "n": stats["n"]
            }

        runtime = make_runtime(thompson_state)

        DEPLOY_TS = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        signal = {
            "token_address": "KELLY_TOKEN",
            "token": "KELLY_TOKEN",
            "chain": "sol",
            "source_primary": "telegram:alpha",
            "source": "telegram:alpha",
            "signal_type": "whale_accumulation",
            "rugcheck_score": 85,
            "cross_source_count": 3,
            "volume_24h": 10_000_000,
            "price": 2.00,
            "onchain_evidence": {},
            "regime_tag": "NEUTRAL",
            "deployment_timestamp": DEPLOY_TS,
        }

        portfolio = make_portfolio(10000)
        decision = fde.evaluate_signal_fast(signal, portfolio, runtime)
        assert_eq("Result", "EXECUTE", decision["result"])

        # Check position_usd in decision — should be Kelly-sized (~952), not flat 100
        pos_usd = decision.get("position_usd", 0)
        assert_true("Position USD > 500 (Kelly active, not flat 100)", pos_usd > 500)
        assert_true("Position USD < 1100 (within Kelly range)", pos_usd < 1100)
        print(f"✓ Kelly-sized position: ${pos_usd:.2f}")

        # Restore
        fde.stage_4_policy_engine = _orig

        print("\n✅ TEST 5 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        test_cold_start()
        test_kelly_active()
        test_kelly_moderate()
        test_kelly_negative()
        test_e2e_kelly()

        print("\n" + "=" * 60)
        print("✅ ALL 5 TESTS PASSED — Dynamic Kelly Criterion sizing")
        print("  Cold start → flat default (7.5%)")
        print("  Active Kelly → half-Kelly with 10% cap")
        print("  Negative Kelly → half of default (minimum)")
        print("  E2E → evaluate_signal_fast uses Kelly from DB stats")
        print("=" * 60)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
