#!/usr/bin/env python3
"""
Sanad Trader V4 — Walk-Forward Evaluation + Promotion

Evaluates candidate policy vs baseline across rolling train/test folds.
Per-fold pass: candidate.trades >= min_trades AND candidate.net_pnl > baseline
              AND candidate.max_dd <= baseline.max_dd * 1.10
Run-level: promote if pass_rate >= 60% AND median improvement > 0
           AND sum(candidate.trades) >= min_trades * num_folds

Usage:
    python3 scripts/eval_walkforward.py \\
        --candidate pvB --baseline pvA \\
        --train-days 14 --test-days 3 --step-days 3 \\
        --min-trades 10 --promote-if-pass

For deterministic tests: --now-iso "2026-02-23T00:00:00+00:00"
"""
import os
import sys
import json
import argparse
import uuid
import statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE_DIR = Path(os.environ.get("SANAD_HOME", str(Path(__file__).resolve().parent.parent)))
sys.path.insert(0, str(BASE_DIR / "scripts"))

import state_store


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[EVAL] {ts} {msg}", flush=True)


def _parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def compute_metrics(trades: list) -> dict:
    """
    Compute evaluation metrics from closed positions.
    All pnl fields are NET (after fees/gas).
    """
    n = len(trades)
    if n == 0:
        return {"trades": 0, "net_pnl_usd": 0, "avg_reward": 0, "win_rate": 0,
                "profit_factor": None, "max_drawdown_usd": 0, "fees_total_usd": 0,
                "slippage_avg_bps": 0}

    net_pnl_usd = sum(float(t.get("pnl_usd") or 0) for t in trades)
    fees = sum(float(t.get("fees_usd_total") or 0) for t in trades)

    rewards = [float(t.get("reward_real") or t.get("pnl_pct") or 0) for t in trades]
    avg_reward = statistics.mean(rewards) if rewards else 0

    bins = [int(t.get("reward_bin") or (1 if (t.get("pnl_usd") or 0) > 0 else 0)) for t in trades]
    win_rate = statistics.mean(bins) if bins else 0

    profits = sum(max(float(t.get("pnl_usd") or 0), 0) for t in trades)
    losses = sum(min(float(t.get("pnl_usd") or 0), 0) for t in trades)
    profit_factor = (profits / abs(losses)) if abs(losses) > 1e-9 else None

    # Max drawdown from cumulative net pnl
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += float(t.get("pnl_usd") or 0)
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)

    # Avg slippage
    slips = []
    for t in trades:
        s1 = abs(float(t.get("entry_slippage_bps") or 0))
        s2 = abs(float(t.get("exit_slippage_bps") or 0))
        if s1 > 0 or s2 > 0:
            slips.append(s1 + s2)
    slippage_avg = statistics.mean(slips) if slips else 0

    return {
        "trades": n,
        "net_pnl_usd": round(net_pnl_usd, 4),
        "avg_reward": round(avg_reward, 6),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "max_drawdown_usd": round(max_dd, 4),
        "fees_total_usd": round(fees, 4),
        "slippage_avg_bps": round(slippage_avg, 2),
    }


def query_closed_positions(conn, policy_version, start_ts, end_ts):
    """Query closed positions for a policy within a time window."""
    rows = conn.execute("""
        SELECT position_id, closed_at, size_usd,
               pnl_usd, pnl_pct, pnl_gross_usd, pnl_gross_pct,
               fees_usd_total, entry_slippage_bps, exit_slippage_bps,
               reward_bin, reward_real, reward_version,
               cost_total_usd
        FROM positions
        WHERE status='CLOSED'
          AND closed_at IS NOT NULL
          AND closed_at >= ?
          AND closed_at < ?
          AND policy_version = ?
          AND reward_real IS NOT NULL
        ORDER BY closed_at ASC
    """, (start_ts, end_ts, policy_version)).fetchall()
    return [dict(r) for r in rows]


def run_eval(args):
    """Run walk-forward evaluation."""
    db_path = Path(args.db) if args.db else state_store.DB_PATH
    state_store.init_db(db_path)

    now = _parse_iso(args.now_iso) if args.now_iso else datetime.now(timezone.utc)
    end_ts = now
    start_ts = now - timedelta(days=args.horizon_days)

    run_id = str(uuid.uuid4())
    candidate = args.candidate
    baseline = args.baseline or state_store.get_active_policy_version(db_path=db_path)

    _log(f"Run {run_id[:8]}: candidate={candidate} vs baseline={baseline}")
    _log(f"Window: {start_ts.isoformat()} → {end_ts.isoformat()}")
    _log(f"Folds: train={args.train_days}d, test={args.test_days}d, step={args.step_days}d")

    # Insert eval_runs row (RUNNING)
    with state_store.get_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO eval_runs (run_id, created_at, candidate_policy, baseline_policy,
                                   train_days, test_days, step_days, start_ts, end_ts,
                                   min_trades, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING')
        """, (run_id, now.isoformat(), candidate, baseline,
              args.train_days, args.test_days, args.step_days,
              start_ts.isoformat(), end_ts.isoformat(), args.min_trades))

    # Generate folds
    folds = []
    t = start_ts + timedelta(days=args.train_days)
    while t + timedelta(days=args.test_days) <= end_ts:
        folds.append({
            "train_start": (t - timedelta(days=args.train_days)).isoformat(),
            "train_end": t.isoformat(),
            "test_start": t.isoformat(),
            "test_end": (t + timedelta(days=args.test_days)).isoformat(),
        })
        t += timedelta(days=args.step_days)

    if not folds:
        _log("No folds generated (insufficient data window)")
        with state_store.get_connection(db_path) as conn:
            conn.execute("UPDATE eval_runs SET status='DONE', decision_json=? WHERE run_id=?",
                         (json.dumps({"decision": "HOLD", "reason": "no_folds"}), run_id))
        return {"decision": "HOLD", "reason": "no_folds"}

    _log(f"Generated {len(folds)} folds")

    # Evaluate each fold
    fold_results = []
    with state_store.get_connection(db_path) as conn:
        for i, fold in enumerate(folds):
            base_trades = query_closed_positions(conn, baseline, fold["test_start"], fold["test_end"])
            cand_trades = query_closed_positions(conn, candidate, fold["test_start"], fold["test_end"])

            base_m = compute_metrics(base_trades)
            cand_m = compute_metrics(cand_trades)

            # Per-fold pass criteria
            fold_pass = (
                cand_m["trades"] >= args.min_trades
                and cand_m["net_pnl_usd"] > base_m["net_pnl_usd"]
                and cand_m["max_drawdown_usd"] <= base_m["max_drawdown_usd"] * 1.10 + 0.01  # tolerance
            )

            fold_results.append({
                "fold_idx": i,
                "baseline": base_m,
                "candidate": cand_m,
                "pass": fold_pass,
            })

            # Persist fold
            conn.execute("""
                INSERT INTO eval_folds (run_id, fold_idx, train_start, train_end,
                                        test_start, test_end,
                                        baseline_metrics_json, candidate_metrics_json, pass)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (run_id, i, fold["train_start"], fold["train_end"],
                  fold["test_start"], fold["test_end"],
                  json.dumps(base_m), json.dumps(cand_m), 1 if fold_pass else 0))

    # Run-level promotion decision
    num_folds = len(fold_results)
    passed = sum(1 for f in fold_results if f["pass"])
    pass_rate = passed / num_folds if num_folds > 0 else 0

    improvements = [f["candidate"]["net_pnl_usd"] - f["baseline"]["net_pnl_usd"]
                    for f in fold_results if f["candidate"]["trades"] > 0]
    median_improve = statistics.median(improvements) if improvements else 0

    total_cand_trades = sum(f["candidate"]["trades"] for f in fold_results)
    total_base_trades = sum(f["baseline"]["trades"] for f in fold_results)

    # Per-policy min trades (Fix 5)
    cand_has_enough = total_cand_trades >= args.min_trades * num_folds
    base_has_enough = total_base_trades >= args.min_trades * num_folds

    promote = (
        args.promote_if_pass
        and pass_rate >= 0.60
        and median_improve > 0
        and cand_has_enough
        and base_has_enough
    )

    decision = "PROMOTE" if promote else "HOLD"
    reason_parts = []
    if pass_rate < 0.60:
        reason_parts.append(f"pass_rate={pass_rate:.0%}<60%")
    if median_improve <= 0:
        reason_parts.append(f"median_improve=${median_improve:.2f}<=0")
    if not cand_has_enough:
        reason_parts.append(f"cand_trades={total_cand_trades}<{args.min_trades*num_folds}")
    if not base_has_enough:
        reason_parts.append(f"base_trades={total_base_trades}<{args.min_trades*num_folds}")
    if not args.promote_if_pass:
        reason_parts.append("--promote-if-pass not set")

    decision_summary = {
        "decision": decision,
        "candidate": candidate,
        "baseline": baseline,
        "folds": num_folds,
        "passed": passed,
        "pass_rate": round(pass_rate, 4),
        "median_improvement_usd": round(median_improve, 4),
        "total_candidate_trades": total_cand_trades,
        "total_baseline_trades": total_base_trades,
        "reason": "; ".join(reason_parts) if reason_parts else "all_checks_passed",
        "promoted_policy": candidate if promote else None,
    }

    _log(f"Decision: {decision} (pass_rate={pass_rate:.0%}, median_improve=${median_improve:.2f})")

    # Persist decision
    with state_store.get_connection(db_path) as conn:
        conn.execute("""
            UPDATE eval_runs SET status='DONE', decision_json=?
            WHERE run_id=?
        """, (json.dumps(decision_summary), run_id))

    # Execute promotion
    if promote:
        state_store.set_active_policy_version(candidate, reason=f"walkforward {run_id}", db_path=db_path)
        _log(f"PROMOTED: active policy → {candidate}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"WALK-FORWARD EVAL: {decision}")
    print(f"{'='*60}")
    print(f"Candidate: {candidate} | Baseline: {baseline}")
    print(f"Folds: {passed}/{num_folds} passed ({pass_rate:.0%})")
    print(f"Median improvement: ${median_improve:+.2f}")
    print(f"Candidate trades: {total_cand_trades} | Baseline trades: {total_base_trades}")
    for i, f in enumerate(fold_results):
        status = "✓" if f["pass"] else "✗"
        print(f"  Fold {i}: {status} cand=${f['candidate']['net_pnl_usd']:+.2f} "
              f"base=${f['baseline']['net_pnl_usd']:+.2f} "
              f"(n={f['candidate']['trades']}/{f['baseline']['trades']})")
    print(f"{'='*60}")

    # Telegram notification
    if args.notify:
        try:
            from notifier import send as notify
            emoji = "🟢" if promote else "⚪"
            notify(
                f"{emoji} *EVAL: {decision}*\n\n"
                f"Candidate: {candidate}\n"
                f"Baseline: {baseline}\n"
                f"Folds: {passed}/{num_folds} ({pass_rate:.0%})\n"
                f"Median improve: ${median_improve:+.2f}\n"
                f"Trades: {total_cand_trades} vs {total_base_trades}",
                level="L2", title=f"Eval: {decision}"
            )
        except Exception as e:
            _log(f"Telegram notification failed: {e}")

    return decision_summary


def main():
    parser = argparse.ArgumentParser(description="Walk-forward evaluation")
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument("--candidate", type=str, required=True)
    parser.add_argument("--baseline", type=str, default=None, help="Default: active policy")
    parser.add_argument("--horizon-days", type=int, default=30)
    parser.add_argument("--train-days", type=int, default=14)
    parser.add_argument("--test-days", type=int, default=3)
    parser.add_argument("--step-days", type=int, default=3)
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--promote-if-pass", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--now-iso", type=str, default=None, help="Override now (for tests)")
    args = parser.parse_args()

    run_eval(args)


if __name__ == "__main__":
    main()
