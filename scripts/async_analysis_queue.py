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
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

from state_store import get_connection, DBBusyError

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
CONFIG_PATH = BASE_DIR / "config" / "thresholds.yaml"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "async_analysis_queue.log"

# Load config
try:
    with open(CONFIG_PATH, "r") as f:
        CONFIG = yaml.safe_load(f)
        COLD_PATH_CONFIG = CONFIG.get("cold_path", {})
except Exception as e:
    print(f"ERROR: Failed to load config: {e}")
    sys.exit(1)

# Cold path settings
MODEL = COLD_PATH_CONFIG.get("model", "anthropic/claude-opus-4-6")
JUDGE_MODEL = COLD_PATH_CONFIG.get("judge_model", "openai/gpt-5.2")
TIMEOUT_SECONDS = COLD_PATH_CONFIG.get("timeout_seconds", 300)
MAX_RETRIES = COLD_PATH_CONFIG.get("max_retries", 3)
PARALLEL_BULL_BEAR = COLD_PATH_CONFIG.get("parallel_bull_bear", True)
CATASTROPHIC_THRESHOLD = COLD_PATH_CONFIG.get("catastrophic_confidence_threshold", 85)

RETRY_DELAYS = [300, 900, 3600]  # 5m, 15m, 60m in seconds


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
# LLM CALL STUB (TODO: Replace with real OpenAI/Anthropic calls)
# ─────────────────────────────────────────────

def _call_llm(prompt: str, model: str, timeout: int = 60, force_reject: bool = False) -> dict:
    """
    Stub LLM call. Replace with real API integration.
    
    For now, returns deterministic responses for testing.
    """
    import hashlib
    
    # Deterministic response based on prompt hash
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
    
    # Check role markers (prioritize judge > bull/bear)
    is_judge = "al-muhasbi" in prompt.lower() or "judge" in prompt.lower()
    is_bull = "al-baqarah" in prompt.lower() or ("bull" in prompt.lower() and not is_judge)
    is_bear = "al-dahhak" in prompt.lower() or ("bear" in prompt.lower() and not is_judge)
    
    if is_judge:
        # Judge verdict
        if force_reject or "FORCE_REJECT" in prompt:
            return {
                "verdict": "REJECT",
                "confidence": 90,
                "bias_flags": [],
                "risk_assessment": "HIGH",
                "reasoning": "Forced rejection for catastrophic test",
                "model": model
            }
        return {
            "verdict": "APPROVE",
            "confidence": 80,
            "bias_flags": [],
            "risk_assessment": "MODERATE",
            "reasoning": f"Judge verdict: Debate balanced (hash: {prompt_hash[:8]})",
            "model": model
        }
    elif is_bull:
        return {
            "verdict": "BUY",
            "confidence": 75,
            "rationale": f"Bull analysis: Strong fundamentals (hash: {prompt_hash[:8]})",
            "model": model
        }
    elif is_bear:
        return {
            "verdict": "SKIP",
            "confidence": 60,
            "rationale": f"Bear analysis: High volatility concerns (hash: {prompt_hash[:8]})",
            "model": model
        }
    else:
        # Sanad verification
        return {
            "trust_score": 85,
            "rugpull_flags": [],
            "sybil_risk": "LOW",
            "model": model
        }


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
            
            # Atomic update: only succeed if still PENDING and ready
            cursor = conn.execute("""
                UPDATE async_tasks
                SET status = 'RUNNING',
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE task_id = ? 
                  AND status = 'PENDING'
                  AND next_run_at <= ?
            """, (now_iso, task_id, now_iso))
            
            conn.commit()
            claimed = cursor.rowcount > 0
            
            if claimed:
                _log(f"Claimed task {task_id}")
            
            return claimed
    except Exception as e:
        _log(f"Error claiming task {task_id}: {e}")
        return False


def mark_task_done(task_id: str):
    """Mark task as DONE."""
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            conn.execute("""
                UPDATE async_tasks
                SET status = 'DONE',
                    updated_at = ?
                WHERE task_id = ?
            """, (now_iso, task_id))
            
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
                _log(f"Task {task_id} FAILED permanently after {attempts} attempts")
                
                # Mark position as permanently failed
                row = conn.execute("SELECT entity_id FROM async_tasks WHERE task_id = ?", (task_id,)).fetchone()
                if row:
                    conn.execute("""
                        UPDATE positions
                        SET risk_flag = 'FLAG_ASYNC_FAILED_PERMANENT'
                        WHERE position_id = ?
                    """, (row["entity_id"],))
                    _log(f"Position {row['entity_id']} marked FLAG_ASYNC_FAILED_PERMANENT")
            else:
                # Schedule retry
                delay_sec = RETRY_DELAYS[attempts - 1] if attempts <= len(RETRY_DELAYS) else RETRY_DELAYS[-1]
                next_run = datetime.now(timezone.utc) + timedelta(seconds=delay_sec)
                next_run_iso = next_run.isoformat()
                
                conn.execute("""
                    UPDATE async_tasks
                    SET status = 'PENDING',
                        last_error = ?,
                        next_run_at = ?,
                        updated_at = ?
                    WHERE task_id = ?
                """, (error_msg, next_run_iso, now_iso, task_id))
                _log(f"Task {task_id} retry scheduled in {delay_sec}s (attempt {attempts}/{MAX_RETRIES})")
            
            conn.commit()
    except Exception as e:
        _log(f"Error marking task {task_id} failed: {e}")


# ─────────────────────────────────────────────
# ANALYSIS FUNCTIONS
# ─────────────────────────────────────────────

def run_sanad_verification(position_data: dict, signal_payload: dict) -> dict:
    """
    Run Sanad verification (rugpull, sybil, trust).
    
    Uses Opus model configured in cold_path.model.
    """
    _log(f"Running Sanad verification for position {position_data.get('position_id', '?')}")
    
    prompt = f"""
Analyze token {position_data.get('token_address')} for Sanad verification:
- Chain: {position_data.get('chain')}
- Entry price: {position_data.get('entry_price')}

Provide:
1. Trust score (0-100)
2. Rugpull flags (list)
3. Sybil risk (LOW/MEDIUM/HIGH)
4. Source reliability assessment
"""
    
    try:
        result = _call_llm(prompt, MODEL, TIMEOUT_SECONDS)
        return {
            "sanad_verification": result,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        _log(f"Sanad verification error: {e}")
        raise


def run_bull_analysis(signal_payload: dict) -> dict:
    """Run Bull argument (pro-trade)."""
    _log("Running Bull analysis")
    
    prompt = f"""
As Al-Baqarah (Bull), argue FOR this trade:
{json.dumps(signal_payload, indent=2)}

Provide verdict, confidence (0-100), and rationale.
"""
    
    result = _call_llm(prompt, MODEL, TIMEOUT_SECONDS)
    return {
        "bull_argument": result,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def run_bear_analysis(signal_payload: dict) -> dict:
    """Run Bear argument (anti-trade)."""
    _log("Running Bear analysis")
    
    prompt = f"""
As Al-Dahhak (Bear), argue AGAINST this trade:
{json.dumps(signal_payload, indent=2)}

Provide verdict, confidence (0-100), and rationale.
"""
    
    result = _call_llm(prompt, MODEL, TIMEOUT_SECONDS)
    return {
        "bear_argument": result,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def run_judge_verdict(signal_payload: dict, bull: dict, bear: dict) -> dict:
    """
    Run Judge verdict (GPT Codex reviews full evidence).
    """
    _log("Running Judge verdict")
    
    # Check for forced rejection in signal (for catastrophic tests)
    force_reject = signal_payload.get("_test_force_reject", False)
    
    prompt = f"""
As Al-Muhasbi (Judge), review the debate:

Bull: {json.dumps(bull, indent=2)}
Bear: {json.dumps(bear, indent=2)}

{"FORCE_REJECT - This is a catastrophic test case." if force_reject else ""}

Provide:
- verdict (APPROVE/REJECT)
- confidence (0-100)
- bias_flags (list)
- risk_assessment (LOW/MODERATE/HIGH)
- reasoning (string)
"""
    
    result = _call_llm(prompt, JUDGE_MODEL, TIMEOUT_SECONDS, force_reject=force_reject)
    return {
        "judge_verdict": result,
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
    attempts = task["attempts"]
    
    _log(f"Processing task {task_id} (type={task_type}, position={position_id}, attempt={attempts})")
    
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
        
        # Build signal payload (reconstruct from position metadata)
        signal_id = position_data.get("signal_id", "")
        force_reject = "CATASTROPHIC" in str(signal_id)
        
        _log(f"Signal ID: {signal_id}")
        _log(f"Force reject: {force_reject}")
        
        signal_payload = {
            "token_address": position_data["token_address"],
            "chain": position_data["chain"],
            "entry_price": position_data["entry_price"],
            "size_usd": position_data["size_usd"],
            "strategy_id": position_data["strategy_id"],
            # Check for catastrophic test marker
            "_test_force_reject": force_reject
        }
        
        # Run Cold Path analysis
        start = time.perf_counter()
        
        # Step 1: Sanad verification
        sanad_result = run_sanad_verification(position_data, signal_payload)
        
        # Step 2: Bull/Bear debate (parallel if configured)
        if PARALLEL_BULL_BEAR:
            with ThreadPoolExecutor(max_workers=2) as executor:
                bull_future = executor.submit(run_bull_analysis, signal_payload)
                bear_future = executor.submit(run_bear_analysis, signal_payload)
                
                bull_result = bull_future.result(timeout=TIMEOUT_SECONDS)
                bear_result = bear_future.result(timeout=TIMEOUT_SECONDS)
        else:
            bull_result = run_bull_analysis(signal_payload)
            bear_result = run_bear_analysis(signal_payload)
        
        # Step 3: Judge verdict
        judge_result = run_judge_verdict(signal_payload, bull_result, bear_result)
        
        duration = time.perf_counter() - start
        
        # Combine results with stable schema
        analysis_result = {
            "sanad": sanad_result,
            "bull": bull_result,
            "bear": bear_result,
            "judge": judge_result,
            "meta": {
                "model": MODEL,
                "judge_model": JUDGE_MODEL,
                "started_at": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "duration_sec": duration
            }
        }
        
        # Update position record
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            # Check if judge flagged catastrophic risk
            risk_flag = None
            verdict = judge_result["judge_verdict"]
            if verdict["verdict"] == "REJECT":
                confidence = verdict["confidence"]
                if confidence >= CATASTROPHIC_THRESHOLD:
                    risk_flag = "FLAG_JUDGE_HIGH_CONF_REJECT"
                    _log(f"CATASTROPHIC: Judge rejected with {confidence}% confidence")
            
            conn.execute("""
                UPDATE positions
                SET async_analysis_json = ?,
                    async_analysis_complete = 1,
                    risk_flag = COALESCE(?, risk_flag),
                    updated_at = ?
                WHERE position_id = ?
            """, (json.dumps(analysis_result), risk_flag, now_iso, position_id))
            
            conn.commit()
        
        _log(f"Task {task_id} completed in {duration:.1f}s (judge={verdict['verdict']}, confidence={verdict['confidence']})")
        
        # Mark task done
        mark_task_done(task_id)
        
    except Exception as e:
        _log(f"Task {task_id} failed: {e}")
        import traceback
        _log(traceback.format_exc())
        
        # Mark failed (will retry if attempts remain)
        mark_task_failed(task_id, str(e), attempts + 1)


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def main():
    """Main worker loop: poll, claim, process."""
    _log("=" * 60)
    _log(f"Async Analysis Queue Worker START (model={MODEL}, judge={JUDGE_MODEL})")
    
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
            _log(f"Task {task_id} not claimed (already taken or not ready)")
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
