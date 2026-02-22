#!/usr/bin/env python3
"""
Test Ticket 7 — Async Queue Production Wiring

ALL tests run against an ISOLATED temp SQLite DB. Never touches production.

Tests:
1. Worker processes PENDING task with non-existent position → retry (attempts=1→PENDING, next_run_at +300s)
2. Heartbeat WARNING when PENDING task overdue >15min
3. Heartbeat CRITICAL when RUNNING task stuck beyond timeout
4. Heartbeat CRITICAL when PENDING backlog >50
5. Heartbeat OK when queue is healthy
"""

import os
import sys
import uuid
import sqlite3
import tempfile
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

from state_store import init_db, get_connection, DB_PATH


def assert_eq(label, expected, actual):
    if expected != actual:
        print(f"❌ FAIL: {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"✓ {label}: {actual!r}")


def assert_in(label, substring, actual):
    if substring not in str(actual):
        print(f"❌ FAIL: {label}: expected {substring!r} in {actual!r}")
        sys.exit(1)
    print(f"✓ {label}: contains {substring!r}")


class IsolatedDB:
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sanad_t7_")
        self.db_path = Path(self.tmpdir) / "state" / "sanad_trader.db"
        init_db(self.db_path)

    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def query_one(self, sql, params=()):
        c = self.conn()
        row = c.execute(sql, params).fetchone()
        c.close()
        return dict(row) if row else None

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def insert_async_task(db, task_id=None, entity_id=None, status="PENDING",
                      attempts=0, next_run_at=None, updated_at=None, task_type="ANALYZE_EXECUTED"):
    """Insert a task into async_tasks for testing."""
    task_id = task_id or str(uuid.uuid4())
    entity_id = entity_id or str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    next_run_at = next_run_at or now_iso
    updated_at = updated_at or now_iso

    c = db.conn()
    c.execute("""
        INSERT INTO async_tasks (task_id, entity_id, task_type, status, attempts,
                                 next_run_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (task_id, entity_id, task_type, status, attempts, next_run_at, now_iso, updated_at))
    c.commit()
    c.close()
    return task_id


# ─────────────────────────────────────────────
# TEST 1: Worker retry on non-existent position
# ─────────────────────────────────────────────

def test_worker_retry():
    print("=" * 60)
    print("TEST 1: Worker retry — non-existent position → PENDING + next_run_at +300s")
    print("=" * 60)

    db = IsolatedDB()

    # Create a PENDING task pointing to non-existent position
    fake_position_id = str(uuid.uuid4())
    task_id = insert_async_task(db, entity_id=fake_position_id, status="PENDING", attempts=0)

    # Point state_store to isolated DB
    import state_store as ss
    import async_analysis_queue as aaq
    old_db = ss.DB_PATH
    ss.DB_PATH = db.db_path

    # Monkey-patch get_connection to use isolated DB
    original_get_conn = aaq.get_connection
    from state_store import get_connection as new_get_conn
    aaq.get_connection = lambda db_path=None: new_get_conn(db.db_path)

    try:
        before = datetime.now(timezone.utc)

        # Run one poll+claim+process cycle
        task_ids = aaq.poll_pending_tasks()
        assert_eq("Found 1 task", 1, len(task_ids))
        assert_eq("Correct task_id", task_id, task_ids[0])

        claimed = aaq.claim_task(task_id)
        if claimed:
            assert_eq("Claimed attempts", 1, claimed["attempts"])
            # process_task will fail because position doesn't exist → mark_task_failed
            aaq.process_task(
                task_id=claimed["task_id"],
                entity_id=claimed["entity_id"],
                task_type=claimed["task_type"],
                attempts_now=claimed["attempts"]
            )

        after = datetime.now(timezone.utc)

        # Check DB: should be back to PENDING with attempts=1, next_run_at ~+300s
        row = db.query_one("SELECT status, attempts, next_run_at FROM async_tasks WHERE task_id=?", (task_id,))
        assert_eq("Status after failure", "PENDING", row["status"])
        assert_eq("Attempts after failure", 1, row["attempts"])

        # Verify next_run_at is ~300s in the future (within tolerance)
        next_run = datetime.fromisoformat(row["next_run_at"].replace("Z", "+00:00"))
        expected_earliest = before + timedelta(seconds=290)
        expected_latest = after + timedelta(seconds=310)
        in_range = expected_earliest <= next_run <= expected_latest
        assert_eq("next_run_at ~+300s", True, in_range)

    finally:
        ss.DB_PATH = old_db
        aaq.get_connection = original_get_conn

    print("\n✅ TEST 1 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 2: Heartbeat WARNING — PENDING task overdue >15min
# ─────────────────────────────────────────────

def test_heartbeat_warning_overdue():
    print("\n" + "=" * 60)
    print("TEST 2: Heartbeat WARNING — PENDING task overdue >15min")
    print("=" * 60)

    db = IsolatedDB()

    # Create a PENDING task with next_run_at 20 minutes ago
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    insert_async_task(db, status="PENDING", next_run_at=old_time)

    import state_store as ss
    old_db = ss.DB_PATH
    ss.DB_PATH = db.db_path

    # Monkey-patch heartbeat's state_store.get_connection
    import heartbeat as hb
    original_get_conn = None
    try:
        # We need heartbeat to use our isolated DB
        import state_store
        original_get_conn_fn = state_store.get_connection
        state_store.get_connection = lambda db_path=None: sqlite3.connect(db.db_path)
        # Need row_factory
        _orig = state_store.get_connection
        def _patched(db_path=None):
            c = sqlite3.connect(db.db_path)
            c.row_factory = sqlite3.Row
            return c
        state_store.get_connection = _patched

        result = hb.check_async_queue_backlog()
        assert_eq("Status", "WARNING", result["status"])
        assert_in("Detail mentions overdue", "overdue", result["detail"])

    finally:
        if original_get_conn_fn:
            state_store.get_connection = original_get_conn_fn
        ss.DB_PATH = old_db

    print("\n✅ TEST 2 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 3: Heartbeat CRITICAL — RUNNING task stuck
# ─────────────────────────────────────────────

def test_heartbeat_critical_stuck():
    print("\n" + "=" * 60)
    print("TEST 3: Heartbeat CRITICAL — RUNNING task stuck beyond timeout")
    print("=" * 60)

    db = IsolatedDB()

    # Create a RUNNING task with updated_at 10 minutes ago (timeout=300s, so 300+60=360s → 6min)
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    insert_async_task(db, status="RUNNING", updated_at=old_time)

    import state_store as ss
    old_db = ss.DB_PATH
    ss.DB_PATH = db.db_path

    import state_store
    original_get_conn_fn = state_store.get_connection
    def _patched(db_path=None):
        c = sqlite3.connect(db.db_path)
        c.row_factory = sqlite3.Row
        return c
    state_store.get_connection = _patched

    try:
        import heartbeat as hb
        result = hb.check_async_queue_backlog()
        assert_eq("Status", "CRITICAL", result["status"])
        assert_in("Detail mentions stuck", "stuck", result["detail"])

    finally:
        state_store.get_connection = original_get_conn_fn
        ss.DB_PATH = old_db

    print("\n✅ TEST 3 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 4: Heartbeat CRITICAL — PENDING backlog > 50
# ─────────────────────────────────────────────

def test_heartbeat_critical_backlog():
    print("\n" + "=" * 60)
    print("TEST 4: Heartbeat CRITICAL — PENDING backlog > 50")
    print("=" * 60)

    db = IsolatedDB()

    # Insert 51 PENDING tasks
    for _ in range(51):
        insert_async_task(db, status="PENDING")

    import state_store as ss
    old_db = ss.DB_PATH
    ss.DB_PATH = db.db_path

    import state_store
    original_get_conn_fn = state_store.get_connection
    def _patched(db_path=None):
        c = sqlite3.connect(db.db_path)
        c.row_factory = sqlite3.Row
        return c
    state_store.get_connection = _patched

    try:
        import heartbeat as hb
        result = hb.check_async_queue_backlog()
        assert_eq("Status", "CRITICAL", result["status"])
        assert_in("Detail mentions backlog", "backlog", result["detail"])

    finally:
        state_store.get_connection = original_get_conn_fn
        ss.DB_PATH = old_db

    print("\n✅ TEST 4 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# TEST 5: Heartbeat OK — healthy queue
# ─────────────────────────────────────────────

def test_heartbeat_ok():
    print("\n" + "=" * 60)
    print("TEST 5: Heartbeat OK — healthy queue (few recent PENDING)")
    print("=" * 60)

    db = IsolatedDB()

    # Insert 2 recent PENDING tasks (next_run_at = now, not overdue)
    now_iso = datetime.now(timezone.utc).isoformat()
    insert_async_task(db, status="PENDING", next_run_at=now_iso)
    insert_async_task(db, status="PENDING", next_run_at=now_iso)

    import state_store as ss
    old_db = ss.DB_PATH
    ss.DB_PATH = db.db_path

    import state_store
    original_get_conn_fn = state_store.get_connection
    def _patched(db_path=None):
        c = sqlite3.connect(db.db_path)
        c.row_factory = sqlite3.Row
        return c
    state_store.get_connection = _patched

    try:
        import heartbeat as hb
        result = hb.check_async_queue_backlog()
        assert_eq("Status", "OK", result["status"])
        assert_in("Detail shows counts", "2 PENDING", result["detail"])

    finally:
        state_store.get_connection = original_get_conn_fn
        ss.DB_PATH = old_db

    print("\n✅ TEST 5 PASSED")
    db.cleanup()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        test_worker_retry()
        test_heartbeat_warning_overdue()
        test_heartbeat_critical_stuck()
        test_heartbeat_critical_backlog()
        test_heartbeat_ok()

        print("\n" + "=" * 60)
        print("✅ ALL 5 TESTS PASSED (isolated temp DB, no production data touched)")
        print("=" * 60)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
