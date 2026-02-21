#!/usr/bin/env python3
"""
Comprehensive test for async_analysis_queue.py

Tests:
1. Normal case: ANALYZE_EXECUTED task → DONE
2. Catastrophic case: Judge REJECT high confidence → risk_flag set
3. Retry case: Force error → PENDING with backoff
"""

import sys
import hashlib
import json
import subprocess
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '.')

from scripts.state_store import get_connection, init_db
from scripts.ids import make_signal_id, make_decision_id, make_position_id

# Initialize DB
init_db()

def create_test_position(signal_id_suffix=""):
    """Create a test position and task."""
    test_signal = {
        "token_address": f"TEST{signal_id_suffix}",
        "source": "test",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    signal_id = make_signal_id(test_signal) + signal_id_suffix
    decision_id = make_decision_id(signal_id, "v3.1.0")
    position_id = make_position_id(decision_id)
    task_id = hashlib.sha256(f"task_{position_id}_{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO positions (
                position_id, decision_id, signal_id, token_address, chain, strategy_id,
                entry_price, size_usd, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            position_id, decision_id, signal_id, test_signal["token_address"], "solana", "test_strategy",
            1.23, 100.0, "OPEN", now_iso, now_iso
        ))
        
        conn.execute("""
            INSERT INTO async_tasks (
                task_id, entity_id, task_type, status, 
                attempts, next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, position_id, "ANALYZE_EXECUTED", "PENDING",
            0, now_iso, now_iso, now_iso
        ))
        
        conn.commit()
    
    return task_id, position_id

def run_worker():
    """Run async_analysis_queue.py worker."""
    result = subprocess.run(
        ["python3", "scripts/async_analysis_queue.py"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd="/data/.openclaw/workspace/trading"
    )
    return result.stdout, result.stderr

def check_task_status(task_id):
    """Get task status."""
    with get_connection() as conn:
        row = conn.execute("SELECT status, attempts, next_run_at FROM async_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

def check_position_status(position_id):
    """Get position status."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT async_analysis_complete, async_analysis_json, risk_flag 
            FROM positions WHERE position_id = ?
        """, (position_id,)).fetchone()
        return dict(row) if row else None

# ============================================================================
# TEST 1: NORMAL CASE
# ============================================================================

print("=" * 70)
print("TEST 1: Normal Case (ANALYZE_EXECUTED → DONE)")
print("=" * 70)

# Clean up
with get_connection() as conn:
    conn.execute("DELETE FROM async_tasks WHERE task_type = 'ANALYZE_EXECUTED'")
    conn.execute("DELETE FROM positions WHERE token_address LIKE 'TEST%'")
    conn.commit()

task1_id, pos1_id = create_test_position("")

print(f"\n✅ Created task: {task1_id[:16]}...")
print(f"✅ Created position: {pos1_id[:16]}...")

task1_before = check_task_status(task1_id)
pos1_before = check_position_status(pos1_id)
print(f"\nBefore:")
print(f"  Task status: {task1_before['status']}")
print(f"  Position analysis_complete: {pos1_before['async_analysis_complete']}")

print(f"\n▶ Running worker...")
stdout, stderr = run_worker()
print(stdout)

task1_after = check_task_status(task1_id)
pos1_after = check_position_status(pos1_id)
print(f"\nAfter:")
print(f"  Task status: {task1_after['status']}")
print(f"  Position analysis_complete: {pos1_after['async_analysis_complete']}")
print(f"  Position risk_flag: {pos1_after['risk_flag']}")

if pos1_after['async_analysis_json']:
    analysis = json.loads(pos1_after['async_analysis_json'])
    print(f"  Analysis keys: {list(analysis.keys())}")
    print(f"  Judge verdict: {analysis['judge']['judge_verdict']['verdict']}")
    print(f"  Judge confidence: {analysis['judge']['judge_verdict']['confidence']}")

test1_pass = (
    task1_after['status'] == 'DONE' and
    pos1_after['async_analysis_complete'] == 1 and
    pos1_after['async_analysis_json'] is not None
)

print(f"\n{'✅ TEST 1 PASSED' if test1_pass else '❌ TEST 1 FAILED'}")

# ============================================================================
# TEST 2: CATASTROPHIC CASE
# ============================================================================

print("\n" + "=" * 70)
print("TEST 2: Catastrophic Case (Judge REJECT high confidence)")
print("=" * 70)

task2_id, pos2_id = create_test_position("_CATASTROPHIC")

print(f"\n✅ Created catastrophic task: {task2_id[:16]}...")
print(f"✅ Created position: {pos2_id[:16]}...")

task2_before = check_task_status(task2_id)
pos2_before = check_position_status(pos2_id)
print(f"\nBefore:")
print(f"  Task status: {task2_before['status']}")
print(f"  Position risk_flag: {pos2_before['risk_flag']}")

print(f"\n▶ Running worker...")
stdout, stderr = run_worker()
print(stdout)

task2_after = check_task_status(task2_id)
pos2_after = check_position_status(pos2_id)
print(f"\nAfter:")
print(f"  Task status: {task2_after['status']}")
print(f"  Position analysis_complete: {pos2_after['async_analysis_complete']}")
print(f"  Position risk_flag: {pos2_after['risk_flag']}")

if pos2_after['async_analysis_json']:
    analysis = json.loads(pos2_after['async_analysis_json'])
    print(f"  Judge verdict: {analysis['judge']['judge_verdict']['verdict']}")
    print(f"  Judge confidence: {analysis['judge']['judge_verdict']['confidence']}")

test2_pass = (
    task2_after['status'] == 'DONE' and
    pos2_after['async_analysis_complete'] == 1 and
    pos2_after['risk_flag'] == 'FLAG_JUDGE_HIGH_CONF_REJECT'
)

print(f"\n{'✅ TEST 2 PASSED' if test2_pass else '❌ TEST 2 FAILED'}")

# ============================================================================
# TEST 3: RETRY CASE
# ============================================================================

print("\n" + "=" * 70)
print("TEST 3: Retry Case (Force error → backoff)")
print("=" * 70)

# For retry test, we'll manually inject a task that will be processed
# but we can't easily force a Python error without modifying the worker
# So we'll create a position that doesn't exist to trigger ValueError

task3_id = hashlib.sha256(f"retry_test_{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()
fake_position_id = "FAKE_POSITION_ID_DOES_NOT_EXIST"
now_iso = datetime.now(timezone.utc).isoformat()

with get_connection() as conn:
    conn.execute("""
        INSERT INTO async_tasks (
            task_id, entity_id, task_type, status, 
            attempts, next_run_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task3_id, fake_position_id, "ANALYZE_EXECUTED", "PENDING",
        0, now_iso, now_iso, now_iso
    ))
    conn.commit()

print(f"\n✅ Created failing task: {task3_id[:16]}...")
print(f"   (references non-existent position)")

task3_before = check_task_status(task3_id)
print(f"\nBefore:")
print(f"  Task status: {task3_before['status']}")
print(f"  Task attempts: {task3_before['attempts']}")
print(f"  Task next_run_at: {task3_before['next_run_at']}")

print(f"\n▶ Running worker (will fail and schedule retry)...")
stdout, stderr = run_worker()
print(stdout)

task3_after = check_task_status(task3_id)
print(f"\nAfter:")
print(f"  Task status: {task3_after['status']}")
print(f"  Task attempts: {task3_after['attempts']}")
print(f"  Task next_run_at: {task3_after['next_run_at']}")

# Calculate expected backoff
next_run_dt = datetime.fromisoformat(task3_after['next_run_at'].replace("Z", "+00:00"))
now_dt = datetime.now(timezone.utc)
backoff_sec = (next_run_dt - now_dt).total_seconds()
print(f"  Backoff: ~{int(backoff_sec)}s (expected ~300s)")

test3_pass = (
    task3_after['status'] == 'PENDING' and
    task3_after['attempts'] == 1 and
    250 <= backoff_sec <= 350  # 300s ± 50s tolerance
)

print(f"\n{'✅ TEST 3 PASSED' if test3_pass else '❌ TEST 3 FAILED'}")

# ============================================================================
# SUMMARY
# ============================================================================

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"Test 1 (Normal):       {'✅ PASS' if test1_pass else '❌ FAIL'}")
print(f"Test 2 (Catastrophic): {'✅ PASS' if test2_pass else '❌ FAIL'}")
print(f"Test 3 (Retry):        {'✅ PASS' if test3_pass else '❌ FAIL'}")
print()

all_pass = test1_pass and test2_pass and test3_pass
sys.exit(0 if all_pass else 1)
