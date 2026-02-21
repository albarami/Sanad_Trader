#!/usr/bin/env python3
"""
Test Async Retry Schedule — Ticket 4 Validation

Verifies:
1. Retry/backoff schedule: 300s / 900s / 3600s / FAILED
2. RUNNING state is real and observable (not theoretical)
3. attempts increments are authoritative from DB
4. State transitions are guarded by status='RUNNING'

Uses ASYNC_TEST_MODE=1 to add deliberate 2s sleep after claim,
allowing assertion of RUNNING state mid-flight.
"""

import os
import sys
import uuid
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add scripts to path
BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

from state_store import get_connection

EXPECTED_DELAYS = [300, 900, 3600]
MAX_ATTEMPTS = 4
TOLERANCE_SEC = 5


def get_task_state(task_id):
    """Fetch current task state from DB."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT status, attempts, next_run_at, last_error, updated_at
            FROM async_tasks
            WHERE task_id = ?
        """, (task_id,)).fetchone()
        return dict(row) if row else None


def force_task_ready(task_id):
    """Force task to be immediately claimable (reset next_run_at + status)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute("""
            UPDATE async_tasks
            SET next_run_at = ?,
                status = 'PENDING'
            WHERE task_id = ?
        """, (now_iso, task_id))
        conn.commit()


def run_worker(test_mode=False):
    """Run the worker subprocess. Returns (success, stdout)."""
    env = os.environ.copy()
    if test_mode:
        env["ASYNC_TEST_MODE"] = "1"
    
    result = subprocess.run(
        ["python3", str(BASE_DIR / "scripts" / "async_analysis_queue.py")],
        capture_output=True,
        text=True,
        timeout=120,
        env=env
    )
    return result.returncode == 0, result.stdout


def run_worker_background(test_mode=True):
    """Start worker in background (for RUNNING state observation)."""
    env = os.environ.copy()
    if test_mode:
        env["ASYNC_TEST_MODE"] = "1"
    
    proc = subprocess.Popen(
        ["python3", str(BASE_DIR / "scripts" / "async_analysis_queue.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )
    return proc


def assert_eq(label, expected, actual):
    if expected != actual:
        print(f"❌ FAIL: {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"✓ {label}: {actual!r}")


def assert_approx(label, expected, actual, tolerance=TOLERANCE_SEC):
    if abs(expected - actual) > tolerance:
        print(f"❌ FAIL: {label}: expected ~{expected}, got {actual:.0f} (tolerance={tolerance}s)")
        sys.exit(1)
    print(f"✓ {label}: {actual:.0f}s (~{expected}s)")


# ─────────────────────────────────────────────
# TEST 1: Retry schedule (attempts 1→2→3→4)
# ─────────────────────────────────────────────

def test_retry_schedule():
    """Validate retry schedule across 4 attempts with RUNNING proof."""
    print("=" * 60)
    print("TEST 1: Retry Schedule (4 attempts)")
    print("=" * 60)
    
    # Setup: task pointing to non-existent position
    task_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO async_tasks (
                task_id, task_type, entity_id, status, 
                attempts, created_at, next_run_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, 'ANALYZE_EXECUTED', 'FAKE_POS_DOES_NOT_EXIST',
            'PENDING', 0, now_iso, now_iso, now_iso
        ))
        conn.commit()
    
    print(f"✓ Created test task: {task_id}")
    print(f"  Points to: FAKE_POS_DOES_NOT_EXIST (forced failure)")
    
    for attempt_num in range(1, MAX_ATTEMPTS + 1):
        print(f"\n--- Attempt {attempt_num} ---")
        
        # Force task ready
        force_task_ready(task_id)
        
        state_before = get_task_state(task_id)
        assert_eq(f"Pre-run status", "PENDING", state_before["status"])
        assert_eq(f"Pre-run attempts", attempt_num - 1, state_before["attempts"])
        
        if attempt_num == 1:
            # First attempt: use background worker + TEST_MODE to observe RUNNING
            print("Running worker in background (ASYNC_TEST_MODE=1)...")
            proc = run_worker_background(test_mode=True)
            
            # Wait for claim + sleep(2) to start
            import time
            time.sleep(1)
            
            # Check RUNNING state mid-flight
            state_mid = get_task_state(task_id)
            assert_eq("Mid-flight status (RUNNING proof)", "RUNNING", state_mid["status"])
            assert_eq("Mid-flight attempts", 1, state_mid["attempts"])
            
            # Wait for worker to finish
            proc.wait(timeout=30)
        else:
            # Subsequent attempts: normal run
            print("Running worker...")
            run_worker(test_mode=False)
        
        # Check state after
        state_after = get_task_state(task_id)
        assert_eq(f"Post-run attempts", attempt_num, state_after["attempts"])
        
        if attempt_num < MAX_ATTEMPTS:
            assert_eq(f"Post-run status", "PENDING", state_after["status"])
            
            # Check backoff delay
            next_run_at = datetime.fromisoformat(state_after["next_run_at"].replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            delta_sec = (next_run_at - now).total_seconds()
            expected_delay = EXPECTED_DELAYS[attempt_num - 1]
            assert_approx(f"Backoff delay", expected_delay, delta_sec)
        else:
            assert_eq(f"Post-run status (final)", "FAILED", state_after["status"])
            assert_eq(f"Has error code", True, "ERR_" in state_after["last_error"])
            print(f"✓ Error: {state_after['last_error'][:60]}...")
    
    # Cleanup
    with get_connection() as conn:
        conn.execute("DELETE FROM async_tasks WHERE task_id = ?", (task_id,))
        conn.commit()
    print(f"\n✓ Cleaned up test task: {task_id}")
    
    print("\n✅ TEST 1 PASSED: Retry schedule verified")
    print("  Attempt 1 → RUNNING (observed) → PENDING +300s")
    print("  Attempt 2 → PENDING +900s")
    print("  Attempt 3 → PENDING +3600s")
    print("  Attempt 4 → FAILED permanently")


# ─────────────────────────────────────────────
# TEST 2: Worker ignores non-ANALYZE_EXECUTED tasks
# ─────────────────────────────────────────────

def test_ignore_wrong_task_type():
    """Prove worker ignores tasks with task_type != 'ANALYZE_EXECUTED'."""
    print("\n" + "=" * 60)
    print("TEST 2: Worker Ignores Non-ANALYZE_EXECUTED Tasks")
    print("=" * 60)
    
    task_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO async_tasks (
                task_id, task_type, entity_id, status,
                attempts, created_at, next_run_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, 'SOMETHING_ELSE', 'SOME_ENTITY',
            'PENDING', 0, now_iso, now_iso, now_iso
        ))
        conn.commit()
    
    print(f"✓ Created task {task_id} with task_type='SOMETHING_ELSE'")
    print("Running worker...")
    
    run_worker(test_mode=False)
    
    state = get_task_state(task_id)
    assert_eq("Task still PENDING", "PENDING", state["status"])
    assert_eq("Attempts unchanged", 0, state["attempts"])
    
    # Cleanup
    with get_connection() as conn:
        conn.execute("DELETE FROM async_tasks WHERE task_id = ?", (task_id,))
        conn.commit()
    
    print(f"✓ Cleaned up test task")
    print("\n✅ TEST 2 PASSED: Worker correctly ignores non-ANALYZE_EXECUTED tasks")


# ─────────────────────────────────────────────
# TEST 3: Real task reaches DONE
# ─────────────────────────────────────────────

def test_real_task_done():
    """Create a real position + task, run worker, verify DONE."""
    print("\n" + "=" * 60)
    print("TEST 2: Real Task Reaches DONE")
    print("=" * 60)
    
    position_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with get_connection() as conn:
        conn.execute('''
            INSERT INTO positions (
                position_id, signal_id, token_address, entry_price, 
                size_usd, chain, strategy_id, decision_id, status, 
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            position_id, 'TEST_DONE_' + now_iso, 'ETH', 3200.00,
            500.0, 'eth', 'momentum_flip', 'dec_' + position_id[:8],
            'OPEN', now_iso, now_iso
        ))
        
        conn.execute('''
            INSERT INTO async_tasks (
                task_id, task_type, entity_id, status, 
                attempts, created_at, next_run_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task_id, 'ANALYZE_EXECUTED', position_id,
            'PENDING', 0, now_iso, now_iso, now_iso
        ))
        conn.commit()
    
    print(f"✓ Created position {position_id} (ETH @ $3,200)")
    print(f"✓ Created task {task_id}")
    print("Running worker (real LLM calls)...")
    
    success, stdout = run_worker(test_mode=False)
    
    # Check task
    with get_connection() as conn:
        task = conn.execute("SELECT * FROM async_tasks WHERE task_id = ?", (task_id,)).fetchone()
        task = dict(task)
        
        pos = conn.execute("SELECT * FROM positions WHERE position_id = ?", (position_id,)).fetchone()
        pos = dict(pos)
    
    assert_eq("Task status", "DONE", task["status"])
    assert_eq("Task attempts", 1, task["attempts"])
    assert_eq("Position async_analysis_complete", 1, pos["async_analysis_complete"])
    assert_eq("Position has async_analysis_json", True, pos["async_analysis_json"] is not None and len(pos["async_analysis_json"]) > 100)
    
    # Validate JSON structure
    import json
    analysis = json.loads(pos["async_analysis_json"])
    assert_eq("Has sanad", True, "sanad" in analysis)
    assert_eq("Has bull", True, "bull" in analysis)
    assert_eq("Has bear", True, "bear" in analysis)
    assert_eq("Has judge", True, "judge" in analysis)
    assert_eq("Has meta", True, "meta" in analysis)
    
    judge = analysis["judge"]["parsed"]
    assert_eq("Judge has verdict", True, judge.get("verdict") in ["APPROVE", "REJECT"])
    assert_eq("Judge has confidence (int)", True, isinstance(judge.get("confidence"), int))
    
    print(f"\n  Judge verdict: {judge['verdict']}, confidence: {judge['confidence']}%")
    print(f"  Model: {analysis['meta']['model']}")
    print(f"  Judge model: {analysis['meta']['judge_model']}")
    
    # Cleanup
    with get_connection() as conn:
        conn.execute("DELETE FROM async_tasks WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM positions WHERE position_id = ?", (position_id,))
        conn.commit()
    
    print(f"\n✓ Cleaned up test data")
    print("\n✅ TEST 3 PASSED: Real task reached DONE with valid analysis JSON")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        test_retry_schedule()
        test_ignore_wrong_task_type()
        test_real_task_done()
        
        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
    except KeyboardInterrupt:
        print("\n⚠ Test interrupted by user")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
