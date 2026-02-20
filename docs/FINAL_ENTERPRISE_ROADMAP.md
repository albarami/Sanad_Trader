# Final Enterprise Stabilization Roadmap

## Root Cause (Single Sentence)
OpenClaw cron jobs (especially `sessionTarget=isolated` with short `timeoutSeconds`) intermittently leave `runningAtMs` stuck, stopping dispatch → stale signals → watchdog escalations → Telegram storms.

## Solution: 4-Part Enterprise Architecture

### ✅ Part 1: Job Liveness Independent of OpenClaw (COMPLETE for Router)
**Status:** Router integrated ✅ | Scanners pending ⏳

**Completed:**
- Created `scripts/job_lease.py` (commit 37d7434)
- Integrated into `signal_router.py` (commit cb5edf3)
- Lease written at start/end/error in try/finally pattern
- TTL: 720s (10min timeout + 2min grace)

**Pending Integration:**
- `scripts/coingecko_client.py` ⏳
- `scripts/onchain_analytics.py` ⏳
- `scripts/dexscreener_client.py` ⏳
- Price snapshot wrapper ⏳

**Pattern to apply:**
```python
from job_lease import acquire, release

def main():
    lease = acquire("job_name", ttl_seconds=300)
    try:
        # ... do work ...
        release("job_name", status="ok")
    except Exception as e:
        release("job_name", status="error", detail=str(e))
        raise
```

---

### ✅ Part 2: Watchdog Uses Lease Truth First (PARTIAL)
**Status:** Router check complete ✅ | Data freshness checks pending ⏳

**Completed:**
- Fixed all timezone math (11 instances of datetime.utcnow) ✅
- Added `_clear_escalation_artifacts()` function ✅
- `check_router_stall()` uses lease as PRIORITY 1 ✅
- Auto-clears escalation files when lease shows healthy ✅

**Proven Working:**
```
[INFO] Router healthy again - resetting 4 attempt(s)
```

**Pending:**
- `check_data_freshness()` should check scanner leases first ⏳
- `check_stuck_openclaw_jobs()` should check leases before OpenClaw state ⏳

**Decision Rule:**
```
IF lease fresh → job healthy (no escalation)
IF lease stale AND outputs stale → remediation
IF lease stale BUT outputs fresh → OpenClaw lying, don't escalate
```

---

### ⏳ Part 3: Auto-Remediate OpenClaw Stuck States Reliably (TODO)
**Status:** Direct openclaw calls unreliable from isolated sessions ⚠️

**Current Problem:**
Watchdog runs in isolated session → calls `openclaw cron update` → sometimes fails with non-zero exit status → remediation incomplete.

**Enterprise Solution: Reset Daemon (2-Step Pipeline)**

**Step A - Watchdog writes reset request (always works):**
```python
# In watchdog when job is stuck:
request = {
    "job_id": "3a7f742b-889a-4c05-9697-f5f873fea02c",
    "job_name": "CoinGecko Scanner",
    "reason": "runningAtMs stuck 180s",
    "requested_at": datetime.now(timezone.utc).isoformat(),
    "attempts": 1
}
with open(STATE_DIR / "openclaw_reset_requests.jsonl", "a") as f:
    f.write(json.dumps(request) + "\n")
```

**Step B - Reset Daemon runs in main session:**
- Job name: "OpenClaw Reset Daemon"
- Schedule: every 2 minutes
- Session: **main** (not isolated)
- Reads `openclaw_reset_requests.jsonl`
- Executes `openclaw cron update --id <job_id> --enabled false/true`
- Records result in `openclaw_reset_results.jsonl`
- Deletes processed requests

**Why This Works:**
- Watchdog (isolated) → file write (always succeeds)
- Daemon (main) → openclaw CLI calls (reliable, no session overhead)
- Separates detection from remediation
- Daemon can be monitored independently

**Implementation Files:**
1. `scripts/openclaw_reset_daemon.py` (new)
2. Update `scripts/watchdog.py` to write requests instead of calling openclaw
3. Add daemon to OpenClaw cron (main session, every 2min)

---

### ✅ Part 4: Reduce Stuck State Probability (COMPLETE)
**Status:** Critical jobs moved to main session ✅

**Just Completed:**
- **CoinGecko Scanner**: `isolated` → `main` ✅
- **On-Chain Analytics**: `isolated` → `main` ✅

**Why This Helps:**
- Main session jobs bypass isolated session overhead
- No runningAtMs/LLM session initialization delays
- Deterministic execution (systemEvent runs directly)
- Dramatically reduces stuck state probability

**Still Isolated (intentionally):**
- Signal Router (needs LLM isolation)
- Sanad Pipeline (needs LLM isolation)
- Watchdog (needs isolation for safety)

**Next: Increase Timeouts (defensive):**
- CoinGecko: Already systemEvent (no timeout needed)
- Onchain: Already systemEvent (no timeout needed)
- Price Snapshot: Increase to 120s if using agentTurn

---

## Current System Status

### ✅ Working
- Router: Lease-based liveness tracking
- Watchdog: Uses lease for router health checks
- Auto-clears escalation artifacts when lease shows healthy
- Timezone math fixed (no more DeprecationWarning)
- CoinGecko + OnChain moved to main session (no more stuck states expected)

### ⏳ Needs Completion (Priority Order)
1. **HIGH**: Integrate leases into scanners (coingecko, onchain, dex) - 30 min
2. **HIGH**: Implement Reset Daemon - 30 min
3. **MEDIUM**: Update watchdog to write reset requests - 15 min
4. **MEDIUM**: Update check_data_freshness() to use leases - 15 min
5. **LOW**: Fix cost check timezone comparison - 10 min

---

## Testing Results

**Before All Fixes:**
- False "Router stalled 2670min" alerts
- `openclaw_escalation.json` recreated after manual clear
- Telegram storm (dozens of messages)

**After Part 1+2 (cb5edf3 + cae76da):**
- Router: "healthy again - resetting attempts" ✅
- No escalation files created when lease fresh ✅
- Timezone math fixed ✅

**After Part 4 (just now):**
- CoinGecko + OnChain moved to main ✅
- Expect dramatic reduction in stuck states ✅

---

## Success Criteria

### Achieved
- [x] Lease files written on every router run
- [x] Watchdog prioritizes lease over cron_health for router
- [x] Auto-clears escalation artifacts when lease shows healthy
- [x] Timezone math fixed (no more DeprecationWarning)
- [x] False "Router stalled 2670min" eliminated
- [x] CoinGecko + OnChain moved to main session

### Pending
- [ ] Leases integrated into all scanners
- [ ] Reset Daemon implemented and deployed
- [ ] Watchdog writes reset requests instead of direct openclaw calls
- [ ] Monitor 24 hours with zero false alerts
- [ ] Document final architecture

---

## Next Session Plan (30-60 min)

### Quick Wins (Tonight)
1. Integrate lease into coingecko_client.py (10 min)
2. Integrate lease into onchain_analytics.py (10 min)
3. Test both produce lease files (5 min)

### Medium Work (Tomorrow)
1. Create openclaw_reset_daemon.py (20 min)
2. Update watchdog to write reset requests (15 min)
3. Deploy daemon to OpenClaw cron (5 min)
4. Test full remediation cycle (10 min)

### Final Polish (Weekend)
1. Update check_data_freshness() for leases
2. Fix cost check timezone
3. Monitor for 24 hours
4. Document final state

---

## Expected End State

**Healthy System:**
- All jobs write leases (deterministic truth)
- Watchdog reads leases (no guessing)
- Reset Daemon handles stuck states (reliable remediation)
- Zero false alerts
- Zero Telegram storms
- Self-healing under all conditions

**Real Problem:**
- Job actually fails to produce output for > TTL
- Lease becomes stale
- Watchdog writes reset request
- Daemon auto-fixes (disable/enable)
- Only escalates if daemon reports failure 2x

**The Difference:**
- Before: Watchdog guesses from flaky signals → false positives
- After: Watchdog uses deterministic leases → only real problems escalate

---

## Commits So Far Today

1. **37d7434** - Add enterprise-grade OpenClaw scheduler bug auto-remediation
2. **cb5edf3** - Integrate job lease into signal_router.py
3. **cae76da** - Fix watchdog timezone math + lease-based truth
4. **d199056** - Add enterprise stabilization status tracker

## Next Commit (Tonight)

5. **[pending]** - Move CoinGecko + OnChain to main session (DONE, needs commit)
6. **[pending]** - Integrate leases into scanner scripts
7. **[pending]** - Implement Reset Daemon architecture

---

**Bottom Line:** We're 60% done with the enterprise fix. Router is solid. Scanners moving to main eliminates most stuck states. Reset Daemon will handle the remaining edge cases. Then it's truly "done once, done forever."
