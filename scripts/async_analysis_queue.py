#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Cold Path: Async Analysis Queue Worker

Polls async_tasks table for PENDING tasks and processes them:
1. Sanad verification (rugpull, sybil, trust scoring)
2. Bull/Bear debate (parallel LLM calls)
3. Judge verdict (GPT Codex)

Updates positions.async_analysis_json with results.

Author: Sanad Trader v3.1
Ticket 4 FIX: Real LLM calls, safe claiming, wall-clock timestamps, production catastrophic logic
"""

import json
import os
import sys
import time
import yaml
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

from state_store import get_connection, DBBusyError
import llm_client

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
CONFIG_PATH = BASE_DIR / "config" / "thresholds.yaml"
PROMPTS_DIR = BASE_DIR / "prompts"
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
MODEL = COLD_PATH_CONFIG.get("model", "claude-opus-4-6")
JUDGE_MODEL = COLD_PATH_CONFIG.get("judge_model", "gpt-5.2")
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
# PROMPT LOADING
# ─────────────────────────────────────────────

def load_prompt(name: str) -> str:
    """Load prompt from prompts/ directory."""
    prompt_path = PROMPTS_DIR / f"{name}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt not found: {prompt_path}")
    return prompt_path.read_text()


# Load prompts at startup
try:
    SANAD_PROMPT = load_prompt("sanad-verifier")
    BULL_PROMPT = load_prompt("bull-albaqarah")
    BEAR_PROMPT = load_prompt("bear-aldahhak")
    JUDGE_PROMPT = load_prompt("judge-almuhasbi")
except Exception as e:
    _log(f"ERROR: Failed to load prompts: {e}")
    sys.exit(1)


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


def claim_task(task_id: str) -> dict:
    """
    Atomically claim task by updating status to RUNNING.
    
    Returns task dict with authoritative DB values if claimed, else None.
    
    FIX B: Safe claiming with single transaction
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            # Atomic update: increment attempts, set RUNNING
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
            
            if cursor.rowcount == 0:
                return None
            
            # Fetch authoritative DB values
            row = conn.execute("""
                SELECT task_id, entity_id, task_type, attempts, created_at
                FROM async_tasks
                WHERE task_id = ?
            """, (task_id,)).fetchone()
            
            if not row:
                return None
            
            task = dict(row)
            _log(f"Claimed task {task_id} (attempt {task['attempts']})")
            return task
            
    except Exception as e:
        _log(f"Error claiming task {task_id}: {e}")
        return None


def mark_task_done(task_id: str):
    """Mark task as DONE."""
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            conn.execute("""
                UPDATE async_tasks
                SET status = 'DONE',
                    updated_at = ?,
                    last_error = NULL
                WHERE task_id = ?
                  AND status = 'RUNNING'
            """, (now_iso, task_id))
            
            conn.commit()
            _log(f"Task {task_id} marked DONE")
    except Exception as e:
        _log(f"Error marking task {task_id} done: {e}")


def mark_task_failed(task_id: str, error_msg: str, attempts: int):
    """
    Mark task as FAILED or schedule retry.
    
    If attempts < MAX_RETRIES, reset to PENDING with backoff.
    
    FIX B: Use DB attempts value (already incremented at claim)
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
                # Schedule retry with exponential backoff
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
# ANALYSIS FUNCTIONS (FIX A: Real LLM calls)
# ─────────────────────────────────────────────

def run_sanad_verification(position_data: dict, signal_payload: dict) -> dict:
    """
    Run Sanad verification (rugpull, sybil, trust).
    
    FIX A: Real Claude API call with structured output parsing.
    """
    position_id = position_data.get('position_id', '?')
    token_symbol = position_data.get('token_address', '?')
    
    _log(f"Running Sanad verification for position {position_id}")
    
    # Build user message with token context
    user_msg = f"""
Token: {position_data.get('token_address')}
Chain: {position_data.get('chain')}
Entry price: {position_data.get('entry_price')}
Size: ${position_data.get('size_usd')}
Strategy: {position_data.get('strategy_id')}

Analyze this token for Sanad verification. Return JSON only with this exact schema:
{{
  "trust_score": <int 0-100>,
  "rugpull_flags": [<string list>],
  "sybil_risk": "<LOW|MEDIUM|HIGH>",
  "source_reliability": "<string>",
  "reasoning": "<string>"
}}
"""
    
    try:
        raw = llm_client.call_claude(
            SANAD_PROMPT + "\n\nReturn JSON only with the schema above.",
            user_msg,
            model=MODEL,
            max_tokens=2000,
            stage="cold_sanad",
            token_symbol=token_symbol
        )
        
        if not raw:
            raise RuntimeError("Sanad API call returned None")
        
        parsed = llm_client.parse_json_failsafe(raw)
        if not parsed:
            raise RuntimeError("Failed to parse Sanad JSON response")
        
        return {
            "raw": raw,
            "parsed": parsed,
            "model": MODEL,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        _log(f"Sanad verification error: {e}")
        raise


def run_bull_analysis(signal_payload: dict, token_symbol: str) -> dict:
    """
    Run Bull argument (pro-trade).
    
    FIX A: Real Claude API call with structured output parsing.
    """
    _log("Running Bull analysis")
    
    user_msg = f"""
{json.dumps(signal_payload, indent=2)}

Argue FOR this trade as Al-Baqarah (Bull). Return JSON only:
{{
  "verdict": "<BUY|SKIP>",
  "confidence": <int 0-100>,
  "rationale": "<string>",
  "key_strengths": [<string list>]
}}
"""
    
    try:
        raw = llm_client.call_claude(
            BULL_PROMPT + "\n\nReturn JSON only with the schema above.",
            user_msg,
            model=MODEL,
            max_tokens=2000,
            stage="cold_bull",
            token_symbol=token_symbol
        )
        
        if not raw:
            raise RuntimeError("Bull API call returned None")
        
        parsed = llm_client.parse_json_failsafe(raw)
        if not parsed:
            raise RuntimeError("Failed to parse Bull JSON response")
        
        return {
            "raw": raw,
            "parsed": parsed,
            "model": MODEL,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        _log(f"Bull analysis error: {e}")
        raise


def run_bear_analysis(signal_payload: dict, token_symbol: str) -> dict:
    """
    Run Bear argument (anti-trade).
    
    FIX A: Real Claude API call with structured output parsing.
    """
    _log("Running Bear analysis")
    
    user_msg = f"""
{json.dumps(signal_payload, indent=2)}

Argue AGAINST this trade as Al-Dahhak (Bear). Return JSON only:
{{
  "verdict": "<SKIP|BUY>",
  "confidence": <int 0-100>,
  "rationale": "<string>",
  "key_risks": [<string list>]
}}
"""
    
    try:
        raw = llm_client.call_claude(
            BEAR_PROMPT + "\n\nReturn JSON only with the schema above.",
            user_msg,
            model=MODEL,
            max_tokens=2000,
            stage="cold_bear",
            token_symbol=token_symbol
        )
        
        if not raw:
            raise RuntimeError("Bear API call returned None")
        
        parsed = llm_client.parse_json_failsafe(raw)
        if not parsed:
            raise RuntimeError("Failed to parse Bear JSON response")
        
        return {
            "raw": raw,
            "parsed": parsed,
            "model": MODEL,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        _log(f"Bear analysis error: {e}")
        raise


def run_judge_verdict(signal_payload: dict, sanad: dict, bull: dict, bear: dict, token_symbol: str) -> dict:
    """
    Run Judge verdict (GPT reviews full evidence).
    
    FIX A: Real OpenAI API call with structured output parsing.
    """
    _log("Running Judge verdict")
    
    user_msg = f"""
Review the following analysis for trade decision:

Signal:
{json.dumps(signal_payload, indent=2)}

Sanad:
{json.dumps(sanad.get('parsed', {}), indent=2)}

Bull:
{json.dumps(bull.get('parsed', {}), indent=2)}

Bear:
{json.dumps(bear.get('parsed', {}), indent=2)}

As Al-Muhasbi (Judge), provide your verdict. Return JSON only:
{{
  "verdict": "<APPROVE|REJECT>",
  "confidence": <int 0-100>,
  "reasons": [<string list>],
  "key_risks": [<string list>],
  "bias_flags": [<string list>],
  "risk_assessment": "<LOW|MODERATE|HIGH>",
  "reasoning": "<string>"
}}
"""
    
    try:
        raw = llm_client.call_openai(
            JUDGE_PROMPT + "\n\nReturn JSON only with the schema above.",
            user_msg,
            model=JUDGE_MODEL,
            max_tokens=2000,
            stage="cold_judge",
            token_symbol=token_symbol
        )
        
        if not raw:
            raise RuntimeError("Judge API call returned None")
        
        parsed = llm_client.parse_json_failsafe(raw)
        if not parsed:
            raise RuntimeError("Failed to parse Judge JSON response")
        
        # Validate required fields
        if "verdict" not in parsed or parsed["verdict"] not in ["APPROVE", "REJECT"]:
            raise RuntimeError(f"Invalid Judge verdict: {parsed.get('verdict')}")
        if "confidence" not in parsed or not isinstance(parsed["confidence"], int):
            raise RuntimeError(f"Invalid Judge confidence: {parsed.get('confidence')}")
        
        return {
            "raw": raw,
            "parsed": parsed,
            "model": JUDGE_MODEL,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        _log(f"Judge verdict error: {e}")
        raise


def process_task(task: dict):
    """
    Process a single async task.
    
    Runs full Cold Path analysis and updates position record.
    
    FIX C: Use wall-clock timestamps (not perf_counter)
    FIX D: Production catastrophic logic from parsed Judge output
    """
    task_id = task["task_id"]
    position_id = task["entity_id"]
    task_type = task["task_type"]
    attempts = task["attempts"]  # FIX B: Use DB value
    
    _log(f"Processing task {task_id} (type={task_type}, position={position_id}, attempt={attempts})")
    
    # FIX C: Wall-clock timestamp for started_at
    started_at = datetime.now(timezone.utc).isoformat()
    perf_start = time.perf_counter()  # Only for duration
    
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
        
        token_symbol = position_data.get("token_address", "UNKNOWN")
        
        # Build signal payload
        signal_payload = {
            "token_address": position_data["token_address"],
            "chain": position_data["chain"],
            "entry_price": position_data["entry_price"],
            "size_usd": position_data["size_usd"],
            "strategy_id": position_data["strategy_id"]
        }
        
        # Run Cold Path analysis
        _log(f"Running Cold Path for {token_symbol}")
        
        # Step 1: Sanad verification
        sanad_result = run_sanad_verification(position_data, signal_payload)
        
        # Step 2: Bull/Bear debate (parallel if configured)
        if PARALLEL_BULL_BEAR:
            with ThreadPoolExecutor(max_workers=2) as executor:
                bull_future = executor.submit(run_bull_analysis, signal_payload, token_symbol)
                bear_future = executor.submit(run_bear_analysis, signal_payload, token_symbol)
                
                bull_result = bull_future.result(timeout=TIMEOUT_SECONDS)
                bear_result = bear_future.result(timeout=TIMEOUT_SECONDS)
        else:
            bull_result = run_bull_analysis(signal_payload, token_symbol)
            bear_result = run_bear_analysis(signal_payload, token_symbol)
        
        # Step 3: Judge verdict
        judge_result = run_judge_verdict(signal_payload, sanad_result, bull_result, bear_result, token_symbol)
        
        # FIX C: Wall-clock timestamp for completed_at
        completed_at = datetime.now(timezone.utc).isoformat()
        duration_sec = time.perf_counter() - perf_start
        
        # Combine results
        analysis_result = {
            "sanad": sanad_result,
            "bull": bull_result,
            "bear": bear_result,
            "judge": judge_result,
            "meta": {
                "model": MODEL,
                "judge_model": JUDGE_MODEL,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_sec": round(duration_sec, 2)
            }
        }
        
        # FIX D: Production catastrophic flagging from parsed Judge output
        risk_flag = None
        judge_parsed = judge_result.get("parsed", {})
        verdict = judge_parsed.get("verdict")
        confidence = judge_parsed.get("confidence", 0)
        
        if verdict == "REJECT" and confidence >= CATASTROPHIC_THRESHOLD:
            risk_flag = "FLAG_JUDGE_HIGH_CONF_REJECT"
            _log(f"CATASTROPHIC: Judge rejected {token_symbol} with {confidence}% confidence (threshold={CATASTROPHIC_THRESHOLD})")
        
        # Update position record
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            conn.execute("""
                UPDATE positions
                SET async_analysis_json = ?,
                    async_analysis_complete = 1,
                    risk_flag = COALESCE(?, risk_flag),
                    updated_at = ?
                WHERE position_id = ?
            """, (json.dumps(analysis_result), risk_flag, now_iso, position_id))
            
            conn.commit()
        
        _log(f"Task {task_id} completed in {duration_sec:.1f}s (verdict={verdict}, confidence={confidence}%)")
        
        # Mark task done
        mark_task_done(task_id)
        
    except Exception as e:
        _log(f"Task {task_id} failed: {e}")
        import traceback
        _log(traceback.format_exc())
        
        # Mark failed (will retry if attempts < MAX_RETRIES)
        mark_task_failed(task_id, str(e), attempts)


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
        
        # Attempt to claim (returns authoritative DB values)
        claimed_task = claim_task(task_id)
        if not claimed_task:
            _log(f"Task {task_id} not claimed (already taken or not ready)")
            continue
        
        # Process with authoritative DB values
        process_task(claimed_task)
    
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
