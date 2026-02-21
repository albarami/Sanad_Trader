#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Cold Path: Async Analysis Queue Worker

Polls async_tasks table for PENDING tasks and processes them:
1. Sanad verification (rugpull, sybil, trust scoring)
2. Bull/Bear debate (parallel LLM calls)
3. Judge verdict (GPT Codex)

Updates positions.async_analysis_json with results.

Author: Sanad Trader v3.1
Ticket 4 v4: Race-safe state transitions, authoritative attempts, RUNNING guards
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

# Test mode: set ASYNC_TEST_MODE=1 to add deliberate sleep after claim
TEST_MODE = os.environ.get("ASYNC_TEST_MODE") == "1"

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

# Cold path settings — single source of truth
MODEL = COLD_PATH_CONFIG.get("model", "claude-haiku-4-5-20251001")
JUDGE_MODEL = COLD_PATH_CONFIG.get("judge_model", "gpt-5.2")
TIMEOUT_SECONDS = COLD_PATH_CONFIG.get("timeout_seconds", 300)
MAX_ATTEMPTS = COLD_PATH_CONFIG.get("max_attempts", 4)  # Total: 1st try + 3 retries
PARALLEL_BULL_BEAR = COLD_PATH_CONFIG.get("parallel_bull_bear", True)
CATASTROPHIC_THRESHOLD = COLD_PATH_CONFIG.get("catastrophic_confidence_threshold", 85)

# Backoff schedule (indexed by attempts_now - 1):
#   attempts_now == 1 → RETRY_DELAYS[0] = 300s
#   attempts_now == 2 → RETRY_DELAYS[1] = 900s
#   attempts_now == 3 → RETRY_DELAYS[2] = 3600s
#   attempts_now >= 4 → FAILED
RETRY_DELAYS = [300, 900, 3600]


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
    """Dump raw LLM response to file for debugging."""
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


# Strict JSON contract suffix appended to all prompts
JSON_CONTRACT = """

CRITICAL OUTPUT FORMAT:
Return ONLY a single JSON object.
No markdown. No prose. No code fences.
The JSON object must match the schema provided above exactly.
"""


# ─────────────────────────────────────────────
# TASK STATE MACHINE
#
# State transitions (all guarded by current status):
#   PENDING  → RUNNING   (claim_task: atomic UPDATE WHERE status='PENDING')
#   RUNNING  → DONE      (mark_task_done: UPDATE WHERE status='RUNNING')
#   RUNNING  → PENDING   (mark_task_failed retry: UPDATE WHERE status='RUNNING')
#   RUNNING  → FAILED    (mark_task_failed final: UPDATE WHERE status='RUNNING')
#
# Attempts lifecycle:
#   - Incremented ONLY in claim_task (attempts := attempts + 1)
#   - NEVER incremented in mark_task_failed or process_task
#   - All downstream code uses attempts_now from claim SELECT
# ─────────────────────────────────────────────

def poll_pending_tasks():
    """
    Poll async_tasks for PENDING tasks ready to run.
    Returns list of task_id strings (NOT full task dicts — those come from claim).
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            rows = conn.execute("""
                SELECT task_id
                FROM async_tasks
                WHERE status = 'PENDING'
                  AND task_type = 'ANALYZE_EXECUTED'
                  AND next_run_at <= ?
                ORDER BY next_run_at ASC
                LIMIT 10
            """, (now_iso,)).fetchall()
            
            return [row["task_id"] for row in rows]
    except DBBusyError:
        _log("DB busy during poll, will retry next cycle")
        return []
    except Exception as e:
        _log(f"Error polling tasks: {e}")
        return []


def claim_task(task_id: str) -> dict:
    """
    Atomically claim task: PENDING → RUNNING, attempts += 1.
    
    Returns authoritative task dict (with post-increment attempts) or None.
    The returned dict is the ONLY source of truth for attempts_now.
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            # Atomic claim: only succeeds if PENDING and ready
            cursor = conn.execute("""
                UPDATE async_tasks
                SET status = 'RUNNING',
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE task_id = ? 
                  AND status = 'PENDING'
                  AND task_type = 'ANALYZE_EXECUTED'
                  AND next_run_at <= ?
            """, (now_iso, task_id, now_iso))
            
            conn.commit()
            
            if cursor.rowcount == 0:
                return None
            
            # Immediately read authoritative row in same connection
            row = conn.execute("""
                SELECT task_id, entity_id, task_type, attempts, created_at
                FROM async_tasks
                WHERE task_id = ?
            """, (task_id,)).fetchone()
            
            if not row:
                return None
            
            task = dict(row)
            _log(f"Claimed task {task_id} (attempt {task['attempts']} of {MAX_ATTEMPTS})")
            
            # Test mode: sleep 2s after claim to prove RUNNING state is observable
            if TEST_MODE:
                _log(f"[TEST_MODE] Sleeping 2s after claim — task is RUNNING in DB")
                time.sleep(2)
            
            return task
            
    except Exception as e:
        _log(f"Error claiming task {task_id}: {e}")
        return None


def mark_task_done(task_id: str):
    """
    Mark task as DONE. Guarded by status='RUNNING'.
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            
            cursor = conn.execute("""
                UPDATE async_tasks
                SET status = 'DONE',
                    updated_at = ?,
                    last_error = NULL
                WHERE task_id = ?
                  AND status = 'RUNNING'
            """, (now_iso, task_id))
            
            conn.commit()
            
            if cursor.rowcount == 0:
                _log(f"WARNING: mark_task_done({task_id}) — task was not RUNNING (race?)")
            else:
                _log(f"Task {task_id} marked DONE")
    except Exception as e:
        _log(f"Error marking task {task_id} done: {e}")


def mark_task_failed(task_id: str, error_code: str, error_msg: str, attempts_now: int):
    """
    Mark task as retry-PENDING or FAILED. Guarded by status='RUNNING'.
    
    attempts_now is the authoritative post-claim value from DB.
    It is NOT incremented here — only claim_task increments attempts.
    
    Schedule:
      attempts_now == 1 → retry in 300s
      attempts_now == 2 → retry in 900s
      attempts_now == 3 → retry in 3600s
      attempts_now >= 4 → FAILED permanently
    """
    try:
        with get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            full_error = f"{error_code}: {error_msg}"
            
            if attempts_now >= MAX_ATTEMPTS:
                # Permanent failure — RUNNING → FAILED
                cursor = conn.execute("""
                    UPDATE async_tasks
                    SET status = 'FAILED',
                        last_error = ?,
                        updated_at = ?
                    WHERE task_id = ?
                      AND status = 'RUNNING'
                """, (full_error, now_iso, task_id))
                
                if cursor.rowcount == 0:
                    _log(f"WARNING: mark_task_failed({task_id}) — task was not RUNNING (race?), skipping position update")
                else:
                    _log(f"Task {task_id} FAILED permanently after {attempts_now} attempts ({error_code})")
                    
                    # Only flag position if task update succeeded (rowcount==1)
                    row = conn.execute("SELECT entity_id FROM async_tasks WHERE task_id = ?", (task_id,)).fetchone()
                    if row:
                        pos_exists = conn.execute("SELECT 1 FROM positions WHERE position_id = ?", (row["entity_id"],)).fetchone()
                        if pos_exists:
                            conn.execute("""
                                UPDATE positions
                                SET risk_flag = 'FLAG_ASYNC_FAILED_PERMANENT'
                                WHERE position_id = ?
                            """, (row["entity_id"],))
                            _log(f"Position {row['entity_id']} marked FLAG_ASYNC_FAILED_PERMANENT")
            else:
                # Retry — RUNNING → PENDING with backoff
                delay_sec = RETRY_DELAYS[attempts_now - 1]
                next_run = datetime.now(timezone.utc) + timedelta(seconds=delay_sec)
                next_run_iso = next_run.isoformat()
                
                cursor = conn.execute("""
                    UPDATE async_tasks
                    SET status = 'PENDING',
                        last_error = ?,
                        next_run_at = ?,
                        updated_at = ?
                    WHERE task_id = ?
                      AND status = 'RUNNING'
                """, (full_error, next_run_iso, now_iso, task_id))
                
                if cursor.rowcount == 0:
                    _log(f"WARNING: mark_task_failed({task_id}) — task was not RUNNING (race?)")
                else:
                    _log(f"Task {task_id} retry scheduled in {delay_sec}s (attempt {attempts_now}/{MAX_ATTEMPTS}, {error_code})")
            
            conn.commit()
    except Exception as e:
        _log(f"Error marking task {task_id} failed: {e}")


# ─────────────────────────────────────────────
# ANALYSIS FUNCTIONS (Strict JSON contracts)
# ─────────────────────────────────────────────

def run_sanad_verification(position_data: dict, signal_payload: dict, task_id: str) -> dict:
    """Run Sanad verification (rugpull, sybil, trust)."""
    position_id = position_data.get('position_id', '?')
    token_symbol = position_data.get('token_address', '?')
    
    _log(f"Running Sanad verification for position {position_id}")
    
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
    
    raw = llm_client.call_claude(
        SANAD_PROMPT + JSON_CONTRACT, user_msg,
        model=MODEL, max_tokens=2000, stage="cold_sanad", token_symbol=token_symbol
    )
    
    if not raw:
        raise RuntimeError("Sanad API call returned None")
    
    parsed = llm_client.extract_json_object(raw)
    if not parsed:
        _dump_raw_llm(task_id, "sanad", raw)
        raise ValueError("Failed to extract JSON from Sanad response")
    
    if "trust_score" not in parsed:
        _dump_raw_llm(task_id, "sanad", raw)
        raise ValueError("Sanad JSON missing trust_score")
    
    return {
        "raw": raw[:500] + ("..." if len(raw) > 500 else ""),
        "parsed": parsed,
        "model": MODEL,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def run_bull_analysis(signal_payload: dict, token_symbol: str, task_id: str) -> dict:
    """Run Bull argument (pro-trade)."""
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
    
    raw = llm_client.call_claude(
        BULL_PROMPT + JSON_CONTRACT, user_msg,
        model=MODEL, max_tokens=2000, stage="cold_bull", token_symbol=token_symbol
    )
    
    if not raw:
        raise RuntimeError("Bull API call returned None")
    
    parsed = llm_client.extract_json_object(raw)
    if not parsed:
        _dump_raw_llm(task_id, "bull", raw)
        raise ValueError("Failed to extract JSON from Bull response")
    
    if "verdict" not in parsed or "confidence" not in parsed:
        _dump_raw_llm(task_id, "bull", raw)
        raise ValueError("Bull JSON missing verdict or confidence")
    
    return {
        "raw": raw[:500] + ("..." if len(raw) > 500 else ""),
        "parsed": parsed,
        "model": MODEL,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def run_bear_analysis(signal_payload: dict, token_symbol: str, task_id: str) -> dict:
    """Run Bear argument (anti-trade)."""
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
    
    raw = llm_client.call_claude(
        BEAR_PROMPT + JSON_CONTRACT, user_msg,
        model=MODEL, max_tokens=2000, stage="cold_bear", token_symbol=token_symbol
    )
    
    if not raw:
        raise RuntimeError("Bear API call returned None")
    
    parsed = llm_client.extract_json_object(raw)
    if not parsed:
        _dump_raw_llm(task_id, "bear", raw)
        raise ValueError("Failed to extract JSON from Bear response")
    
    if "verdict" not in parsed or "confidence" not in parsed:
        _dump_raw_llm(task_id, "bear", raw)
        raise ValueError("Bear JSON missing verdict or confidence")
    
    return {
        "raw": raw[:500] + ("..." if len(raw) > 500 else ""),
        "parsed": parsed,
        "model": MODEL,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def run_judge_verdict(signal_payload: dict, sanad: dict, bull: dict, bear: dict, token_symbol: str, task_id: str) -> dict:
    """Run Judge verdict (GPT reviews full evidence)."""
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
    
    raw = llm_client.call_openai(
        JUDGE_PROMPT + JSON_CONTRACT, user_msg,
        model=JUDGE_MODEL, max_tokens=2000, stage="cold_judge", token_symbol=token_symbol
    )
    
    if not raw:
        raise RuntimeError("Judge API call returned None")
    
    parsed = llm_client.extract_json_object(raw)
    if not parsed:
        _dump_raw_llm(task_id, "judge", raw)
        raise ValueError("ERR_JUDGE_PARSE: Failed to extract JSON from Judge response")
    
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
    
    parsed["confidence"] = confidence
    
    return {
        "raw": raw[:500] + ("..." if len(raw) > 500 else ""),
        "parsed": parsed,
        "model": JUDGE_MODEL,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def process_task(task_id: str, entity_id: str, task_type: str, attempts_now: int):
    """
    Process a single async task.
    
    All parameters come from claim_task() (authoritative DB values).
    attempts_now is the post-increment value — NEVER modified here.
    """
    _log(f"Processing task {task_id} (type={task_type}, position={entity_id}, attempt={attempts_now})")
    
    started_at = datetime.now(timezone.utc).isoformat()
    perf_start = time.perf_counter()
    
    try:
        # Load position data
        with get_connection() as conn:
            row = conn.execute("""
                SELECT position_id, token_address, entry_price, 
                       size_usd, decision_id, signal_id, chain, strategy_id
                FROM positions
                WHERE position_id = ?
            """, (entity_id,)).fetchone()
            
            if not row:
                raise ValueError(f"Position {entity_id} not found")
            
            position_data = dict(row)
        
        token_symbol = position_data.get("token_address", "UNKNOWN")
        
        signal_payload = {
            "token_address": position_data["token_address"],
            "chain": position_data["chain"],
            "entry_price": position_data["entry_price"],
            "size_usd": position_data["size_usd"],
            "strategy_id": position_data["strategy_id"]
        }
        
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
        
        completed_at = datetime.now(timezone.utc).isoformat()
        duration_sec = time.perf_counter() - perf_start
        
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
        
        # Catastrophic flagging from Judge JSON ONLY
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
            """, (json.dumps(analysis_result), risk_flag, now_iso, entity_id))
            conn.commit()
        
        _log(f"Task {task_id} completed in {duration_sec:.1f}s (verdict={verdict}, confidence={confidence}%)")
        mark_task_done(task_id)
        
    except ValueError as e:
        error_msg = str(e)
        if "ERR_JUDGE_PARSE" in error_msg:
            error_code = "ERR_JUDGE_PARSE"
        elif "extract JSON" in error_msg or "missing" in error_msg:
            error_code = "ERR_JSON_PARSE"
        else:
            error_code = "ERR_VALIDATION"
        
        _log(f"Task {task_id} failed: {error_code}: {error_msg}")
        mark_task_failed(task_id, error_code, error_msg, attempts_now)
        
    except Exception as e:
        _log(f"Task {task_id} failed: {e}")
        import traceback
        _log(traceback.format_exc())
        mark_task_failed(task_id, "ERR_WORKER", str(e), attempts_now)


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def main():
    """Main worker loop: poll, claim, process."""
    _log("=" * 60)
    _log(f"Async Analysis Queue Worker START (model={MODEL}, judge={JUDGE_MODEL})")
    
    # Poll returns only task_ids (not full rows)
    task_ids = poll_pending_tasks()
    
    if not task_ids:
        _log("No pending tasks")
        return
    
    _log(f"Found {len(task_ids)} pending task(s)")
    
    for task_id in task_ids:
        # Claim returns authoritative row from DB (or None)
        claimed = claim_task(task_id)
        if not claimed:
            _log(f"Task {task_id} not claimed (already taken or not ready)")
            continue
        
        # Pass individual fields from authoritative claim — NOT polled row
        process_task(
            task_id=claimed["task_id"],
            entity_id=claimed["entity_id"],
            task_type=claimed["task_type"],
            attempts_now=claimed["attempts"]
        )
    
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
