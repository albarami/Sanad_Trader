#!/usr/bin/env python3
"""
Test Ticket 10 — SQLite stats feed into hot path (replace JSON state reads)

Tests:
1. get_source_ucb_stats() returns correct DB rows
2. get_bandit_stats() returns correct DB rows
3. UCB1 grades computed from DB → runtime_state["ucb1_grades"]
4. Thompson state computed from DB → runtime_state["thompson_state"]
5. Full E2E: evaluate_signal_fast with DB-backed runtime_state

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
from state_store import init_db, get_source_ucb_stats, get_bandit_stats


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


def assert_close(label, expected, actual, tol=0.01):
    if abs(expected - actual) > tol:
        print(f"❌ FAIL: {label}: expected ~{expected}, got {actual}")
        sys.exit(1)
    print(f"✓ {label}: {actual}")


class IsolatedDB:
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sanad_t10_")
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

    def seed_source_stats(self, rows):
        """Seed source_ucb_stats: rows = [(source_id, n, reward_sum), ...]"""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        for src_id, n, reward_sum in rows:
            conn.execute(
                "INSERT OR REPLACE INTO source_ucb_stats (source_id, n, reward_sum, last_updated) VALUES (?, ?, ?, ?)",
                (src_id, n, reward_sum, now)
            )
        conn.commit()
        conn.close()

    def seed_bandit_stats(self, rows):
        """Seed bandit_strategy_stats: rows = [(strategy_id, regime_tag, alpha, beta, n), ...]"""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        for strat_id, regime, alpha, beta, n in rows:
            conn.execute(
                "INSERT OR REPLACE INTO bandit_strategy_stats (strategy_id, regime_tag, alpha, beta, n, last_updated) VALUES (?, ?, ?, ?, ?, ?)",
                (strat_id, regime, alpha, beta, n, now)
            )
        conn.commit()
        conn.close()

    def cleanup(self):
        state_store.DB_PATH = self._old_db
        state_store.get_connection = self._old_get_conn
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ─────────────────────────────────────────────
# TEST 1: get_source_ucb_stats from DB
# ─────────────────────────────────────────────

def test_source_ucb_stats():
    print("=" * 60)
    print("TEST 1: get_source_ucb_stats() returns DB rows")
    print("=" * 60)

    db = IsolatedDB()
    try:
        # Empty table → empty dict
        result = get_source_ucb_stats()
        assert_eq("Empty table", {}, result)

        # Seed two sources
        db.seed_source_stats([
            ("telegram:alpha", 10, 7.0),
            ("twitter:whale", 5, 1.0),
        ])

        result = get_source_ucb_stats()
        assert_eq("Source count", 2, len(result))
        assert_eq("telegram:alpha n", 10, result["telegram:alpha"]["n"])
        assert_close("telegram:alpha reward_sum", 7.0, result["telegram:alpha"]["reward_sum"])
        assert_eq("twitter:whale n", 5, result["twitter:whale"]["n"])
        assert_close("twitter:whale reward_sum", 1.0, result["twitter:whale"]["reward_sum"])

        print("\n✅ TEST 1 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 2: get_bandit_stats from DB
# ─────────────────────────────────────────────

def test_bandit_stats():
    print("\n" + "=" * 60)
    print("TEST 2: get_bandit_stats() returns DB rows")
    print("=" * 60)

    db = IsolatedDB()
    try:
        # Empty table → empty dict
        result = get_bandit_stats()
        assert_eq("Empty table", {}, result)

        # Seed two strategies
        db.seed_bandit_stats([
            ("momentum", "BULLISH", 5.0, 2.0, 6),
            ("mean_reversion", "NEUTRAL", 3.0, 4.0, 6),
        ])

        result = get_bandit_stats()
        assert_eq("Bandit count", 2, len(result))
        
        mom = result[("momentum", "BULLISH")]
        assert_close("momentum alpha", 5.0, mom["alpha"])
        assert_close("momentum beta", 2.0, mom["beta"])
        assert_eq("momentum n", 6, mom["n"])

        mr = result[("mean_reversion", "NEUTRAL")]
        assert_close("mean_reversion alpha", 3.0, mr["alpha"])
        assert_close("mean_reversion beta", 4.0, mr["beta"])

        print("\n✅ TEST 2 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 3: UCB1 grades computed from DB
# ─────────────────────────────────────────────

def test_ucb1_grades_from_db():
    print("\n" + "=" * 60)
    print("TEST 3: UCB1 grades computed from source_ucb_stats DB rows")
    print("=" * 60)

    db = IsolatedDB()
    try:
        db.seed_source_stats([
            ("telegram:alpha", 10, 9.0),   # 90% win → Grade A
            ("twitter:whale", 20, 12.0),   # 60% win → Grade B
            ("discord:pump", 15, 3.0),     # 20% win → Grade D
        ])

        # Use the same logic the router uses
        ucb_raw = get_source_ucb_stats()
        ucb1_grades = {}
        for src_id, stats in ucb_raw.items():
            n = stats["n"]
            if n == 0:
                ucb1_grades[src_id] = {"grade": "C", "score": 50, "cold_start": True}
            else:
                win_rate = stats["reward_sum"] / n
                score = win_rate * 100
                grade = "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"
                ucb1_grades[src_id] = {"grade": grade, "score": score, "cold_start": False, "n": n}

        assert_eq("telegram:alpha grade", "A", ucb1_grades["telegram:alpha"]["grade"])
        assert_close("telegram:alpha score", 90.0, ucb1_grades["telegram:alpha"]["score"])
        assert_eq("twitter:whale grade", "B", ucb1_grades["twitter:whale"]["grade"])
        assert_eq("discord:pump grade", "D", ucb1_grades["discord:pump"]["grade"])
        assert_close("discord:pump score", 20.0, ucb1_grades["discord:pump"]["score"])

        print("\n✅ TEST 3 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 4: Thompson state computed from DB
# ─────────────────────────────────────────────

def test_thompson_state_from_db():
    print("\n" + "=" * 60)
    print("TEST 4: Thompson state from bandit_strategy_stats DB rows")
    print("=" * 60)

    db = IsolatedDB()
    try:
        db.seed_bandit_stats([
            ("momentum", "BULLISH", 8.0, 3.0, 10),
            ("momentum", "BEARISH", 2.0, 5.0, 6),
            ("mean_reversion", "NEUTRAL", 4.0, 4.0, 7),
        ])

        # Use the same logic the router uses
        bandit_raw = get_bandit_stats()
        thompson_state = {}
        for (strat_id, regime), stats in bandit_raw.items():
            thompson_state.setdefault(strat_id, {})[regime] = {
                "alpha": stats["alpha"], "beta": stats["beta"], "n": stats["n"]
            }

        assert_true("momentum in state", "momentum" in thompson_state)
        assert_true("mean_reversion in state", "mean_reversion" in thompson_state)
        assert_eq("momentum regimes", 2, len(thompson_state["momentum"]))
        assert_close("momentum BULLISH alpha", 8.0, thompson_state["momentum"]["BULLISH"]["alpha"])
        assert_close("momentum BEARISH beta", 5.0, thompson_state["momentum"]["BEARISH"]["beta"])
        assert_eq("mean_reversion NEUTRAL n", 7, thompson_state["mean_reversion"]["NEUTRAL"]["n"])

        print("\n✅ TEST 4 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# TEST 5: Full E2E — evaluate_signal_fast with DB-backed runtime_state
# ─────────────────────────────────────────────

def test_e2e_with_db_stats():
    print("\n" + "=" * 60)
    print("TEST 5: evaluate_signal_fast with DB-backed ucb1_grades + thompson_state")
    print("=" * 60)

    db = IsolatedDB()
    try:
        import fast_decision_engine as fde

        # Stub binance + policy (same as ticket 8 tests)
        fde.HAS_BINANCE = True
        fde.binance_client = type('BinanceStub', (), {
            'get_price': staticmethod(lambda symbol, timeout=10: 2.0)
        })()
        _orig_stage4 = fde.stage_4_policy_engine
        import time as _time
        def _stub_stage_4(dp, portfolio, timings, start_time):
            timings["stage_4_policy"] = round((_time.perf_counter() - start_time) * 1000, 2)
            return True, None, {}
        fde.stage_4_policy_engine = _stub_stage_4

        # Seed DB with stats
        db.seed_source_stats([
            ("telegram:alpha", 10, 8.0),   # 80% → Grade A
        ])
        db.seed_bandit_stats([
            ("default", "NEUTRAL", 6.0, 2.0, 7),
        ])

        # Build runtime_state from DB (same code path as signal_router)
        ucb_raw = get_source_ucb_stats()
        ucb1_grades = {}
        for src_id, stats in ucb_raw.items():
            n = stats["n"]
            if n == 0:
                ucb1_grades[src_id] = {"grade": "C", "score": 50, "cold_start": True}
            else:
                win_rate = stats["reward_sum"] / n
                score = win_rate * 100
                grade = "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"
                ucb1_grades[src_id] = {"grade": grade, "score": score, "cold_start": False, "n": n}

        bandit_raw = get_bandit_stats()
        thompson_state = {}
        for (strat_id, regime), stats in bandit_raw.items():
            thompson_state.setdefault(strat_id, {})[regime] = {
                "alpha": stats["alpha"], "beta": stats["beta"], "n": stats["n"]
            }

        runtime_state = {
            "min_score": 40,
            "regime_tag": "NEUTRAL",
            "kill_switch": False,
            "ucb1_grades": ucb1_grades,
            "thompson_state": thompson_state,
        }

        # Verify stats are populated
        assert_eq("ucb1_grades has telegram:alpha", "A", runtime_state["ucb1_grades"]["telegram:alpha"]["grade"])
        assert_eq("thompson_state has default", True, "default" in runtime_state["thompson_state"])
        assert_close("default NEUTRAL alpha", 6.0, runtime_state["thompson_state"]["default"]["NEUTRAL"]["alpha"])

        DEPLOY_TS = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        signal = {
            "token_address": "DB_STATS_TOKEN",
            "token": "DB_STATS_TOKEN",
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

        portfolio = {
            "cash_balance_usd": 10000,
            "open_position_count": 0,
            "total_exposure_pct": 0,
            "meme_allocation_pct": 0,
            "current_drawdown_pct": 0,
            "daily_pnl_pct": 0,
        }

        decision = fde.evaluate_signal_fast(signal, portfolio, runtime_state)
        assert_eq("Result", "EXECUTE", decision["result"])

        # Verify decision was stored
        pos = db.query_one("SELECT * FROM positions WHERE decision_id=?", (decision["decision_id"],)) if hasattr(db, 'query_one') else None
        # Quick query since IsolatedDB doesn't have query_one built in for this test
        import sqlite3 as sq
        c = sq.connect(db.db_path)
        c.row_factory = sq.Row
        row = c.execute("SELECT status FROM positions WHERE decision_id=?", (decision["decision_id"],)).fetchone()
        c.close()
        assert_eq("Position OPEN", "OPEN", row["status"])

        # Restore
        fde.stage_4_policy_engine = _orig_stage4

        print("\n✅ TEST 5 PASSED")
    finally:
        db.cleanup()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        test_source_ucb_stats()
        test_bandit_stats()
        test_ucb1_grades_from_db()
        test_thompson_state_from_db()
        test_e2e_with_db_stats()

        print("\n" + "=" * 60)
        print("✅ ALL 5 TESTS PASSED — SQLite stats feed into hot path")
        print("  get_source_ucb_stats() → ucb1_grades (DB-only)")
        print("  get_bandit_stats() → thompson_state (DB-only)")
        print("  evaluate_signal_fast() works with DB-backed runtime_state")
        print("=" * 60)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
