# Ticket 4 v3: Retry Semantics Fix

## Summary

All 3 blocking issues fixed and validated with deterministic test.

---

## BLOCKER 1: Retry/Backoff Semantics Now Correct

### Problem
- Config had `max_retries: 3`
- Code failed after attempt 3 (before 3600s backoff could happen)
- Policy was: 5m / 15m / then FAIL (missing 60m)

### Fix
Changed `config/thresholds.yaml`:
```yaml
max_attempts: 4  # Total attempts: 1st try + 3 retries (5m/15m/60m)
```

Updated `async_analysis_queue.py`:
```python
MAX_ATTEMPTS = COLD_PATH_CONFIG.get("max_attempts", 4)
RETRY_DELAYS = [300, 900, 3600]  # 0-indexed array

def mark_task_failed(...):
    if attempts >= MAX_ATTEMPTS:
        # FAILED permanently
    else:
        delay_sec = RETRY_DELAYS[attempts - 1]  # attempts is 1-indexed
        # Schedule retry
```

### Result
- Attempt 1 fails → retry in 300s (5 minutes)
- Attempt 2 fails → retry in 900s (15 minutes)
- Attempt 3 fails → retry in 3600s (60 minutes)
- Attempt 4 fails → FAILED permanently

All delays are now reachable.

---

## BLOCKER 2: Atomic Claim Returns Authoritative Attempts

### Problem
Worker incremented attempts in claim, but could theoretically have drift between claim and subsequent SELECT.

### Fix
`claim_task()` now fetches authoritative attempts in same connection/transaction window immediately after atomic UPDATE:

```python
def claim_task(task_id: str) -> dict:
    with get_connection() as conn:
        # Atomic UPDATE
        cursor = conn.execute("""
            UPDATE async_tasks
            SET status = 'RUNNING', attempts = attempts + 1, updated_at = ?
            WHERE task_id = ? AND status = 'PENDING' AND next_run_at <= ?
        """, ...)
        
        if cursor.rowcount == 0:
            return None
        
        # Fetch authoritative attempts in same connection
        row = conn.execute("""
            SELECT task_id, entity_id, task_type, attempts, created_at
            FROM async_tasks WHERE task_id = ?
        """, (task_id,)).fetchone()
        
        return dict(row)  # Contains DB attempts value
```

### Result
No drift between worker and DB. Worker always uses authoritative attempts value for backoff/failure decisions.

---

## BLOCKER 3: Deterministic Retry Validation Test

### Test Implementation
Created `scripts/test_async_retry_schedule.py`:

1. **Setup**: Insert task pointing to non-existent position (forced failure)
2. **Run 4 times**: Force `next_run_at` back to now each iteration
3. **Assert after each run**:
   - Status transitions: PENDING → PENDING → PENDING → FAILED
   - Attempts increment: 1 → 2 → 3 → 4
   - Backoff deltas: 300s, 900s, 3600s
   - Final state: FAILED with error code

### Test Output

```
============================================================
Async Retry Schedule Test — Ticket 4 Validation
============================================================
✓ Created test task: b4a147d7-2f15-4b57-b1eb-0087852ead7c
  Points to non-existent position: FAKE_POSITION_DOES_NOT_EXIST

--- Attempt 1 ---
Before: status=PENDING, attempts=0
Running worker...
After:  status=PENDING, attempts=1
✓ Attempts incremented correctly: 1
✓ Status=PENDING (retry scheduled)
✓ Backoff delay correct: 300s (~300s)

--- Attempt 2 ---
Before: status=PENDING, attempts=1
Running worker...
After:  status=PENDING, attempts=2
✓ Attempts incremented correctly: 2
✓ Status=PENDING (retry scheduled)
✓ Backoff delay correct: 900s (~900s)

--- Attempt 3 ---
Before: status=PENDING, attempts=2
Running worker...
After:  status=PENDING, attempts=3
✓ Attempts incremented correctly: 3
✓ Status=PENDING (retry scheduled)
✓ Backoff delay correct: 3600s (~3600s)

--- Attempt 4 ---
Before: status=PENDING, attempts=3
Running worker...
After:  status=FAILED, attempts=4
✓ Attempts incremented correctly: 4
✓ Status=FAILED (permanent failure)
✓ Error code present: ERR_VALIDATION: Position FAKE_POSITION_DOES_NOT_EX...

============================================================
✅ ALL ASSERTIONS PASSED
============================================================

Retry schedule verified:
  Attempt 1 fails → retry in 300s (5 minutes)
  Attempt 2 fails → retry in 900s (15 minutes)
  Attempt 3 fails → retry in 3600s (60 minutes)
  Attempt 4 fails → FAILED permanently

✓ Cleaned up test task: b4a147d7-2f15-4b57-b1eb-0087852ead7c
```

### Test Assertions

| Attempt | Status After | Attempts After | Backoff Delay | Result |
|---------|--------------|----------------|---------------|--------|
| 1       | PENDING      | 1              | 300s          | ✓      |
| 2       | PENDING      | 2              | 900s          | ✓      |
| 3       | PENDING      | 3              | 3600s         | ✓      |
| 4       | FAILED       | 4              | N/A           | ✓      |

**All assertions passed.**

---

## Files Modified

1. **config/thresholds.yaml** — Changed `max_retries: 3` to `max_attempts: 4`
2. **scripts/async_analysis_queue.py** — Updated backoff logic and atomic claim
3. **scripts/test_async_retry_schedule.py** — New deterministic test (executable proof)

## Git Commit

**Branch:** `v3.1-ticket-4`  
**Commit:** `733798e`  
**Message:** "Ticket 4 v3: Fix retry semantics (5m/15m/60m then FAIL)"

---

## Summary Table

| Blocker | Status | Evidence |
|---------|--------|----------|
| 1. Retry semantics | ✅ FIXED | Test shows 300s/900s/3600s delays all reached |
| 2. Atomic attempts | ✅ FIXED | claim_task() fetches authoritative DB value |
| 3. Retry validation | ✅ FIXED | test_async_retry_schedule.py passes all assertions |

**Ready for re-review.**
