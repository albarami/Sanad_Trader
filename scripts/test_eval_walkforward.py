#!/usr/bin/env python3
"""
ROI #2 tests: eval_walkforward.py
Run: cd /data/.openclaw/workspace/trading
     python3 scripts/test_eval_walkforward.py
"""
import os, sys, json, tempfile, subprocess, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "scripts"))
import state_store
import eval_walkforward


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _mkdb():
    td = Path(tempfile.mkdtemp(prefix="sanad_eval_"))
    db = td / "sanad_trader.db"
    state_store.init_db(db)
    return td, db


def _one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def _seed_policies(db, active="pvA"):
    with state_store.get_connection(db) as conn:
        for pv in ["pvA", "pvB"]:
            conn.execute(
                "INSERT OR REPLACE INTO policy_configs(policy_version, config_json, created_at, notes) VALUES (?,?,?,?)",
                (pv, json.dumps({"name": pv}), datetime.now(timezone.utc).isoformat(), "test")
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value, updated_at) VALUES ('active_policy_version', ?, ?)",
            (active, datetime.now(timezone.utc).isoformat())
        )


def _make_trade(db, pid, policy_version, closed_at, pnl_usd, size_usd=100.0):
    """Create a closed position with V4 reward fields via open+close."""
    entry_price = 1.0
    close_price = 1.0 + (pnl_usd / size_usd)  # reverse-engineer close price from pnl

    state_store.open_position(
        position_id=pid, token_address=f"T{pid}", chain="SOL",
        entry_price=entry_price, size_usd=size_usd,
        strategy_id="default", regime_tag="TEST",
        source_primary="unknown:general",
        decision_id=f"dec_{pid}", policy_version=policy_version,
        entry_fee_bps=0.0, venue="paper", db_path=db,
    )
    state_store.close_position(
        position_id=pid, close_reason="MANUAL_CLOSE",
        close_price=close_price, exit_fee_bps=0.0,
        venue="paper", db_path=db,
    )
    # Override closed_at for deterministic fold placement
    with state_store.get_connection(db) as conn:
        conn.execute("UPDATE positions SET closed_at=? WHERE position_id=?",
                      (_iso(closed_at), pid))


def _make_args(**kwargs):
    defaults = {
        "db": None, "candidate": "pvB", "baseline": "pvA",
        "horizon_days": 10, "train_days": 4, "test_days": 2,
        "step_days": 2, "min_trades": 2, "promote_if_pass": False,
        "notify": False, "now_iso": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ========== TESTS ==========

def test_fold_boundaries():
    """Verify correct number of folds and window placement."""
    _, db = _mkdb()
    _seed_policies(db)
    now = datetime(2026, 2, 23, 0, 0, 0, tzinfo=timezone.utc)

    # horizon=10, train=4, test=2, step=2
    # start = now - 10d = Feb 13
    # t starts at Feb 13 + 4 = Feb 17
    # Fold 0: test [Feb 17, Feb 19)
    # Fold 1: test [Feb 19, Feb 21)
    # Fold 2: test [Feb 21, Feb 23) — Feb 23 == now, so t+test_days == end → included
    # Expected: 3 folds

    # Create enough trades in each fold for both policies
    for d in range(1, 10):
        t = now - timedelta(days=10 - d)
        _make_trade(db, f"a{d}", "pvA", t, pnl_usd=0.0)
        _make_trade(db, f"b{d}", "pvB", t, pnl_usd=5.0)

    args = _make_args(db=str(db), now_iso=_iso(now))
    result = eval_walkforward.run_eval(args)

    with state_store.get_connection(db) as conn:
        folds = conn.execute("SELECT * FROM eval_folds ORDER BY fold_idx").fetchall()
        assert len(folds) == 3, f"Expected 3 folds, got {len(folds)}"
        # Verify window placement
        fold0 = dict(folds[0])
        assert "2026-02-17" in fold0["test_start"], f"Fold 0 test_start wrong: {fold0['test_start']}"
        assert "2026-02-19" in fold0["test_end"], f"Fold 0 test_end wrong: {fold0['test_end']}"


def test_metrics_computation():
    """Insert positions with known PnL and verify computed metrics."""
    _, db = _mkdb()
    _seed_policies(db)
    now = datetime(2026, 2, 23, 0, 0, 0, tzinfo=timezone.utc)

    # 3 trades: +20, -10, +5 = net +15
    pnls = [20.0, -10.0, 5.0]
    for i, pnl in enumerate(pnls):
        t = now - timedelta(days=3 - i)
        _make_trade(db, f"m{i}", "pvA", t, pnl_usd=pnl, size_usd=100.0)

    with state_store.get_connection(db) as conn:
        trades = eval_walkforward.query_closed_positions(
            conn, "pvA",
            (now - timedelta(days=5)).isoformat(),
            now.isoformat()
        )
    m = eval_walkforward.compute_metrics(trades)

    assert m["trades"] == 3, f"trades: {m['trades']}"
    assert abs(m["net_pnl_usd"] - 15.0) < 0.01, f"net_pnl: {m['net_pnl_usd']}"
    assert abs(m["win_rate"] - 2/3) < 0.01, f"win_rate: {m['win_rate']}"
    # Profit factor: 25/10 = 2.5
    assert m["profit_factor"] is not None and abs(m["profit_factor"] - 2.5) < 0.01, f"pf: {m['profit_factor']}"
    # Max drawdown: after +20, -10 → dd=10
    assert abs(m["max_drawdown_usd"] - 10.0) < 0.01, f"max_dd: {m['max_drawdown_usd']}"


def test_promotion_triggers_when_candidate_better():
    """Candidate wins across folds → PROMOTE."""
    _, db = _mkdb()
    _seed_policies(db, active="pvA")
    now = datetime(2026, 2, 23, 0, 0, 0, tzinfo=timezone.utc)

    # Create trades in each fold: pvA=0, pvB=+15
    for d in range(1, 10):
        t = now - timedelta(days=10 - d)
        _make_trade(db, f"a{d}", "pvA", t, pnl_usd=0.0)
        _make_trade(db, f"b{d}", "pvB", t, pnl_usd=15.0)

    args = _make_args(
        db=str(db), now_iso=_iso(now),
        promote_if_pass=True, min_trades=2,
    )
    result = eval_walkforward.run_eval(args)

    assert result["decision"] == "PROMOTE", f"Expected PROMOTE, got {result['decision']}: {result.get('reason')}"
    assert result["promoted_policy"] == "pvB"

    # Verify active policy changed
    active = state_store.get_active_policy_version(db_path=db)
    assert active == "pvB", f"Active should be pvB, got {active}"

    # Verify eval_runs row
    with state_store.get_connection(db) as conn:
        run = _one(conn, "SELECT * FROM eval_runs ORDER BY created_at DESC LIMIT 1")
        assert run["status"] == "DONE"
        d = json.loads(run["decision_json"])
        assert d["decision"] == "PROMOTE"


def test_no_promotion_if_insufficient_trades():
    """Candidate better but trades < min_trades → HOLD."""
    _, db = _mkdb()
    _seed_policies(db, active="pvA")
    now = datetime(2026, 2, 23, 0, 0, 0, tzinfo=timezone.utc)

    # pvA has many trades, pvB has few (per-policy check, Fix 5)
    for d in range(1, 10):
        t = now - timedelta(days=10 - d)
        _make_trade(db, f"a{d}", "pvA", t, pnl_usd=0.0)
    # pvB: only 1 trade total
    _make_trade(db, "b1", "pvB", now - timedelta(days=2), pnl_usd=100.0)

    args = _make_args(
        db=str(db), now_iso=_iso(now),
        promote_if_pass=True, min_trades=5,  # per fold * 3 folds = 15 total required
    )
    result = eval_walkforward.run_eval(args)

    assert result["decision"] == "HOLD", f"Expected HOLD, got {result['decision']}: {result.get('reason')}"
    active = state_store.get_active_policy_version(db_path=db)
    assert active == "pvA", "Active should remain pvA"


def test_eval_persistence_tables_written():
    """Verify eval_runs + eval_folds rows exist after run."""
    _, db = _mkdb()
    _seed_policies(db)
    now = datetime(2026, 2, 23, 0, 0, 0, tzinfo=timezone.utc)

    for d in range(1, 10):
        t = now - timedelta(days=10 - d)
        _make_trade(db, f"a{d}", "pvA", t, pnl_usd=0.0)
        _make_trade(db, f"b{d}", "pvB", t, pnl_usd=5.0)

    args = _make_args(db=str(db), now_iso=_iso(now))
    eval_walkforward.run_eval(args)

    with state_store.get_connection(db) as conn:
        runs = conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0]
        assert runs == 1, f"Expected 1 eval_runs row, got {runs}"

        folds = conn.execute("SELECT COUNT(*) FROM eval_folds").fetchone()[0]
        assert folds >= 1, f"Expected >= 1 eval_folds rows, got {folds}"

        # Verify fold metrics are valid JSON
        fold = _one(conn, "SELECT baseline_metrics_json, candidate_metrics_json FROM eval_folds LIMIT 1")
        bm = json.loads(fold["baseline_metrics_json"])
        cm = json.loads(fold["candidate_metrics_json"])
        assert "trades" in bm and "net_pnl_usd" in bm, "baseline metrics missing keys"
        assert "trades" in cm and "net_pnl_usd" in cm, "candidate metrics missing keys"


# ========== HARNESS ==========

def main():
    tests = [
        test_fold_boundaries,
        test_metrics_computation,
        test_promotion_triggers_when_candidate_better,
        test_no_promotion_if_insufficient_trades,
        test_eval_persistence_tables_written,
    ]
    ok = fails = 0
    print("=" * 60)
    print("ROI #2 TESTS: eval_walkforward.py")
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
            import traceback
            traceback.print_exc()
            fails += 1
    print("=" * 60)
    print(f"RESULTS: {ok} passed, {fails} failed")
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
