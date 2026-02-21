#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Cold Path: Async Analysis Queue Worker

Polls async_tasks table for PENDING tasks and processes them:
1. Sanad verification (rugpull, sybil, trust scoring)
2. Bull/Bear debate (parallel LLM calls)
3. Judge verdict (GPT Codex)

Updates positions.async_analysis_json with results.

Author: Sanad Trader v3.1
Ticket 4 FIX v2: Strict JSON contracts, consistent attempts, debuggable failures
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
LLM_RAW_DIR = LOGS_DIR / "llm_raw"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LLM_RAW_DIR.mkdir(parents=True, exist_ok=True)

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
MODEL = COLD_PATH_CONFIG.get("model", "claude-haiku-4-5-20251001")
JUDGE_MODEL = COLD_PATH_CONFIG.get("judge_model", "gpt-5.2")
TIMEOUT_SECONDS = COLD_PATH_CONFIG.get("timeout_seconds", 300)
MAX_RETRIES = COLD_PATH_CONFIG.get("max_retries", 3)
PARALLEL_BULL_BEAR = COLD_PATH_CONFIG.get("parallel_bull_bear", True)
CATASTROPHIC_THRESHOLD = COLD_PATH_CONFIG.get("catastrophic_confidence_threshold", 85)

# FIX B: Exact backoff schedule
RETRY_DELAYS = {
    1: 300,   # 5 minutes
    2: 900,   # 15 minutes
    3: 3600,  # 60 minutes
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


def _dump_raw_llm(task_id: str, stage: str, raw_text: str):
    """
    Dump raw LLM response to file for debugging.
    
    FIX C: Operational telemetry for parse failures
    """
    try:
        filename = LLM_RAW_DIR / f"{stage}_{task_id}.txt"
        with open(filename, "w") as f:
            f.write(f"Task: {task_id}\n")
            f.write(f"Stage: {stage}\n")
            f.write(f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n")
            f.write("=" * 60 + "\n")
            f.write(raw_text)
        _log(f"Dumped raw {stage} response to {filename}")
    except Exception as e:
        _log(f"Failed to dump raw {stage} response: {e}")


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


# FIX A1: Strict JSON contract suffix
JSON_CONTRACT = """

CRITICAL OUTPUT FORMAT:
Return ONLY a single JSON object.
No markdown. No prose. No code fences.
The JSON object must match the schema provided above exactly.
"""


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
    
    FIX B: Atomic claim + authoritative DB attempts value
    
    Returns task dict with authoritative DB values if claimed, else None.
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
            
            # Fetch authoritative DB values AFTER claim
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


def mark_task_failed(task_id: str, error_code: str, error_msg: str, attempts: int):
    """
    Mark task as FAILED or schedule retry.
    
    FIX B: Use authoritative attempts value from claim.
    FIX C: Store error code for debugging.
    
    Backoff schedule:
    - attempts == 1 → +300s
    - attempts == 2 → +900s
    - attempts == 3 → +3600s
    - attempts >= 4 → FAILED permanently
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            # Determine if permanently failed
            if attempts >= MAX_RETRIES:
                # Final failure
                full_error = f"{error_code}: {error_msg}"
                conn.execute("""
                    UPDATE async_tasks
                    SET status = 'FAILED',
                        last_error = ?,
                        updated_at = ?
                    WHERE task_id = ?
                """, (full_error, now_iso, task_id))
                _log(f"Task {task_id} FAILED permanently after {attempts} attempts ({error_code})")
                
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
                # Schedule retry with exact backoff
                delay_sec = RETRY_DELAYS.get(attempts, RETRY_DELAYS[3])
                next_run = datetime.now(timezone.utc) + timedelta(seconds=delay_sec)
                next_run_iso = next_run.isoformat()
                
                full_error = f"{error_code}: {error_msg}"
                conn.execute("""
                    UPDATE async_tasks
                    SET status = 'PENDING',
                        last_error = ?,
                        next_run_at = ?,
                        updated_at = ?
                    WHERE task_id = ?
                """, (full_error, next_run_iso, now_iso, task_id))
                _log(f"Task {task_id} retry scheduled in {delay_sec}s (attempt {attempts}/{MAX_RETRIES}, {error_code})")
            
            conn.commit()
    except Exception as e:
        _log(f"Error marking task {task_id} failed: {e}")


# ─────────────────────────────────────────────
# ANALYSIS FUNCTIONS (FIX A: Strict JSON contracts)
# ─────────────────────────────────────────────

def run_sanad_verification(position_data: dict, signal_payload: dict, task_id: str) -> dict:
    """
    Run Sanad verification (rugpull, sybil, trust).
    
    FIX A1: Strict JSON contract in prompt
    FIX A2: Robust extraction + validation
    """
    position_id = position_data.get('position_id', '?')
    token_symbol = position_data.get('token_address', '?')
    
    _log(f"Running Sanad verification for position {position_id}")
    
    # Build user message with explicit schema
    user_msg = f"""
Token: {position_data.get('token_address')}
Chain: {position_data.get('chain')}
Entry price: {position_data.get('entry_price')}
Size: ${position_data.get('size_usd')}
Strategy: {position_data.get('strategy_id')}

Analyze this token for Sanad verification.

Required JSON schema:
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
            SANAD_PROMPT + JSON_CONTRACT,
            user_msg,
            model=MODEL,
            max_tokens=2000,
            stage="cold_sanad",
            token_symbol=token_symbol
        )
        
        if not raw:
            raise RuntimeError("Sanad API call returned None")
        
        # FIX A2: Extract and validate
        parsed = llm_client.extract_json_object(raw)
        if not parsed:
            _dump_raw_llm(task_id, "sanad", raw)
            raise ValueError("Failed to extract JSON from Sanad response")
        
        # Validate required fields
        if "trust_score" not in parsed:
            _dump_raw_llm(task_id, "sanad", raw)
            raise ValueError("Sanad JSON missing trust_score")
        
        return {
            "raw": raw[:500] + "..." if len(raw) > 500 else raw,  # Truncate for storage
            "parsed": parsed,
            "model": MODEL,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        _log(f"Sanad verification error: {e}")
        raise


def run_bull_analysis(signal_payload: dict, token_symbol: str, task_id: str) -> dict:
    """
    Run Bull argument (pro-trade).
    
    FIX A1: Strict JSON contract in prompt
    FIX A2: Robust extraction + validation
    """
    _log("Running Bull analysis")
    
    user_msg = f"""
{json.dumps(signal_payload, indent=2)}

Argue FOR this trade as Al-Baqarah (Bull).

Required JSON schema:
{{
  "verdict": "<BUY|SKIP>",
  "confidence": <int 0-100>,
  "rationale": "<string>",
  "key_strengths": [<string list>]
}}
"""
    
    try:
        raw = llm_client.call_claude(
            BULL_PROMPT + JSON_CONTRACT,
            user_msg,
            model=MODEL,
            max_tokens=2000,
            stage="cold_bull",
            token_symbol=token_symbol
        )
        
        if not raw:
            raise RuntimeError("Bull API call returned None")
        
        # FIX A2: Extract and validate
        parsed = llm_client.extract_json_object(raw)
        if not parsed:
            _dump_raw_llm(task_id, "bull", raw)
            raise ValueError("Failed to extract JSON from Bull response")
        
        # Validate required fields
        if "verdict" not in parsed or "confidence" not in parsed:
            _dump_raw_llm(task_id, "bull", raw)
            raise ValueError("Bull JSON missing verdict or confidence")
        
        return {
            "raw": raw[:500] + "..." if len(raw) > 500 else raw,
            "parsed": parsed,
            "model": MODEL,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        _log(f"Bull analysis error: {e}")
        raise


def run_bear_analysis(signal_payload: dict, token_symbol: str, task_id: str) -> dict:
    """
    Run Bear argument (anti-trade).
    
    FIX A1: Strict JSON contract in prompt
    FIX A2: Robust extraction + validation
    """
    _log("Running Bear analysis")
    
    user_msg = f"""
{json.dumps(signal_payload, indent=2)}

Argue AGAINST this trade as Al-Dahhak (Bear).

Required JSON schema:
{{
  "verdict": "<SKIP|BUY>",
  "confidence": <int 0-100>,
  "rationale": "<string>",
  "key_risks": [<string list>]
}}
"""
    
    try:
        raw = llm_client.call_claude(
            BEAR_PROMPT + JSON_CONTRACT,
            user_msg,
            model=MODEL,
            max_tokens=2000,
            stage="cold_bear",
            token_symbol=token_symbol
        )
        
        if not raw:
            raise RuntimeError("Bear API call returned None")
        
        # FIX A2: Extract and validate
        parsed = llm_client.extract_json_object(raw)
        if not parsed:
            _dump_raw_llm(task_id, "bear", raw)
            raise ValueError("Failed to extract JSON from Bear response")
        
        # Validate required fields
        if "verdict" not in parsed or "confidence" not in parsed:
            _dump_raw_llm(task_id, "bear", raw)
            raise ValueError("Bear JSON missing verdict or confidence")
        
        return {
            "raw": raw[:500] + "..." if len(raw) > 500 else raw,
            "parsed": parsed,
            "model": MODEL,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        _log(f"Bear analysis error: {e}")
        raise


def run_judge_verdict(signal_payload: dict, sanad: dict, bull: dict, bear: dict, token_symbol: str, task_id: str) -> dict:
    """
    Run Judge verdict (GPT reviews full evidence).
    
    FIX A1: Strict JSON contract in prompt
    FIX A2: Robust extraction + validation (ESPECIALLY confidence field)
    FIX C: Dump raw response on parse failure
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

As Al-Muhasbi (Judge), provide your verdict.

Required JSON schema:
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
            JUDGE_PROMPT + JSON_CONTRACT,
            user_msg,
            model=JUDGE_MODEL,
            max_tokens=2000,
            stage="cold_judge",
            token_symbol=token_symbol
        )
        
        if not raw:
            raise RuntimeError("Judge API call returned None")
        
        # FIX A2: Extract and validate
        parsed = llm_client.extract_json_object(raw)
        if not parsed:
            _dump_raw_llm(task_id, "judge", raw)
            raise ValueError("ERR_JUDGE_PARSE: Failed to extract JSON from Judge response")
        
        # FIX A2: Validate REQUIRED fields (especially confidence)
        if "verdict" not in parsed:
            _dump_raw_llm(task_id, "judge", raw)
            raise ValueError("ERR_JUDGE_PARSE: Judge JSON missing verdict field")
        
        if parsed["verdict"] not in ["APPROVE", "REJECT"]:
            _dump_raw_llm(task_id, "judge", raw)
            raise ValueError(f"ERR_JUDGE_PARSE: Invalid verdict value: {parsed['verdict']}")
        
        if "confidence" not in parsed:
            _dump_raw_llm(task_id, "judge", raw)
            raise ValueError("ERR_JUDGE_PARSE: Judge JSON missing confidence field")
        
        if not isinstance(parsed["confidence"], (int, float)):
            _dump_raw_llm(task_id, "judge", raw)
            raise ValueError(f"ERR_JUDGE_PARSE: Invalid confidence type: {type(parsed['confidence'])}")
        
        confidence = int(parsed["confidence"])
        if not (0 <= confidence <= 100):
            _dump_raw_llm(task_id, "judge", raw)
            raise ValueError(f"ERR_JUDGE_PARSE: confidence out of range: {confidence}")
        
        # Normalize confidence to int
        parsed["confidence"] = confidence
        
        return {
            "raw": raw[:500] + "..." if len(raw) > 500 else raw,
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
    
    FIX B: Use authoritative DB attempts value
    FIX C: Error codes for debuggability
    FIX D: Catastrophic logic from Judge JSON only
    """
    task_id = task["task_id"]
    position_id = task["entity_id"]
    task_type = task["task_type"]
    attempts = task["attempts"]  # FIX B: Authoritative from DB
    
    _log(f"Processing task {task_id} (type={task_type}, position={position_id}, attempt={attempts})")
    
    # Wall-clock timestamp for started_at
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
        sanad_result = run_sanad_verification(position_data, signal_payload, task_id)
        
        # Step 2: Bull/Bear debate (parallel if configured)
        if PARALLEL_BULL_BEAR:
            with ThreadPoolExecutor(max_workers=2) as executor:
                bull_future = executor.submit(run_bull_analysis, signal_payload, token_symbol, task_id)
                bear_future = executor.submit(run_bear_analysis, signal_payload, token_symbol, task_id)
                
                bull_result = bull_future.result(timeout=TIMEOUT_SECONDS)
                bear_result = bear_future.result(timeout=TIMEOUT_SECONDS)
        else:
            bull_result = run_bull_analysis(signal_payload, token_symbol, task_id)
            bear_result = run_bear_analysis(signal_payload, token_symbol, task_id)
        
        # Step 3: Judge verdict
        judge_result = run_judge_verdict(signal_payload, sanad_result, bull_result, bear_result, token_symbol, task_id)
        
        # Wall-clock timestamp for completed_at
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
        
        # FIX D: Catastrophic flagging from Judge JSON ONLY
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
        
    except ValueError as e:
        # Parse errors or validation failures
        error_msg = str(e)
        if "ERR_JUDGE_PARSE" in error_msg:
            error_code = "ERR_JUDGE_PARSE"
        elif "extract JSON" in error_msg or "missing" in error_msg:
            error_code = "ERR_JSON_PARSE"
        else:
            error_code = "ERR_VALIDATION"
        
        _log(f"Task {task_id} failed: {error_code}: {error_msg}")
        mark_task_failed(task_id, error_code, error_msg, attempts)
        
    except Exception as e:
        # API failures, timeouts, etc.
        _log(f"Task {task_id} failed: {e}")
        import traceback
        _log(traceback.format_exc())
        
        error_code = "ERR_WORKER"
        mark_task_failed(task_id, error_code, str(e), attempts)


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
