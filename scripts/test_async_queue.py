#!/usr/bin/env python3
"""Test async_analysis_queue.py end-to-end."""

import sys
import hashlib
import json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '.')

from scripts.state_store import get_connection, init_db
from scripts.ids import make_signal_id, make_decision_id, make_position_id

# Initialize DB
init_db()

# Create test IDs
test_signal = {"token_address": "TEST", "source": "test", "timestamp": datetime.now(timezone.utc).isoformat()}
signal_id = make_signal_id(test_signal)
decision_id = make_decision_id(signal_id, "v3.1.0")
position_id = make_position_id(decision_id)
task_id = hashlib.sha256(f"task_{position_id}_{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()
now_iso = datetime.now(timezone.utc).isoformat()

print(f"✅ Creating test position: {position_id}")
print(f"✅ Creating test task: {task_id}")

# Create test position and task
with get_connection() as conn:
    conn.execute("""
        INSERT INTO positions (
            position_id, decision_id, signal_id, token_address, chain, strategy_id,
            entry_price, size_usd, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        position_id, decision_id, signal_id, "TEST_TOKEN", "solana", "test_strategy",
        1.23, 100.0, "OPEN", now_iso, now_iso
    ))
    
    # Create async task
    conn.execute("""
        INSERT INTO async_tasks (
            task_id, entity_id, task_type, status, 
            attempts, next_run_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task_id, position_id, "cold_path_analysis", "PENDING",
        0, now_iso, now_iso, now_iso
    ))
    
    conn.commit()

print(f"\nBefore processing:")

# Check initial state
with get_connection() as conn:
    task = conn.execute("SELECT status FROM async_tasks WHERE task_id = ?", (task_id,)).fetchone()
    pos = conn.execute("SELECT async_analysis_complete FROM positions WHERE position_id = ?", (position_id,)).fetchone()
    print(f"  Task status: {task['status']}")
    print(f"  Position analysis_complete: {pos['async_analysis_complete']}")

# Run worker
print(f"\n▶ Running async_analysis_queue.py...")
import subprocess
result = subprocess.run(
    ["python3", "scripts/async_analysis_queue.py"],
    capture_output=True,
    text=True,
    timeout=30
)

print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)

# Check final state
print(f"\nAfter processing:")
with get_connection() as conn:
    task = conn.execute("SELECT status FROM async_tasks WHERE task_id = ?", (task_id,)).fetchone()
    pos = conn.execute("""
        SELECT async_analysis_complete, async_analysis_json, risk_flag 
        FROM positions WHERE position_id = ?
    """, (position_id,)).fetchone()
    
    print(f"  Task status: {task['status']}")
    print(f"  Position analysis_complete: {pos['async_analysis_complete']}")
    print(f"  Position risk_flag: {pos['risk_flag']}")
    
    if pos['async_analysis_json']:
        analysis = json.loads(pos['async_analysis_json'])
        print(f"  Analysis keys: {list(analysis.keys())}")
        print(f"  Judge verdict: {analysis['judge']['judge_verdict']['decision']}")

# Verify
if task['status'] == 'DONE' and pos['async_analysis_complete'] == 1:
    print("\n✅ SUCCESS: Task processed PENDING → DONE, position updated")
    sys.exit(0)
else:
    print(f"\n❌ FAIL: Task status={task['status']}, complete={pos['async_analysis_complete']}")
    sys.exit(1)
