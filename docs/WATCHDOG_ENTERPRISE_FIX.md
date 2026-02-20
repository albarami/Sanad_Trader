# Watchdog Enterprise Fix - Implementation Guide

## Problem Statement
Watchdog creates false escalations and recreates `openclaw_escalation.json` even when system is healthy due to:
1. Using wrong timestamp sources (mixing cron_health + router_state)
2. Timezone-naive datetime math causing fake stall durations
3. No recovery cleanup path (escalation artifacts persist after recovery)
4. No lease-based truth (depends on flaky OpenClaw state)

## Required Changes

### 1. Fix Timezone Math (ALL datetime.utcnow() → datetime.now(timezone.utc))

**File:** `scripts/watchdog.py`

**Pattern to find:**
```python
datetime.utcnow()
```

**Replace with:**
```python
datetime.now(timezone.utc)
```

**Lines to fix (approximate):**
- Line 82: `_log()` timestamp
- Line 94: `_log_action()` timestamp  
- Line 457: `check_reconciliation_staleness()` age calculation
- Line 754: `check_router_stall()` age calculation
- Line 851: fast_path_flag write
- Line 1230: `check_cost_runaway()` datetime comparison

**Import fix needed at top:**
```python
from datetime import datetime, timedelta, timezone
```

### 2. Add Lease-Based Truth to check_router_stall()

**Current logic (line 718):**
- Reads `cron_health.json` for last_run
- Calculates age with tz-naive math
- Escalates if age > 30min

**New logic:**
```python
def check_router_stall():
    """Check if router is stalled using lease as truth."""
    
    # PRIORITY 1: Check lease file (deterministic truth)
    lease_path = STATE_DIR / "leases" / "signal_router.json"
    if lease_path.exists():
        try:
            lease = json.load(open(lease_path))
            heartbeat_at = lease.get("heartbeat_at", lease.get("started_at"))
            ttl = lease.get("ttl_seconds", 720)
            
            heartbeat_dt = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            age_sec = (now - heartbeat_dt).total_seconds()
            
            # If lease is fresh, router is HEALTHY - clear any escalations
            if age_sec < ttl:
                _clear_escalation_artifacts("signal_router")
                return []  # No issues
        except Exception as e:
            _log(f"Lease check failed: {e}", "WARNING")
    
    # FALLBACK: Check cron_health if no lease
    health_file = STATE_DIR / "cron_health.json"
    if not health_file.exists():
        return []
    
    health = json.load(open(health_file))
    router_entry = health.get("signal_router", {})
    last_run_str = router_entry.get("last_run")
    
    if not last_run_str:
        return []
    
    # tz-aware parsing
    last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    age_minutes = (now - last_run).total_seconds() / 60
    
    # Continue with existing escalation logic...
    if age_minutes > ROUTER_STALL_MIN:
        # ... existing tier logic ...
        pass
```

### 3. Add Recovery Cleanup Function

**Add to watchdog.py:**

```python
def _clear_escalation_artifacts(component):
    """
    Clear escalation artifacts when component recovers.
    Called when lease/heartbeat shows component is healthy.
    """
    files_to_clear = [
        STATE_DIR / "openclaw_escalation.json",
        STATE_DIR / "router_paused.flag",
        STATE_DIR / "router_fast_path.flag"
    ]
    
    cleared = []
    for f in files_to_clear:
        if f.exists():
            f.unlink()
            cleared.append(f.name)
    
    # Reset attempt counters
    _reset_attempts(component)
    
    if cleared:
        _log(f"Cleared escalation artifacts for {component}: {cleared}")
```

### 4. Fix check_stuck_openclaw_jobs() to Use Leases

**Current logic:**
- Reads OpenClaw cron list
- Checks `runningAtMs` age
- Auto disable/enable if stuck

**Add lease check first:**

```python
def check_stuck_openclaw_jobs():
    """Auto-fix OpenClaw jobs stuck in runningAtMs state."""
    issues = []
    fixed = []
    
    critical_jobs = {
        "Signal Router": {
            "id": "00079d3a-0206-4afc-9dd9-8263521e1bf3",
            "lease_name": "signal_router",
            "timeout": 600,
            "grace": 120
        },
        # ... other jobs ...
    }
    
    for job_name, config in critical_jobs.items():
        lease_name = config.get("lease_name")
        
        # PRIORITY 1: Check lease (if available)
        if lease_name:
            lease_path = STATE_DIR / "leases" / f"{lease_name}.json"
            if lease_path.exists():
                try:
                    lease = json.load(open(lease_path))
                    heartbeat_at = lease.get("heartbeat_at", lease.get("started_at"))
                    ttl = lease.get("ttl_seconds", config["timeout"] + config["grace"])
                    
                    heartbeat_dt = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    age_sec = (now - heartbeat_dt).total_seconds()
                    
                    # If lease is fresh, job is HEALTHY
                    if age_sec < ttl:
                        # Clear any stuck OpenClaw state as preventive measure
                        _reset_attempts(f"stuck_openclaw_{lease_name}")
                        continue  # Skip to next job
                except Exception as e:
                    _log(f"Lease check failed for {job_name}: {e}", "WARNING")
        
        # FALLBACK: Check OpenClaw runningAtMs (existing logic)
        # ... existing disable/enable logic ...
```

### 5. Integration Steps

1. **Fix all datetime.utcnow() calls** (search and replace)
2. **Add _clear_escalation_artifacts() function**
3. **Update check_router_stall() with lease priority**
4. **Update check_stuck_openclaw_jobs() with lease priority**
5. **Test watchdog manually**
6. **Commit and deploy**

### 6. Testing

```bash
cd /data/.openclaw/workspace/trading

# Test lease file exists
ls -la state/leases/signal_router.json

# Test watchdog doesn't escalate when lease is fresh
python3 scripts/watchdog.py

# Verify no openclaw_escalation.json created
ls state/openclaw_escalation.json  # Should not exist if router is healthy

# Check logs
tail -100 logs/watchdog.log
```

### 7. Expected Behavior After Fix

**Healthy system:**
- Lease files updated every run
- Watchdog reads lease → sees fresh heartbeat → returns [] (no issues)
- No Telegram alerts
- No escalation files created

**Actual stuck job:**
- Lease becomes stale (age > TTL)
- Watchdog detects via lease
- Auto disable/enable OpenClaw job
- Only escalates if fix fails 2x

**False positive eliminated:**
- Even if cron_health.json is stale
- Even if OpenClaw shows "running"
- Lease is authoritative → no false alerts

## Completion Checklist

- [ ] Fix all datetime.utcnow() calls (search/replace)
- [ ] Add _clear_escalation_artifacts() function
- [ ] Update check_router_stall() with lease priority
- [ ] Update check_stuck_openclaw_jobs() with lease priority  
- [ ] Update check_data_freshness() with lease priority (onchain/coingecko)
- [ ] Test watchdog manually (no false alerts)
- [ ] Commit changes
- [ ] Monitor for 24 hours (should be silent when healthy)

## Success Criteria

1. ✅ No more "Router stalled 2670min" false positives
2. ✅ No more recreated openclaw_escalation.json after manual clear
3. ✅ Watchdog silent when lease files are fresh
4. ✅ Auto-remediation works on real stuck jobs
5. ✅ No DeprecationWarning in watchdog output
