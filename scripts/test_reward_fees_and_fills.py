#!/usr/bin/env python3
"""
ROI #1 tests: reward + fees/slippage storage.
Run: cd /data/.openclaw/workspace/trading
     python3 scripts/test_reward_fees_and_fills.py
"""
import os, sys, json, tempfile, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "scripts"))
import state_store


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()

def _mkdb():
    td = Path(tempfile.mkdtemp(prefix="sanad_test_"))
    db = td / "sanad_trader.db"
    state_store.init_db(db)
    return td, db

def _one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()

def _assert_close(a, b, tol=1e-6, msg=""):
    if a is None or b is None:
        raise AssertionError(f"{msg} got None: a={a}, b={b}")
    if abs(float(a) - float(b)) > tol:
        raise AssertionError(f"{msg} | {a} != {b} (tol={tol})")


# ========== TESTS ==========

def test_schema_has_fills_and_columns():
    _, db = _mkdb()
    with state_store.get_connection(db) as conn:
        r = _one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='fills'")
        assert r is not None, "fills table missing"
        cols = [row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()]
        for c in ["entry_fill_id", "exit_fill_id", "fees_usd_total", "pnl_gross_usd",
                   "pnl_gross_pct", "reward_bin", "reward_real", "reward_version",
                   "policy_version", "decision_id"]:
            assert c in cols, f"positions.{c} missing"
        # Eval tables
        for t in ["policy_configs", "meta", "eval_walkforward_runs"]:
            r = _one(conn, f"SELECT name FROM sqlite_master WHERE type='table' AND name='{t}'")
            assert r is not None, f"{t} table missing"
        # Seed check
        meta = _one(conn, "SELECT value FROM meta WHERE key='active_policy_version'")
        assert meta and meta[0] == "main", "meta seed failed"


def test_record_fill_computes_notional_and_fee():
    _, db = _mkdb()
    fill_id = state_store.record_fill(
        position_id="pos_test_fill", side="BUY", venue="paper",
        expected_price=2.00, exec_price=2.00, qty_base=100.0,
        fee_bps=10.0, fee_usd=None, slippage_bps=0.0, db_path=db,
    )
    assert isinstance(fill_id, str) and len(fill_id) > 10, "fill_id not returned"
    with state_store.get_connection(db) as conn:
        row = _one(conn, "SELECT * FROM fills WHERE fill_id=?", (fill_id,))
        assert row is not None, "fill row not persisted"
        _assert_close(row["notional_usd"], 200.0, msg="notional_usd wrong")
        _assert_close(row["fee_usd"], 0.2, msg="fee_usd compute wrong")
        _assert_close(row["fee_bps"], 10.0, msg="fee_bps persisted wrong")


def test_open_position_writes_entry_fill_and_cost_fields():
    _, db = _mkdb()
    state_store.open_position(
        position_id="pos_open_1", token_address="TESTTOKEN", chain="SOL",
        entry_price=2.00, size_usd=200.0, strategy_id="default",
        regime_tag="TEST", source_primary="test:source",
        decision_id="dec1", policy_version="pvA",
        entry_expected_price=2.00, entry_slippage_bps=0.0,
        entry_fee_bps=10.0, entry_fee_usd=None, venue="paper", db_path=db,
    )
    with state_store.get_connection(db) as conn:
        pos = _one(conn, "SELECT * FROM positions WHERE position_id='pos_open_1'")
        assert pos is not None, "position missing"
        assert pos["entry_fill_id"] is not None, "entry_fill_id missing"
        assert pos["status"] == "OPEN", "status not OPEN"
        _assert_close(pos["entry_fee_bps"], 10.0, msg="entry_fee_bps wrong")
        _assert_close(pos["entry_fee_usd"], 0.2, msg="entry_fee_usd wrong")
        assert pos["policy_version"] == "pvA", "policy_version wrong"
        assert pos["decision_id"] == "dec1", "decision_id wrong"
        f = _one(conn, "SELECT * FROM fills WHERE fill_id=?", (pos["entry_fill_id"],))
        assert f is not None, "entry fill missing"
        assert f["side"] == "BUY", "entry fill side wrong"
        _assert_close(f["qty_base"], 100.0, msg="entry qty_base wrong")
        _assert_close(f["exec_price"], 2.0, msg="entry exec_price wrong")


def test_open_position_rejects_bad_entry_price():
    _, db = _mkdb()
    try:
        state_store.open_position(
            position_id="bad1", token_address="T", chain="SOL",
            entry_price=0.0, size_usd=200.0, db_path=db,
        )
        raise AssertionError("Should have raised ValueError for entry_price=0")
    except ValueError:
        pass
    try:
        state_store.open_position(
            position_id="bad2", token_address="T", chain="SOL",
            entry_price=1.0, size_usd=-5.0, db_path=db,
        )
        raise AssertionError("Should have raised ValueError for size_usd<0")
    except ValueError:
        pass


def test_close_position_computes_gross_net_fees_reward_and_exit_fill():
    _, db = _mkdb()
    state_store.open_position(
        position_id="pos_close_1", token_address="TESTTOKEN", chain="SOL",
        entry_price=2.00, size_usd=200.0, strategy_id="default",
        regime_tag="TEST", source_primary="test:source",
        decision_id="dec2", policy_version="pvA",
        entry_expected_price=2.00, entry_slippage_bps=0.0,
        entry_fee_bps=10.0, venue="paper", db_path=db,
    )
    state_store.close_position(
        position_id="pos_close_1", close_reason="TAKE_PROFIT",
        close_price=2.20, exit_expected_price=2.20,
        exit_slippage_bps=0.0, exit_fee_bps=10.0,
        exit_fee_usd=None, venue="paper", db_path=db,
    )
    with state_store.get_connection(db) as conn:
        pos = _one(conn, "SELECT * FROM positions WHERE position_id='pos_close_1'")
        assert pos["status"] == "CLOSED", "not closed"
        assert pos["exit_fill_id"] is not None, "exit_fill_id missing"
        # qty_base = 200/2 = 100, gross_exit = 100*2.2 = 220
        _assert_close(pos["pnl_gross_usd"], 20.0, msg="gross pnl wrong")
        _assert_close(pos["pnl_gross_pct"], 20.0/200.0, msg="gross pct wrong")
        # entry_fee=0.2, exit_fee=220*10/10000=0.22, total=0.42
        _assert_close(pos["fees_usd_total"], 0.42, msg="fees_total wrong")
        # net = 20 - 0.42 = 19.58
        _assert_close(pos["pnl_usd"], 19.58, msg="net pnl wrong")
        _assert_close(pos["pnl_pct"], 19.58/200.0, msg="net pct wrong")
        assert pos["reward_version"] == "v1", "reward_version wrong"
        assert pos["reward_bin"] == 1, "reward_bin should be 1 for net win"
        _assert_close(pos["reward_real"], 19.58/200.0, msg="reward_real wrong")
        f = _one(conn, "SELECT * FROM fills WHERE fill_id=?", (pos["exit_fill_id"],))
        assert f["side"] == "SELL", "exit fill side wrong"
        _assert_close(f["notional_usd"], 220.0, msg="exit notional wrong")
        _assert_close(f["fee_usd"], 0.22, msg="exit fee wrong")


def test_reward_real_is_clamped():
    _, db = _mkdb()
    state_store.open_position(
        position_id="pos_clamp", token_address="CLAMP", chain="SOL",
        entry_price=1.0, size_usd=200.0, strategy_id="default",
        entry_fee_bps=0.0, venue="paper", db_path=db,
    )
    # Huge win: close_price=5 => pnl_pct=+4.0 (400%), should clamp to +1.0
    state_store.close_position(
        position_id="pos_clamp", close_reason="TAKE_PROFIT",
        close_price=5.0, exit_fee_bps=0.0, venue="paper", db_path=db,
    )
    with state_store.get_connection(db) as conn:
        pos = _one(conn, "SELECT reward_real, pnl_pct FROM positions WHERE position_id='pos_clamp'")
        assert pos is not None
        assert pos["pnl_pct"] > 1.0, f"setup failed: pnl_pct={pos['pnl_pct']} not > 1.0"
        _assert_close(pos["reward_real"], 1.0, msg="reward_real not clamped to +1.0")


# ========== HARNESS ==========

def main():
    tests = [
        test_schema_has_fills_and_columns,
        test_record_fill_computes_notional_and_fee,
        test_open_position_writes_entry_fill_and_cost_fields,
        test_open_position_rejects_bad_entry_price,
        test_close_position_computes_gross_net_fees_reward_and_exit_fill,
        test_reward_real_is_clamped,
    ]
    ok = fails = 0
    print("=" * 60)
    print("ROI #1 TESTS: Reward + Fees/Slippage Storage")
    print("=" * 60)
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
            fails += 1
        except Exception as e:
            print(f"✗ {t.__name__}: ERROR {e}")
            fails += 1
    print("=" * 60)
    print(f"RESULTS: {ok} passed, {fails} failed")
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
