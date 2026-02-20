#!/usr/bin/env python3
"""
Reset Daemon — Enterprise Stabilization Component
Processes OpenClaw cron reset requests from isolated watchdog sessions.
Runs in sessionTarget=main for reliable openclaw CLI access.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
RESET_QUEUE = STATE_DIR / "reset_requests.jsonl"
RESET_LOG = STATE_DIR / "reset_daemon.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str, level: str = "INFO"):
    """Append log to reset_daemon.log."""
    ts = datetime.now(timezone.utc).isoformat()
    entry = f"[{ts}] [{level}] {msg}\n"
    print(entry.strip())
    
    try:
        with open(RESET_LOG, "a") as f:
            f.write(entry)
    except Exception as e:
        print(f"Failed to write log: {e}")


# ---------------------------------------------------------------------------
# Reset Execution
# ---------------------------------------------------------------------------
def execute_reset(request: dict) -> dict:
    """
    Execute a single reset request (disable → enable).
    
    Args:
        request: {
            "job_id": "...",
            "job_name": "...",
            "requested_at": "...",
            "reason": "..."
        }
    
    Returns:
        {
            "job_id": "...",
            "success": bool,
            "executed_at": "...",
            "error": "..." (if failed)
        }
    """
    job_id = request["job_id"]
    job_name = request["job_name"]
    
    _log(f"Executing reset for {job_name} ({job_id})")
    
    result = {
        "job_id": job_id,
        "job_name": job_name,
        "success": False,
        "executed_at": datetime.now(timezone.utc).isoformat()
    }
    
    try:
        # Step 1: Disable
        _log(f"  Disabling {job_name}...")
        disable_result = subprocess.run(
            ["openclaw", "cron", "disable", job_id],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if disable_result.returncode != 0:
            result["error"] = f"Disable failed: {disable_result.stderr}"
            _log(f"  Disable FAILED: {disable_result.stderr}", "ERROR")
            return result
        
        _log(f"  Disabled successfully")
        
        # Brief pause
        time.sleep(2)
        
        # Step 2: Enable
        _log(f"  Enabling {job_name}...")
        enable_result = subprocess.run(
            ["openclaw", "cron", "enable", job_id],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if enable_result.returncode != 0:
            result["error"] = f"Enable failed: {enable_result.stderr}"
            _log(f"  Enable FAILED: {enable_result.stderr}", "ERROR")
            return result
        
        _log(f"  Enabled successfully")
        result["success"] = True
        
    except subprocess.TimeoutExpired:
        result["error"] = "OpenClaw CLI timeout (30s)"
        _log(f"  Timeout calling openclaw CLI", "ERROR")
    except Exception as e:
        result["error"] = str(e)
        _log(f"  Unexpected error: {e}", "ERROR")
    
    return result


# ---------------------------------------------------------------------------
# Queue Processing
# ---------------------------------------------------------------------------
def process_queue():
    """
    Read reset_requests.jsonl and process all pending requests.
    Writes results to reset_results.jsonl.
    """
    if not RESET_QUEUE.exists():
        _log("No reset queue file found", "DEBUG")
        return
    
    # Read all pending requests
    requests = []
    try:
        with open(RESET_QUEUE, "r") as f:
            for line in f:
                if line.strip():
                    requests.append(json.loads(line))
    except Exception as e:
        _log(f"Failed to read queue: {e}", "ERROR")
        return
    
    if not requests:
        _log("Queue empty", "DEBUG")
        return
    
    _log(f"Processing {len(requests)} reset request(s)")
    
    # Process each request
    results = []
    for req in requests:
        result = execute_reset(req)
        results.append(result)
    
    # Write results
    results_file = STATE_DIR / "reset_results.jsonl"
    try:
        with open(results_file, "a") as f:
            for result in results:
                f.write(json.dumps(result) + "\n")
        _log(f"Wrote {len(results)} result(s) to reset_results.jsonl")
    except Exception as e:
        _log(f"Failed to write results: {e}", "ERROR")
    
    # Clear queue (archive to reset_requests_archive.jsonl)
    try:
        archive = STATE_DIR / "reset_requests_archive.jsonl"
        with open(archive, "a") as f:
            for req in requests:
                f.write(json.dumps(req) + "\n")
        
        # Truncate queue
        RESET_QUEUE.write_text("")
        _log(f"Archived {len(requests)} request(s), queue cleared")
    except Exception as e:
        _log(f"Failed to archive queue: {e}", "ERROR")
    
    # Summary
    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count
    
    _log(f"Reset summary: {success_count} success, {fail_count} failed")
    
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    """Run reset daemon (single pass)."""
    _log("=== Reset Daemon START ===")
    
    try:
        results = process_queue()
        
        if results:
            _log(f"Processed {len(results)} request(s)")
        else:
            _log("No requests processed")
    
    except Exception as e:
        _log(f"FATAL: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        return 1
    
    _log("=== Reset Daemon END ===")
    return 0


if __name__ == "__main__":
    exit(main())
