#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Ticket 5: Learning Loop

Deterministic Python. No LLMs.

Exactly-once processing via durable learning_status state machine:
  PENDING → RUNNING → DONE (or FAILED)

Atomic claim (BEGIN IMMEDIATE) prevents double-counting.
Atomic SQL increments prevent lost-update races.
Canonical source keys via signal_normalizer.canonical_source().
Enrichers (solscan, rugcheck, helius) skipped via signal_normalizer.is_enricher().

Usage:
  python3 scripts/learning_loop.py          # scan + process all PENDING
  python3 scripts/learning_loop.py --once   # single pass (for cron/lifecycle)
"""

import json
import os
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from state_store import get_connection, init_db, DB_PATH

# Import canonical source + enricher guard from signal_normalizer
from signal_normalizer import canonical_source, is_enricher

BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "learning_loop.log"


def _log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}\n"
    print(line.strip())
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def process_closed_position(position_id: str, db_path=None) -> dict:
    """
    Process a single CLOSED position in ONE atomic transaction.

    State machine: PENDING → RUNNING → DONE
    Uses BEGIN IMMEDIATE for write lock from the start.

    Steps (all in one transaction):
    1. Atomic claim: UPDATE learning_status PENDING→RUNNING (rowcount==1 or skip)
    2. Atomic increment bandit_strategy_stats
    3. Atomic increment source_ucb_stats (skip enrichers)
    4. Mark learning_status=DONE
    5. Commit once

    Any exception → rollback → mark FAILED with error text.
    """
    db_path = db_path or DB_PATH
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")

    try:
        # BEGIN IMMEDIATE — acquire write lock upfront
        conn.execute("BEGIN IMMEDIATE")

        # Step 1: Atomic claim
        cursor = conn.execute("""
            UPDATE positions
            SET learning_status = 'RUNNING', learning_updated_at = ?
            WHERE position_id = ?
              AND status = 'CLOSED'
              AND learning_status = 'PENDING'
        """, (now_iso, position_id))

        if cursor.rowcount != 1:
            conn.rollback()
            _log(f"Position {position_id}: claim failed (not CLOSED/PENDING or already claimed)")
            return {"position_id": position_id, "skipped": True}

        # Read position data (within same transaction, guaranteed consistent)
        row = conn.execute("""
            SELECT position_id, strategy_id, regime_tag, source_primary,
                   pnl_usd, pnl_pct, token_address,
                   reward_bin, reward_real, fees_usd_total
            FROM positions WHERE position_id = ?
        """, (position_id,)).fetchone()

        pos = dict(row)

        if pos["pnl_pct"] is None:
            conn.execute("""
                UPDATE positions SET learning_status='FAILED',
                    learning_error='pnl_pct is NULL', learning_updated_at=?
                WHERE position_id=?
            """, (now_iso, position_id))
            conn.commit()
            raise ValueError(f"Position {position_id} has no pnl_pct")

        # V4: Use stored reward_bin (fallback to pnl_pct > 0 for legacy positions)
        # V5 FIX: Check Judge REJECT override (catastrophic trades treated as hard loss)
        judge_override = False
        if pos.get("reward_bin") is not None:
            is_win = bool(pos["reward_bin"])
        else:
            # Check async_analysis_json for Judge REJECT high confidence
            async_json_str = conn.execute(
                "SELECT async_analysis_json FROM positions WHERE position_id = ?",
                (position_id,)
            ).fetchone()[0]
            
            if async_json_str:
                try:
                    async_json = json.loads(async_json_str)
                    judge_parsed = async_json.get("judge", {}).get("parsed", {})
                    verdict = judge_parsed.get("verdict")
                    confidence = judge_parsed.get("confidence", 0)
                    
                    # If Judge REJECT ≥85% confidence, treat as HARD LOSS
                    if verdict == "REJECT" and confidence >= 85:
                        is_win = False
                        judge_override = True
                        _log(f"Position {position_id}: Judge REJECT override (confidence={confidence}%), forcing LOSS")
                    else:
                        is_win = pos["pnl_pct"] > 0
                except Exception:
                    is_win = pos["pnl_pct"] > 0
            else:
                is_win = pos["pnl_pct"] > 0
        
        strategy_id = pos["strategy_id"]
        regime_tag = pos["regime_tag"] or "unknown"
        raw_source = pos["source_primary"] or "unknown"

        # Canonicalize source
        try:
            source_info = canonical_source(raw_source)
            source_id = source_info["source_key"]
        except Exception:
            source_id = raw_source

        _log(f"Processing {position_id}: {pos['token_address']} "
             f"{'WIN' if is_win else 'LOSS'} ({pos['pnl_pct']:+.2%}) "
             f"strategy={strategy_id} source={source_id}")

        # Step 2: Atomic increment bandit_strategy_stats
        # V5 FIX: Apply strong negative penalty for Judge REJECT
        if judge_override:
            # Treat Judge REJECT as 3x loss penalty (beta += 3.0 instead of 1.0)
            win_inc = 0.0
            loss_inc = 3.0
            _log(f"Bandit: Applying 3x loss penalty for Judge REJECT (beta += 3.0)")
        else:
            win_inc = 1.0 if is_win else 0.0
            loss_inc = 0.0 if is_win else 1.0

        conn.execute("""
            INSERT INTO bandit_strategy_stats(strategy_id, regime_tag, alpha, beta, n, last_updated)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(strategy_id, regime_tag) DO UPDATE SET
                alpha = bandit_strategy_stats.alpha + ?,
                beta = bandit_strategy_stats.beta + ?,
                n = bandit_strategy_stats.n + 1,
                last_updated = excluded.last_updated
        """, (
            strategy_id, regime_tag,
            1.0 + win_inc, 1.0 + loss_inc,
            now_iso,
            win_inc, loss_inc
        ))

        bandit_row = dict(conn.execute("""
            SELECT alpha, beta, n FROM bandit_strategy_stats
            WHERE strategy_id = ? AND regime_tag = ?
        """, (strategy_id, regime_tag)).fetchone())

        expected = bandit_row["alpha"] / (bandit_row["alpha"] + bandit_row["beta"])
        _log(f"Bandit: {strategy_id}/{regime_tag} {'WIN' if is_win else 'LOSS'} → "
             f"Alpha={bandit_row['alpha']:.0f} Beta={bandit_row['beta']:.0f} "
             f"n={bandit_row['n']} E[v]={expected:.3f}")

        # Step 3: Atomic increment source_ucb_stats (skip enrichers)
        # V5 FIX: Apply strong negative penalty for Judge REJECT
        source_result = {}
        if is_enricher(source_id):
            _log(f"Source: {source_id} is enricher, skipping UCB1")
        else:
            if judge_override:
                # Treat Judge REJECT as reward = -2.0 (strong negative signal)
                reward = -2.0
                _log(f"Source: Applying -2.0 reward penalty for Judge REJECT")
            else:
                reward = 1.0 if is_win else 0.0
            conn.execute("""
                INSERT INTO source_ucb_stats(source_id, n, reward_sum, last_updated)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    n = source_ucb_stats.n + 1,
                    reward_sum = source_ucb_stats.reward_sum + ?,
                    last_updated = excluded.last_updated
            """, (source_id, reward, now_iso, reward))

            source_row = dict(conn.execute("""
                SELECT n, reward_sum FROM source_ucb_stats WHERE source_id = ?
            """, (source_id,)).fetchone())

            win_rate = source_row["reward_sum"] / source_row["n"] if source_row["n"] > 0 else 0.0
            _log(f"Source: {source_id} {'WIN' if is_win else 'LOSS'} → "
                 f"n={source_row['n']} reward={source_row['reward_sum']:.0f} "
                 f"win_rate={win_rate:.2%}")
            source_result = {
                "source_id": source_id,
                "n": source_row["n"],
                "reward_sum": source_row["reward_sum"],
                "win_rate": win_rate
            }

        # Step 4: Mark DONE
        conn.execute("""
            UPDATE positions
            SET learning_status = 'DONE',
                learning_updated_at = ?,
                learning_error = NULL
            WHERE position_id = ? AND learning_status = 'RUNNING'
        """, (now_iso, position_id))

        # Step 5: Single commit
        conn.commit()
        _log(f"Position {position_id} → DONE")

        return {
            "position_id": position_id,
            "is_win": is_win,
            "pnl_pct": pos["pnl_pct"],
            "bandit": {
                "strategy_id": strategy_id,
                "regime_tag": regime_tag,
                "alpha": bandit_row["alpha"],
                "beta": bandit_row["beta"],
                "n": bandit_row["n"],
                "expected_value": expected
            },
            "source": source_result
        }

    except Exception as e:
        conn.rollback()
        # Try to mark FAILED
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                UPDATE positions
                SET learning_status = 'FAILED',
                    learning_error = ?,
                    learning_updated_at = ?
                WHERE position_id = ? AND learning_status = 'RUNNING'
            """, (str(e)[:500], now_iso, position_id))
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def scan_unprocessed_closures(db_path=None) -> list:
    """Find CLOSED positions with learning_status='PENDING'."""
    db_path = db_path or DB_PATH
    with get_connection(db_path) as conn:
        rows = conn.execute("""
            SELECT position_id, token_address, pnl_pct, strategy_id, source_primary
            FROM positions
            WHERE status = 'CLOSED'
              AND pnl_pct IS NOT NULL
              AND learning_status = 'PENDING'
            ORDER BY closed_at ASC
        """).fetchall()
        return [dict(r) for r in rows]


def run(db_path=None):
    """Main entry: scan for unprocessed closures and process them."""
    db_path = db_path or DB_PATH
    # Ensure schema + backfill before scanning
    init_db(db_path)
    _log("=" * 60)
    _log("Learning Loop START")

    unprocessed = scan_unprocessed_closures(db_path)
    if not unprocessed:
        _log("No unprocessed closures")
        return []

    _log(f"Found {len(unprocessed)} unprocessed closure(s)")
    results = []
    for pos in unprocessed:
        try:
            result = process_closed_position(pos["position_id"], db_path)
            if not result.get("skipped"):
                results.append(result)
        except Exception as e:
            _log(f"Error processing {pos['position_id']}: {e}")

    _log(f"Learning Loop END — processed {len(results)}/{len(unprocessed)} positions")
    return results


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        _log("Interrupted")
    except Exception as e:
        _log(f"Learning loop crashed: {e}")
        import traceback
        _log(traceback.format_exc())
        sys.exit(1)
