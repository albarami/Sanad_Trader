# Enterprise Stabilization Status

## Goal
Stop Telegram alert storms and eliminate false "stalled X minutes" escalations permanently through deterministic lease-based liveness tracking.

## Root Cause (Confirmed)
OpenClaw scheduler bug leaves jobs stuck in `runningAtMs` state + watchdog using flaky timestamp sources + timezone-naive datetime math → cascading false alerts even when system is healthy.

## Solution: 4-Part Enterprise Fix

### ✅ Part 1: Job Lease System (COMPLETE)
**Commit:** 37d7434, cb5edf3

**Created:**
- `scripts/job_lease.py` - Deterministic liveness tracking
- Lease acquire/release in `signal_router.py` with try/finally pattern
- Lease files written to `state/leases/signal_router.json`
- TTL: 720s (10min timeout + 2min grace)

**Status:** ✅ Router now writes lease on every run

**Proof:**
```bash
ls -la state/leases/signal_router.json
# Should exist and update every 10 minutes
```

### ✅ Part 2: Watchdog Lease-Based Truth (COMPLETE)
**Commit:** cae76da

**Fixed:**
- All 11 `datetime.utcnow()` → `datetime.now(timezone.utc)` (eliminates tz-naive math)
- Added `_clear_escalation_artifacts()` function
- `check_router_stall()` now checks lease FIRST (priority 1)
- Auto-clears escalation files when lease is fresh
- `check_stuck_openclaw_jobs()` enhanced with lease priority

**Status:** ✅ Watchdog now reports "Router healthy again - resetting attempts" when lease is fresh

**Proof:**
```
[INFO] Router healthy again - resetting 4 attempt(s)
```

**Eliminated:**
- ❌ False "Router stalled 2670min" alerts
- ❌ False "Router stalled 45min" alerts  
- ❌ Recreated `openclaw_escalation.json` after manual clear
- ❌ DeprecationWarning for datetime.utcnow()

### ⏳ Part 3: Integrate Leases into Scanners (TODO)
**Pending integration:**
- `scripts/coingecko_client.py`
- `scripts/onchain_analytics.py`
- `scripts/dexscreener_client.py`
- Price snapshot runner

**Pattern to apply:**
```python
from job_lease import acquire, release

def main():
    acquire("job_name", ttl_seconds=180)
    try:
        # ... do work ...
        release("job_name", "ok")
    except Exception as e:
        release("job_name", "error", str(e))
        raise
```

**Why needed:**
- Eliminates "stale coingecko 22min" false alerts
- Gives watchdog deterministic truth for all critical jobs
- Stops dependency on OpenClaw scheduler state

### ⏳ Part 4: Cost Check Timezone Fix (TODO)
**Remaining issue:**
```
[ERROR] Cost check failed: can't compare offset-naive and offset-aware datetimes
```

**Location:** `scripts/watchdog.py` line ~1230 in `check_cost_runaway()`

**Fix:** Ensure all datetime objects in cost comparison are tz-aware

## Current System Status

### ✅ Working
- Router writes lease every run
- Watchdog uses lease to determine router health
- Auto-clears escalation artifacts when healthy
- No more false "Router stalled" for router specifically
- Timezone math fixed (11 instances)

### ⚠️ Needs Completion
- Scanner lease integration (coingecko, onchain, dexscreener)
- Cost check timezone fix
- Monitor for 24 hours to verify no false alerts

## Testing Results

**Before fix:**
- Router running fine (last_run 3 min ago)
- Watchdog still escalated with "OpenClaw working on signal_router"
- `openclaw_escalation.json` recreated after manual deletion

**After fix (cb5edf3 + cae76da):**
- Router running fine (lease fresh)
- Watchdog reports "Router healthy again - resetting attempts"
- No escalation files created
- ✅ **Working as designed**

## Success Criteria

### Achieved
- [x] Lease files written on every router run
- [x] Watchdog prioritizes lease over cron_health
- [x] Auto-clears escalation artifacts when lease shows healthy
- [x] Timezone math fixed (no more DeprecationWarning)
- [x] False "Router stalled 2670min" eliminated

### Pending
- [ ] Integrate leases into scanners (coingecko, onchain, dex)
- [ ] Fix cost check timezone comparison
- [ ] Monitor 24 hours with no false alerts
- [ ] Document lease integration pattern for future jobs

## Commits
1. **37d7434** - Add enterprise-grade OpenClaw scheduler bug auto-remediation
2. **cb5edf3** - Integrate job lease into signal_router.py
3. **cae76da** - Fix watchdog timezone math + lease-based truth

## Next Steps

1. **Tonight** (30 min):
   - Integrate lease into coingecko_client.py
   - Integrate lease into onchain_analytics.py
   - Test watchdog shows no false "stale" alerts

2. **Tomorrow** (monitoring):
   - Watch for any remaining false alerts
   - Verify system stable for 24 hours
   - Document final state

## Expected Behavior

**Healthy system:**
- Lease files update every run
- Watchdog reads lease → sees fresh → returns [] (no issues)
- Zero Telegram alerts
- Zero escalation files

**Real problem:**
- Lease becomes stale (age > TTL)
- Watchdog detects via lease
- Auto disable/enable OpenClaw job (attempt 1-2)
- Only escalates if auto-fix fails twice

**The difference:**
Before: Watchdog could escalate even when healthy (flaky signals)
After: Watchdog only escalates when lease proves actual staleness
