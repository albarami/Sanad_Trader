#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Ticket 5: Learning Loop

Deterministic Python. No LLMs.

When a position is CLOSED with PnL data, this module:
1. Updates bandit_strategy_stats (Thompson Sampling Beta params)
2. Updates source_ucb_stats (UCB1 source grading)

All state is in SQLite (single source of truth for v3.1).

Functions:
- process_closed_position(position_id) — Main entry: reads position, updates stats
- update_bandit_stats(strategy_id, regime_tag, is_win) — Thompson alpha/beta
- update_source_stats(source_id, is_win) — UCB1 reward accumulation
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


def _log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}\n"
    print(line.strip())
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


# ─────────────────────────────────────────────
# BANDIT STRATEGY STATS (Thompson Sampling)
# ─────────────────────────────────────────────

def update_bandit_stats(strategy_id: str, regime_tag: str, is_win: bool) -> dict:
    """
    Update Thompson Sampling Beta(alpha, beta) for a strategy+regime pair.
    
    - Win: alpha += 1
    - Loss: beta += 1
    - n += 1 always
    
    Creates row if not exists (prior: alpha=1, beta=1, n=0).
    
    Returns updated row as dict.
    """
    if not strategy_id:
        _log("WARNING: update_bandit_stats called with empty strategy_id, skipping")
        return {}
    
    regime_tag = regime_tag or "unknown"
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with get_connection() as conn:
        # Check if row exists
        row = conn.execute("""
            SELECT alpha, beta, n FROM bandit_strategy_stats
            WHERE strategy_id = ? AND regime_tag = ?
        """, (strategy_id, regime_tag)).fetchone()
        
        if row:
            alpha = row["alpha"]
            beta = row["beta"]
            n = row["n"]
        else:
            # Prior: Beta(1, 1) = uniform
            alpha = 1.0
            beta = 1.0
            n = 0
        
        # Update
        if is_win:
            alpha += 1.0
        else:
            beta += 1.0
        n += 1
        
        # Upsert
        conn.execute("""
            INSERT INTO bandit_strategy_stats (strategy_id, regime_tag, alpha, beta, n, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, regime_tag) DO UPDATE SET
                alpha = excluded.alpha,
                beta = excluded.beta,
                n = excluded.n,
                last_updated = excluded.last_updated
        """, (strategy_id, regime_tag, alpha, beta, n, now_iso))
        
        conn.commit()
    
    expected = alpha / (alpha + beta)
    _log(f"Bandit: {strategy_id}/{regime_tag} {'WIN' if is_win else 'LOSS'} → "
         f"Alpha={alpha:.0f} Beta={beta:.0f} n={n} E[v]={expected:.3f}")
    
    return {
        "strategy_id": strategy_id,
        "regime_tag": regime_tag,
        "alpha": alpha,
        "beta": beta,
        "n": n,
        "expected_value": expected
    }


# ─────────────────────────────────────────────
# SOURCE UCB STATS (UCB1 Source Grading)
# ─────────────────────────────────────────────

def update_source_stats(source_id: str, is_win: bool) -> dict:
    """
    Update UCB1 source stats: n += 1, reward_sum += (1 if win else 0).
    
    Creates row if not exists (n=0, reward_sum=0).
    
    Returns updated row as dict.
    """
    if not source_id:
        _log("WARNING: update_source_stats called with empty source_id, skipping")
        return {}
    
    now_iso = datetime.now(timezone.utc).isoformat()
    reward = 1.0 if is_win else 0.0
    
    with get_connection() as conn:
        # Check if row exists
        row = conn.execute("""
            SELECT n, reward_sum FROM source_ucb_stats
            WHERE source_id = ?
        """, (source_id,)).fetchone()
        
        if row:
            n = row["n"] + 1
            reward_sum = row["reward_sum"] + reward
        else:
            n = 1
            reward_sum = reward
        
        # Upsert
        conn.execute("""
            INSERT INTO source_ucb_stats (source_id, n, reward_sum, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                n = excluded.n,
                reward_sum = excluded.reward_sum,
                last_updated = excluded.last_updated
        """, (source_id, n, reward_sum, now_iso))
        
        conn.commit()
    
    win_rate = reward_sum / n if n > 0 else 0.0
    _log(f"Source: {source_id} {'WIN' if is_win else 'LOSS'} → "
         f"n={n} reward_sum={reward_sum:.0f} win_rate={win_rate:.2%}")
    
    return {
        "source_id": source_id,
        "n": n,
        "reward_sum": reward_sum,
        "win_rate": win_rate
    }


# ─────────────────────────────────────────────
# POSITION PROCESSING
# ─────────────────────────────────────────────

def process_closed_position(position_id: str) -> dict:
    """
    Process a single CLOSED position:
    1. Read position from DB
    2. Determine win/loss from pnl_pct
    3. Update bandit_strategy_stats
    4. Update source_ucb_stats
    5. Mark position as learning_complete
    
    Returns summary dict or raises on error.
    """
    with get_connection() as conn:
        row = conn.execute("""
            SELECT position_id, strategy_id, regime_tag, source_primary,
                   pnl_usd, pnl_pct, status, exit_reason, token_address
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
    
    is_win = pos["pnl_pct"] > 0
    strategy_id = pos["strategy_id"]
    regime_tag = pos["regime_tag"] or "unknown"
    source_id = pos["source_primary"] or "unknown"
    
    _log(f"Processing position {position_id}: {pos['token_address']} "
         f"{'WIN' if is_win else 'LOSS'} ({pos['pnl_pct']:+.2%}) "
         f"strategy={strategy_id} source={source_id}")
    
    # 1. Update Thompson Sampling bandit stats
    bandit_result = update_bandit_stats(strategy_id, regime_tag, is_win)
    
    # 2. Update UCB1 source stats
    source_result = update_source_stats(source_id, is_win)
    
    # 3. Mark position as learning_complete in features_json
    with get_connection() as conn:
        # Read existing features_json
        feat_row = conn.execute("""
            SELECT features_json FROM positions WHERE position_id = ?
        """, (position_id,)).fetchone()
        
        features = {}
        if feat_row and feat_row["features_json"]:
            try:
                features = json.loads(feat_row["features_json"])
            except json.JSONDecodeError:
                features = {}
        
        features["learning_complete"] = True
        features["learning_at"] = datetime.now(timezone.utc).isoformat()
        
        conn.execute("""
            UPDATE positions
            SET features_json = ?,
                updated_at = ?
            WHERE position_id = ?
        """, (json.dumps(features), datetime.now(timezone.utc).isoformat(), position_id))
        
        conn.commit()
    
    _log(f"Position {position_id} learning complete")
    
    return {
        "position_id": position_id,
        "is_win": is_win,
        "pnl_pct": pos["pnl_pct"],
        "bandit": bandit_result,
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
