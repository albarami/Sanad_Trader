#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Ticket 5: Learning Loop

Deterministic Python. No LLMs.

When a position is CLOSED with PnL data, this module:
1. Updates bandit_strategy_stats (Thompson Sampling Beta params)
2. Updates source_ucb_stats (UCB1 source grading)

All operations are exactly-once: a single DB transaction atomically
claims the position, increments stats, and marks learning complete.

Functions:
- process_closed_position(position_id) — Single-transaction atomic processing
- scan_unprocessed_closures() — Find CLOSED positions not yet learned from
- run() — CLI: scan + process all unprocessed closures
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from state_store import get_connection, DBBusyError

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "learning_loop.log"

# Enricher sources that must NOT be graded via UCB1
ENRICHER_SOURCES = frozenset([
    "solscan", "rugcheck", "dexscreener", "birdeye_enricher",
    "etherscan", "bscscan", "blockchair",
])


def _log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}\n"
    print(line.strip())
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def _is_enricher(source_id: str) -> bool:
    """Check if source is an enricher (not a signal source)."""
    if not source_id:
        return False
    # Check against known enrichers
    lower = source_id.lower()
    for e in ENRICHER_SOURCES:
        if e in lower:
            return True
    # Also try the signal_normalizer if available
    try:
        from signal_normalizer import is_enricher
        return is_enricher(source_id)
    except Exception:
        pass
    return False


def process_closed_position(position_id: str) -> dict:
    """
    Process a single CLOSED position in ONE atomic transaction.
    
    Single transaction:
    1. Atomic claim: check CLOSED + pnl_pct NOT NULL + not already learning_complete
    2. Atomic increment bandit_strategy_stats (no read-modify-write)
    3. Atomic increment source_ucb_stats (no read-modify-write)
    4. Mark position learning_complete
    5. Commit once
    
    If any step fails, entire transaction rolls back — no double-counting.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with get_connection() as conn:
        # Step 1: Atomic claim — read position AND verify it's eligible
        row = conn.execute("""
            SELECT position_id, strategy_id, regime_tag, source_primary,
                   pnl_usd, pnl_pct, status, token_address, features_json
            FROM positions
            WHERE position_id = ?
        """, (position_id,)).fetchone()
        
        if not row:
            raise ValueError(f"Position {position_id} not found")
        
        pos = dict(row)
        
        if pos["status"] != "CLOSED":
            raise ValueError(f"Position {position_id} is {pos['status']}, not CLOSED")
        
        if pos["pnl_pct"] is None:
            raise ValueError(f"Position {position_id} has no pnl_pct")
        
        # Check if already processed
        features = {}
        if pos["features_json"]:
            try:
                features = json.loads(pos["features_json"])
            except json.JSONDecodeError:
                features = {}
        
        if features.get("learning_complete"):
            _log(f"Position {position_id} already processed, skipping")
            return {"position_id": position_id, "skipped": True}
        
        is_win = pos["pnl_pct"] > 0
        strategy_id = pos["strategy_id"]
        regime_tag = pos["regime_tag"] or "unknown"
        source_id = pos["source_primary"] or "unknown"
        
        _log(f"Processing position {position_id}: {pos['token_address']} "
             f"{'WIN' if is_win else 'LOSS'} ({pos['pnl_pct']:+.2%}) "
             f"strategy={strategy_id} source={source_id}")
        
        # Step 2: Atomic increment bandit_strategy_stats (no read-modify-write)
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
            1.0 + win_inc, 1.0 + loss_inc,  # New row: prior(1) + increment
            now_iso,
            win_inc, loss_inc                 # Existing row: just increment
        ))
        
        # Read back for logging
        bandit_row = conn.execute("""
            SELECT alpha, beta, n FROM bandit_strategy_stats
            WHERE strategy_id = ? AND regime_tag = ?
        """, (strategy_id, regime_tag)).fetchone()
        bandit_row = dict(bandit_row)
        
        expected = bandit_row["alpha"] / (bandit_row["alpha"] + bandit_row["beta"])
        _log(f"Bandit: {strategy_id}/{regime_tag} {'WIN' if is_win else 'LOSS'} → "
             f"Alpha={bandit_row['alpha']:.0f} Beta={bandit_row['beta']:.0f} "
             f"n={bandit_row['n']} E[v]={expected:.3f}")
        
        # Step 3: Atomic increment source_ucb_stats (skip enrichers)
        source_result = {}
        if _is_enricher(source_id):
            _log(f"Source: {source_id} is enricher, skipping UCB1 update")
        else:
            reward = 1.0 if is_win else 0.0
            
            conn.execute("""
                INSERT INTO source_ucb_stats(source_id, n, reward_sum, last_updated)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    n = source_ucb_stats.n + 1,
                    reward_sum = source_ucb_stats.reward_sum + ?,
                    last_updated = excluded.last_updated
            """, (
                source_id,
                reward,      # New row: first reward
                now_iso,
                reward       # Existing row: increment reward
            ))
            
            # Read back for logging
            source_row = conn.execute("""
                SELECT n, reward_sum FROM source_ucb_stats
                WHERE source_id = ?
            """, (source_id,)).fetchone()
            source_row = dict(source_row)
            
            win_rate = source_row["reward_sum"] / source_row["n"] if source_row["n"] > 0 else 0.0
            _log(f"Source: {source_id} {'WIN' if is_win else 'LOSS'} → "
                 f"n={source_row['n']} reward_sum={source_row['reward_sum']:.0f} "
                 f"win_rate={win_rate:.2%}")
            
            source_result = {
                "source_id": source_id,
                "n": source_row["n"],
                "reward_sum": source_row["reward_sum"],
                "win_rate": win_rate
            }
        
        # Step 4: Mark position learning_complete
        features["learning_complete"] = True
        features["learning_at"] = now_iso
        
        conn.execute("""
            UPDATE positions
            SET features_json = ?,
                updated_at = ?
            WHERE position_id = ?
        """, (json.dumps(features), now_iso, position_id))
        
        # Step 5: Single commit — all or nothing
        conn.commit()
    
    _log(f"Position {position_id} learning complete")
    
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


def scan_unprocessed_closures() -> list:
    """
    Find CLOSED positions that haven't been processed by the learning loop.
    
    A position is unprocessed if:
    - status = 'CLOSED'
    - pnl_pct is NOT NULL
    - features_json is NULL or doesn't contain learning_complete=true
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT position_id, token_address, pnl_pct, strategy_id, source_primary
            FROM positions
            WHERE status = 'CLOSED'
              AND pnl_pct IS NOT NULL
              AND (features_json IS NULL 
                   OR features_json NOT LIKE '%"learning_complete": true%'
                   AND features_json NOT LIKE '%"learning_complete":true%')
            ORDER BY closed_at ASC
        """).fetchall()
        
        return [dict(r) for r in rows]


def run():
    """Main entry: scan for unprocessed closures and process them."""
    _log("=" * 60)
    _log("Learning Loop START")
    
    unprocessed = scan_unprocessed_closures()
    
    if not unprocessed:
        _log("No unprocessed closures")
        return []
    
    _log(f"Found {len(unprocessed)} unprocessed closure(s)")
    
    results = []
    for pos in unprocessed:
        try:
            result = process_closed_position(pos["position_id"])
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
        _log("Interrupted by user")
    except Exception as e:
        _log(f"Learning loop crashed: {e}")
        import traceback
        _log(traceback.format_exc())
        sys.exit(1)
