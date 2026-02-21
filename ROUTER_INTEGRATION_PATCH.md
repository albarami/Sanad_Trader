# SIGNAL_ROUTER.PY INTEGRATION PATCH FOR v3.1

## Summary
Replace subprocess call to `sanad_pipeline.py` with direct function call to `fast_decision_engine.evaluate_signal_fast()`.

## Changes Required

### 1. Add imports at top of file (after existing imports)

```python
# v3.1 Hot Path imports
import fast_decision_engine
import state_store
import ids
```

### 2. Replace the subprocess execution block

**FIND (approximately line ~700-750):**
```python
# OLD v3.0 CODE:
result = subprocess.run(
    ["python3", PIPELINE_SCRIPT, "--signal-file", signal_file],
    timeout=300,  # 5 minutes
    capture_output=True
)
```

**REPLACE WITH:**
```python
# NEW v3.1 CODE:
# Load portfolio and runtime state
portfolio = _load_json(PORTFOLIO_PATH, default={
    "cash_balance_usd": 10000,
    "open_position_count": 0,
    "total_exposure_pct": 0
})

runtime_state = {
    "min_score": 40,
    "regime_tag": get_current_regime(),
    "ucb1_grades": load_ucb1_grades(),
    "thompson_state": load_thompson_state()
}

# Call fast decision engine (Hot Path)
try:
    decision_record = fast_decision_engine.evaluate_signal_fast(
        signal=enriched_signal,
        portfolio=portfolio,
        runtime_state=runtime_state,
        policy_version="v3.1.0"
    )
except Exception as e:
    _log(f"Fast decision engine error: {e}")
    decision_record = {
        "result": "SKIP",
        "reason_code": "SKIP_ENGINE_ERROR",
        "error": str(e)
    }

# Log decision based on result
result_type = decision_record.get("result")

if result_type in ("SKIP", "BLOCK"):
    # Log to DB (fallback to JSONL if DB busy)
    try:
        state_store.insert_decision(decision_record)
    except state_store.DBBusyError:
        _log(f"DB busy, logging to JSONL only: {decision_record['decision_id']}")
        append_to_jsonl("logs/decisions.jsonl", decision_record)
    except Exception as e:
        _log(f"Decision insert error: {e}, logging to JSONL fallback")
        append_to_jsonl("logs/decisions.jsonl", decision_record)
    
    _log(f"Decision: {result_type} - {decision_record.get('reason_code')}")

elif result_type == "EXECUTE":
    # Position already created by try_open_position_atomic() in engine
    position_id = decision_record.get("execution", {}).get("position_id")
    _log(f"Decision: EXECUTE - Position {position_id} opened")
    
    # Update portfolio state (increment counters)
    portfolio["open_position_count"] += 1
    portfolio["daily_trades"] = portfolio.get("daily_trades", 0) + 1
    _save_json_atomic(PORTFOLIO_PATH, portfolio)

# Always append to JSONL for observability
append_to_jsonl("logs/decisions.jsonl", decision_record)
```

### 3. Add helper functions (if not already present)

```python
def load_ucb1_grades():
    """Load UCB1 source grades from DB or state file."""
    try:
        with state_store.get_connection() as conn:
            rows = conn.execute("SELECT source_id, n, reward_sum FROM source_ucb_stats").fetchall()
            return {row["source_id"]: compute_grade(row) for row in rows}
    except Exception:
        return {}

def load_thompson_state():
    """Load Thompson sampling state from DB or state file."""
    try:
        with state_store.get_connection() as conn:
            rows = conn.execute("SELECT strategy_id, regime_tag, alpha, beta FROM bandit_strategy_stats").fetchall()
            return {f"{row['strategy_id']}_{row['regime_tag']}": {"alpha": row["alpha"], "beta": row["beta"]} for row in rows}
    except Exception:
        return {}

def get_current_regime():
    """Get current market regime (placeholder)."""
    # TODO: integrate with regime_classifier.py
    return "NEUTRAL"

def append_to_jsonl(filepath, record):
    """Append JSON record to .jsonl file."""
    try:
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        _log(f"JSONL append error: {e}")

def compute_grade(ucb_row):
    """Compute letter grade from UCB1 stats (placeholder)."""
    if ucb_row["n"] < 10:
        return "C"  # Insufficient data
    win_rate = ucb_row["reward_sum"] / ucb_row["n"]
    if win_rate >= 0.7:
        return "A"
    elif win_rate >= 0.6:
        return "B"
    elif win_rate >= 0.5:
        return "C"
    elif win_rate >= 0.4:
        return "D"
    else:
        return "F"
```

### 4. Update batch size logic

**FIND (approximately line ~650):**
```python
BATCH_SIZE = 2  # or similar
```

**REPLACE WITH:**
```python
# Dynamic batch size based on DB position count
try:
    BATCH_SIZE = state_store.get_batch_size()
except Exception:
    BATCH_SIZE = 5  # Default
```

### 5. Remove/comment out signal_queue.py lease logic (if present)

**FIND blocks like:**
```python
if HAS_LEASE:
    lease_id = acquire(...)
    ...
    release(lease_id)
```

**ACTION:** Comment out or wrap in `if False:` block (not needed with direct function call)

---

## Testing After Integration

1. **Dry run:**
   ```bash
   python3 scripts/signal_router.py --dry-run
   ```

2. **Check decision log:**
   ```bash
   tail -f logs/decisions.jsonl
   ```

3. **Check DB:**
   ```bash
   sqlite3 state/sanad_trader.db "SELECT COUNT(*) FROM decisions;"
   sqlite3 state/sanad_trader.db "SELECT COUNT(*) FROM positions;"
   ```

4. **Performance:**
   - Each signal should complete in <3s
   - Batch of 5 signals should complete in <15s

---

## Rollback Plan

If v3.1 integration fails:

1. Revert signal_router.py to previous commit
2. System falls back to v3.0 (sanad_pipeline.py subprocess)
3. No data loss (SQLite DB independent of router)

---

## Expected Behavior After Patch

- **SKIP decisions:** Logged to DB + JSONL
- **BLOCK decisions:** Logged to DB + JSONL
- **EXECUTE decisions:** Position in DB (via try_open_position_atomic), decision in DB, both in JSONL
- **DB busy:** Graceful fallback to JSONL-only logging
- **Throughput:** 5-10 signals per cycle (vs 2 in v3.0)
- **Latency:** <3s per signal (vs 4-6min in v3.0)
