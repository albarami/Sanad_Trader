#!/usr/bin/env python3
"""
Rebuild Learning Stats from Scratch

Deterministic rebuild of bandit_strategy_stats and source_ucb_stats
from all CLOSED positions using current Judge-aware penalty logic.

This ensures the system "learns from past mistakes" retroactively
after implementing stronger Judge REJECT penalties.

Process:
1. Backup current stats tables
2. Truncate stats tables
3. Replay all CLOSED positions in chronological order
4. Apply current learning_loop logic (3x beta penalty, -2.0 UCB reward for REJECT ≥85%)
5. Mark all positions as learning_complete=true

CAUTION: This is a one-time migration. Run only once after deploying
         the new Judge-aware learning penalties.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from state_store import DB_PATH

BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

BACKUP_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
BANDIT_BACKUP = BACKUP_DIR / f"bandit_strategy_stats_backup_{BACKUP_TIMESTAMP}.json"
UCB_BACKUP = BACKUP_DIR / f"source_ucb_stats_backup_{BACKUP_TIMESTAMP}.json"


def backup_stats():
    """Backup current stats before rebuild."""
    print("📦 Backing up current stats...")
    
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    
    # Backup bandit stats
    bandit_rows = conn.execute("SELECT * FROM bandit_strategy_stats").fetchall()
    bandit_data = [dict(r) for r in bandit_rows]
    BANDIT_BACKUP.write_text(json.dumps(bandit_data, indent=2))
    print(f"  ✅ Bandit stats backed up: {BANDIT_BACKUP} ({len(bandit_data)} strategies)")
    
    # Backup UCB stats
    ucb_rows = conn.execute("SELECT * FROM source_ucb_stats").fetchall()
    ucb_data = [dict(r) for r in ucb_rows]
    UCB_BACKUP.write_text(json.dumps(ucb_data, indent=2))
    print(f"  ✅ UCB stats backed up: {UCB_BACKUP} ({len(ucb_data)} sources)")
    
    conn.close()


def truncate_stats():
    """Clear all stats tables."""
    print("🗑️  Truncating stats tables...")
    
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("DELETE FROM bandit_strategy_stats")
    conn.execute("DELETE FROM source_ucb_stats")
    conn.commit()
    conn.close()
    
    print("  ✅ Stats tables cleared")


def replay_positions():
    """
    Replay all CLOSED positions and recompute stats using current logic.
    """
    print("🔄 Replaying all CLOSED positions...")
    
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    
    # Get all CLOSED positions in chronological order
    positions = conn.execute("""
        SELECT position_id, strategy, source, pnl_pct, async_analysis_json, features_json
        FROM positions
        WHERE status = 'CLOSED'
          AND pnl_pct IS NOT NULL
        ORDER BY closed_at ASC
    """).fetchall()
    
    print(f"  Found {len(positions)} CLOSED positions to replay")
    
    processed = 0
    skipped = 0
    
    for pos in positions:
        position_id = pos["position_id"]
        strategy = pos["strategy"]
        source = pos["source"] or "unknown:general"
        pnl_pct = pos["pnl_pct"]
        async_json_str = pos["async_analysis_json"]
        
        # Parse Judge verdict
        judge_reject_high_conf = False
        if async_json_str:
            try:
                async_json = json.loads(async_json_str)
                judge = async_json.get("judge", {}).get("parsed", {})
                if judge.get("verdict") == "REJECT" and judge.get("confidence", 0) >= 85:
                    judge_reject_high_conf = True
            except Exception:
                pass
        
        # Determine WIN/LOSS
        if judge_reject_high_conf:
            # Catastrophic: treat as strong loss (not just PnL-based)
            outcome = "LOSS"
            pnl_for_ucb = -2.0  # Strong negative reward
        elif pnl_pct > 0:
            outcome = "WIN"
            pnl_for_ucb = 1.0
        else:
            outcome = "LOSS"
            pnl_for_ucb = 0.0
        
        # Update bandit stats (Thompson Sampling)
        if strategy:
            # Get current stats
            row = conn.execute("""
                SELECT alpha, beta FROM bandit_strategy_stats WHERE strategy_name = ?
            """, (strategy,)).fetchone()
            
            if row:
                alpha, beta = row["alpha"], row["beta"]
            else:
                alpha, beta = 1.0, 1.0  # Uniform prior
            
            # Update based on outcome
            if outcome == "WIN":
                alpha += 1.0
            else:
                if judge_reject_high_conf:
                    beta += 3.0  # 3x penalty for Judge REJECT
                else:
                    beta += 1.0
            
            # Upsert
            conn.execute("""
                INSERT INTO bandit_strategy_stats (strategy_name, alpha, beta, last_updated)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(strategy_name) DO UPDATE SET
                    alpha = excluded.alpha,
                    beta = excluded.beta,
                    last_updated = excluded.last_updated
            """, (strategy, alpha, beta, datetime.now(timezone.utc).isoformat()))
        
        # Update UCB stats
        row = conn.execute("""
            SELECT n, reward_sum FROM source_ucb_stats WHERE source_name = ?
        """, (source,)).fetchone()
        
        if row:
            n, reward_sum = row["n"], row["reward_sum"]
        else:
            n, reward_sum = 0, 0.0
        
        n += 1
        reward_sum += pnl_for_ucb
        
        conn.execute("""
            INSERT INTO source_ucb_stats (source_name, n, reward_sum, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
                n = excluded.n,
                reward_sum = excluded.reward_sum,
                last_updated = excluded.last_updated
        """, (source, n, reward_sum, datetime.now(timezone.utc).isoformat()))
        
        processed += 1
    
    # Mark all positions as learning_complete
    conn.execute("""
        UPDATE positions
        SET features_json = json_set(
            COALESCE(features_json, '{}'),
            '$.learning_complete',
            1
        )
        WHERE status = 'CLOSED' AND pnl_pct IS NOT NULL
    """)
    
    conn.commit()
    conn.close()
    
    print(f"  ✅ Processed {processed} positions")
    print(f"  ⏭️  Skipped {skipped} positions (missing data)")


def verify_rebuild():
    """Verify rebuild results."""
    print("🔍 Verifying rebuild...")
    
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    
    bandit_count = conn.execute("SELECT COUNT(*) FROM bandit_strategy_stats").fetchone()[0]
    ucb_count = conn.execute("SELECT COUNT(*) FROM source_ucb_stats").fetchone()[0]
    learning_complete = conn.execute("""
        SELECT COUNT(*) FROM positions 
        WHERE status = 'CLOSED' 
          AND json_extract(features_json, '$.learning_complete') = 1
    """).fetchone()[0]
    
    conn.close()
    
    print(f"  ✅ Bandit strategies: {bandit_count}")
    print(f"  ✅ UCB sources: {ucb_count}")
    print(f"  ✅ Positions marked learning_complete: {learning_complete}")


def main():
    print("=" * 70)
    print("REBUILD LEARNING STATS FROM SCRATCH")
    print("=" * 70)
    print()
    print("⚠️  WARNING: This will:")
    print("  1. Backup current stats to backups/")
    print("  2. Truncate bandit_strategy_stats and source_ucb_stats")
    print("  3. Replay all CLOSED positions with Judge-aware penalties")
    print()
    
    response = input("Proceed with rebuild? (yes/no): ")
    if response.lower() != "yes":
        print("❌ Aborted.")
        return
    
    print()
    backup_stats()
    truncate_stats()
    replay_positions()
    verify_rebuild()
    
    print()
    print("=" * 70)
    print("✅ REBUILD COMPLETE")
    print("=" * 70)
    print()
    print("The system has retroactively learned from all past trades using")
    print("the new Judge-aware penalty logic (3x beta penalty, -2.0 UCB reward).")
    print()
    print(f"Backups saved to:")
    print(f"  - {BANDIT_BACKUP}")
    print(f"  - {UCB_BACKUP}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
