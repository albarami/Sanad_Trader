#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Cold Path: Async Analysis Queue Worker

Polls async_tasks table for PENDING tasks and processes them:
1. Sanad verification (rugpull, sybil, trust scoring)
2. Bull/Bear debate (parallel LLM calls)
3. Judge verdict (GPT Codex)

Updates positions.async_analysis_json with results.

Author: Sanad Trader v3.1
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

from state_store import get_connection, DBBusyError

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "async_analysis_queue.log"

MAX_RETRIES = 3
RETRY_DELAYS = [300, 900, 3600]  # 5m, 15m, 60m in seconds

# Cold path model config (unified)
# TODO: Load from thresholds.yaml or config
COLD_PATH_MODELS = {
    "sanad": "anthropic/claude-opus-4-6",
    "bull": "anthropic/claude-opus-4-6",
    "bear": "anthropic/claude-opus-4-6",
    "judge": "openai/gpt-5.2"
}


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

def _log(msg: str):
    """Append timestamped log message."""
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}\n"
    print(line.strip())
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


# ─────────────────────────────────────────────
# TASK PROCESSING
# ─────────────────────────────────────────────

def poll_pending_tasks():
    """
    Poll async_tasks for PENDING tasks ready to run.
    
    Returns list of task dicts.
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            rows = conn.execute("""
                SELECT task_id, entity_id, task_type, 
                       attempts, created_at, next_run_at
                FROM async_tasks
                WHERE status = 'PENDING'
                  AND next_run_at <= ?
                ORDER BY next_run_at ASC
                LIMIT 10
            """, (now_iso,)).fetchall()
            
            return [dict(row) for row in rows]
    except DBBusyError:
        _log("DB busy during poll, will retry next cycle")
        return []
    except Exception as e:
        _log(f"Error polling tasks: {e}")
        return []


def claim_task(task_id: str) -> bool:
    """
    Atomically claim task by updating status to RUNNING.
    
    Returns True if successfully claimed.
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            # Atomic update: only succeed if still PENDING
            cursor = conn.execute("""
                UPDATE async_tasks
                SET status = 'RUNNING',
                    updated_at = ?
                WHERE task_id = ? AND status = 'PENDING'
            """, (now_iso, task_id))
            
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        _log(f"Error claiming task {task_id}: {e}")
        return False


def mark_task_done(task_id: str, result_json: dict):
    """Mark task as DONE and store result."""
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            # Store result in last_error (repurpose as result field for now)
            # TODO: Add result_json column to async_tasks schema
            conn.execute("""
                UPDATE async_tasks
                SET status = 'DONE',
                    last_error = ?,
                    updated_at = ?
                WHERE task_id = ?
            """, (json.dumps(result_json)[:500], now_iso, task_id))
            
            conn.commit()
            _log(f"Task {task_id} marked DONE")
    except Exception as e:
        _log(f"Error marking task {task_id} done: {e}")


def mark_task_failed(task_id: str, error_msg: str, attempts: int):
    """
    Mark task as FAILED or schedule retry.
    
    If attempts < MAX_RETRIES, reset to PENDING with backoff.
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            if attempts >= MAX_RETRIES:
                # Final failure
                conn.execute("""
                    UPDATE async_tasks
                    SET status = 'FAILED',
                        last_error = ?,
                        updated_at = ?
                    WHERE task_id = ?
                """, (error_msg, now_iso, task_id))
                _log(f"Task {task_id} FAILED after {attempts} attempts")
            else:
                # Schedule retry
                delay_sec = RETRY_DELAYS[attempts] if attempts < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
                next_run = datetime.now(timezone.utc) + timedelta(seconds=delay_sec)
                next_run_iso = next_run.isoformat()
                
                conn.execute("""
                    UPDATE async_tasks
                    SET status = 'PENDING',
                        attempts = ?,
                        last_error = ?,
                        next_run_at = ?,
                        updated_at = ?
                    WHERE task_id = ?
                """, (attempts + 1, error_msg, next_run_iso, now_iso, task_id))
                _log(f"Task {task_id} retry scheduled in {delay_sec}s (attempt {attempts+1})")
            
            conn.commit()
    except Exception as e:
        _log(f"Error marking task {task_id} failed: {e}")


# ─────────────────────────────────────────────
# ANALYSIS FUNCTIONS (STUBS)
# ─────────────────────────────────────────────

def run_sanad_verification(position_data: dict) -> dict:
    """
    Run Sanad verification (rugpull, sybil, trust).
    
    TODO: Replace stub with actual Opus call to sanad verifier.
    """
    _log(f"Running Sanad verification for position {position_data.get('position_id', '?')}")
    
    # Stub result
    return {
        "sanad_verification": {
            "rugpull_flags": [],
            "sybil_risk": "LOW",
            "trust_score": 85,
            "takhrij_sources": ["birdeye", "dexscreener"],
            "ucb1_scores": {}
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def run_bull_bear_debate(position_data: dict) -> dict:
    """
    Run Bull/Bear adversarial debate (parallel).
    
    TODO: Replace stub with actual Opus parallel calls.
    """
    _log(f"Running Bull/Bear debate for position {position_data.get('position_id', '?')}")
    
    # Stub result
    return {
        "bull_argument": {
            "verdict": "BUY",
            "confidence": 0.75,
            "rationale": "Strong fundamentals, solid liquidity"
        },
        "bear_argument": {
            "verdict": "SKIP",
            "confidence": 0.60,
            "rationale": "High volatility, recent whale activity"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def run_judge_verdict(position_data: dict, sanad: dict, debate: dict) -> dict:
    """
    Run Judge verdict (GPT Codex reviews full evidence).
    
    TODO: Replace stub with actual GPT-5.2 call.
    """
    _log(f"Running Judge verdict for position {position_data.get('position_id', '?')}")
    
    # Stub result
    return {
        "judge_verdict": {
            "decision": "APPROVE",
            "confidence": 0.80,
            "bias_flags": [],
            "risk_assessment": "MODERATE",
            "reasoning": "Debate balanced, Sanad verification clean"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def process_task(task: dict):
    """
    Process a single async task.
    
    Runs full Cold Path analysis and updates position record.
    """
    task_id = task["task_id"]
    position_id = task["entity_id"]  # entity_id contains position_id
    task_type = task["task_type"]
    
    _log(f"Processing task {task_id} (type={task_type}, position={position_id})")
    
    try:
        # Load position data
        with get_connection() as conn:
            row = conn.execute("""
                SELECT position_id, token_address, entry_price, 
                       size_usd, decision_id, signal_id, chain, strategy_id
                FROM positions
                WHERE position_id = ?
            """, (position_id,)).fetchone()
            
            if not row:
                raise ValueError(f"Position {position_id} not found")
            
            position_data = dict(row)
        
        # Run Cold Path analysis
        start = time.perf_counter()
        
        # Step 1: Sanad verification
        sanad_result = run_sanad_verification(position_data)
        
        # Step 2: Bull/Bear debate (parallel in production)
        debate_result = run_bull_bear_debate(position_data)
        
        # Step 3: Judge verdict
        judge_result = run_judge_verdict(position_data, sanad_result, debate_result)
        
        duration = time.perf_counter() - start
        
        # Combine results
        analysis_result = {
            "sanad": sanad_result,
            "debate": debate_result,
            "judge": judge_result,
            "analysis_duration_sec": duration,
            "completed_at": datetime.now(timezone.utc).isoformat()
        }
        
        # Update position record
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            # Check if judge flagged catastrophic risk
            risk_flag = None
            if judge_result["judge_verdict"]["decision"] == "REJECT":
                confidence = judge_result["judge_verdict"]["confidence"]
                if confidence >= 0.9:
                    risk_flag = "FLAG_JUDGE_HIGH_CONF_REJECT"
            
            conn.execute("""
                UPDATE positions
                SET async_analysis_json = ?,
                    async_analysis_complete = 1,
                    risk_flag = ?,
                    updated_at = ?
                WHERE position_id = ?
            """, (json.dumps(analysis_result), risk_flag, now_iso, position_id))
            
            conn.commit()
        
        _log(f"Task {task_id} completed in {duration:.1f}s (judge={judge_result['judge_verdict']['decision']})")
        
        # Mark task done
        mark_task_done(task_id, analysis_result)
        
    except Exception as e:
        _log(f"Task {task_id} failed: {e}")
        import traceback
        _log(traceback.format_exc())
        
        # Mark failed (will retry if attempts remain)
        mark_task_failed(task_id, str(e), task["attempts"])


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def main():
    """Main worker loop: poll, claim, process."""
    _log("=" * 60)
    _log("Async Analysis Queue Worker START")
    
    # Poll for pending tasks
    tasks = poll_pending_tasks()
    
    if not tasks:
        _log("No pending tasks")
        return
    
    _log(f"Found {len(tasks)} pending task(s)")
    
    # Process each task
    for task in tasks:
        task_id = task["task_id"]
        
        # Attempt to claim
        if not claim_task(task_id):
            _log(f"Task {task_id} already claimed by another worker")
            continue
        
        # Process
        process_task(task)
    
    _log("Async Analysis Queue Worker END")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("Worker interrupted by user")
    except Exception as e:
        _log(f"Worker crashed: {e}")
        import traceback
        _log(traceback.format_exc())
        sys.exit(1)
