#!/usr/bin/env python3
"""
Test Async Retry Schedule — Ticket 4 Validation

Verifies the retry/backoff schedule is correct:
- Attempt 1 fails → retry in 300s (5 minutes)
- Attempt 2 fails → retry in 900s (15 minutes)
- Attempt 3 fails → retry in 3600s (60 minutes)
- Attempt 4 fails → FAILED permanently

This test:
1. Inserts a task pointing to a non-existent position (forced failure)
2. Runs the worker 4 times (forcing next_run_at back to now each time)
3. Asserts after each run:
   - status transitions (RUNNING → PENDING for retries 1-3, then FAILED)
   - attempts increments exactly (1, 2, 3, 4)
   - next_run_at deltas match 300s, 900s, 3600s
   - after attempt 4: status = FAILED
"""

import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add scripts to path
BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

from state_store import get_connection

# Expected backoff delays
EXPECTED_DELAYS = [300, 900, 3600]
MAX_ATTEMPTS = 4


def setup_test():
    """Create a task pointing to non-existent position (will always fail)."""
    task_id = str(uuid.uuid4())
    fake_position_id = "FAKE_POSITION_DOES_NOT_EXIST"
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO async_tasks (
                task_id, task_type, entity_id, status, 
                attempts, created_at, next_run_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id,
            'ANALYZE_EXECUTED',
            fake_position_id,
            'PENDING',
            0,
            now_iso,
            now_iso,  # Ready immediately
            now_iso
        ))
        conn.commit()
    
    print(f"✓ Created test task: {task_id}")
    print(f"  Points to non-existent position: {fake_position_id}")
    return task_id


def force_task_ready(task_id):
    """Force task next_run_at to now so worker will pick it up."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute("""
            UPDATE async_tasks
            SET next_run_at = ?,
                status = 'PENDING'
            WHERE task_id = ?
        """, (now_iso, task_id))
        conn.commit()


def get_task_state(task_id):
    """Fetch current task state."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT status, attempts, next_run_at, last_error
            FROM async_tasks
            WHERE task_id = ?
        """, (task_id,)).fetchone()
        return dict(row) if row else None


def run_worker():
    """Run the async_analysis_queue worker once."""
    import subprocess
    result = subprocess.run(
        ["python3", str(BASE_DIR / "scripts" / "async_analysis_queue.py")],
        capture_output=True,
        text=True,
        timeout=120
    )
    return result.returncode == 0


def test_retry_schedule():
    """Main test: validate retry schedule across 4 attempts."""
    print("=" * 60)
    print("Async Retry Schedule Test — Ticket 4 Validation")
    print("=" * 60)
    
    # Setup
    task_id = setup_test()
    
    # Track previous next_run_at for delta calculation
    prev_next_run_at = None
    
    for attempt_num in range(1, MAX_ATTEMPTS + 1):
        print(f"\n--- Attempt {attempt_num} ---")
        
        # Force task ready
        force_task_ready(task_id)
        
        # Get state before
        state_before = get_task_state(task_id)
        print(f"Before: status={state_before['status']}, attempts={state_before['attempts']}")
        
        # Run worker
        print("Running worker...")
        success = run_worker()
        if not success:
            print("⚠ Worker execution failed (non-zero exit), but continuing test...")
        
        # Get state after
        state_after = get_task_state(task_id)
        print(f"After:  status={state_after['status']}, attempts={state_after['attempts']}")
        
        # Assertions
        expected_attempts = attempt_num
        actual_attempts = state_after['attempts']
        
        if actual_attempts != expected_attempts:
            print(f"❌ FAIL: Expected attempts={expected_attempts}, got {actual_attempts}")
            sys.exit(1)
        else:
            print(f"✓ Attempts incremented correctly: {actual_attempts}")
        
        # Check status transition
        if attempt_num < MAX_ATTEMPTS:
            # Should be PENDING (retry scheduled)
            if state_after['status'] != 'PENDING':
                print(f"❌ FAIL: Expected status=PENDING after attempt {attempt_num}, got {state_after['status']}")
                sys.exit(1)
            else:
                print(f"✓ Status=PENDING (retry scheduled)")
            
            # Check backoff delay
            next_run_at = datetime.fromisoformat(state_after['next_run_at'].replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            delta_sec = (next_run_at - now).total_seconds()
            expected_delay = EXPECTED_DELAYS[attempt_num - 1]
            
            # Allow 5s tolerance for test execution time
            if abs(delta_sec - expected_delay) > 5:
                print(f"❌ FAIL: Expected delay ~{expected_delay}s, got {delta_sec:.0f}s")
                sys.exit(1)
            else:
                print(f"✓ Backoff delay correct: {delta_sec:.0f}s (~{expected_delay}s)")
        else:
            # Final attempt: should be FAILED
            if state_after['status'] != 'FAILED':
                print(f"❌ FAIL: Expected status=FAILED after attempt {attempt_num}, got {state_after['status']}")
                sys.exit(1)
            else:
                print(f"✓ Status=FAILED (permanent failure)")
            
            # Check error code
            if 'ERR_' not in state_after['last_error']:
                print(f"❌ FAIL: Expected error code in last_error, got: {state_after['last_error']}")
                sys.exit(1)
            else:
                print(f"✓ Error code present: {state_after['last_error'][:50]}...")
    
    print("\n" + "=" * 60)
    print("✅ ALL ASSERTIONS PASSED")
    print("=" * 60)
    print("\nRetry schedule verified:")
    print("  Attempt 1 fails → retry in 300s (5 minutes)")
    print("  Attempt 2 fails → retry in 900s (15 minutes)")
    print("  Attempt 3 fails → retry in 3600s (60 minutes)")
    print("  Attempt 4 fails → FAILED permanently")
    
    # Cleanup
    with get_connection() as conn:
        conn.execute("DELETE FROM async_tasks WHERE task_id = ?", (task_id,))
        conn.commit()
    print(f"\n✓ Cleaned up test task: {task_id}")


if __name__ == "__main__":
    try:
        test_retry_schedule()
    except KeyboardInterrupt:
        print("\n⚠ Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
