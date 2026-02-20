# Sanad Trader v3.0 — Phase 1-3 Operational Runbook

**Version:** 2026-02-21  
**Current State:** Hotfixes applied (Binance + MEXC breaker self-heal, NTP observational, LIVE strict)  
**System Mode:** PAPER  
**Next Steps:** 24h soak → full patch → LIVE trial

---

## Phase 1 — 24h PAPER Soak (Acceptance Criteria)

### What You Should See for 24 Hours

✅ **Heartbeat stays Overall: OK** (or at worst occasional WARNING that's explained and transient)

✅ **NTP check remains observational**
- No breaker mutations during `check_ntp_sync()`
- No `[BINANCE]` or `[MEXC] … RESET` messages caused by heartbeat

✅ **Clock skew stays stable**
- `abs_skew_ms <= 1000` almost always
- No spikes > 2000ms

✅ **Breaker stability**
- No "stuck OPEN" after cooldown expiry
- If it trips, it must close automatically on the next successful request (hotfix enforces this)

✅ **Scheduler health**
- Router `lastRunAtMs` advances consistently (no frozen runs)
- `lastDurationMs` stays comfortably below the schedule interval

### Minimal Monitoring Commands (Copy/Paste)

```bash
cd /data/.openclaw/workspace/trading

# 1) Watch last 200 heartbeats
tail -200 execution-logs/heartbeat.log

# 2) Ensure breaker file stays sane
cat state/circuit_breakers.json | python3 -m json.tool

# 3) Spot-check skew without side effects (raw)
python3 - <<'PY'
import time, json, urllib.request
r=json.loads(urllib.request.urlopen("https://api.binance.com/api/v3/time", timeout=5).read())
local=int(time.time()*1000); server=int(r["serverTime"])
print("abs_skew_ms:", abs(local-server))
PY
```

---

## Phase 2 — Maintenance Window: Apply Full Ship-Safe Breaker Architecture

### Scope to Apply

1. **OPEN → HALF_OPEN auto-transitions** (policy_engine + heartbeat)
2. **Unique-temp atomic writes everywhere** (avoid tmp collisions)
3. **HALF_OPEN crash recovery** (probe TTL + fail-fast reopen)
4. **Persisted-state ErrorTracker** (Binance + MEXC parity)

### Patch Validation Sequence (Must Run in This Order)

```bash
cd /data/.openclaw/workspace/trading

# 0) Backups (fast rollback)
cp scripts/policy_engine.py scripts/policy_engine.py.bak.$(date +%s)
cp scripts/heartbeat.py scripts/heartbeat.py.bak.$(date +%s)
cp scripts/binance_client.py scripts/binance_client.py.bak.$(date +%s)
cp scripts/mexc_client.py scripts/mexc_client.py.bak.$(date +%s)
cp state/circuit_breakers.json state/circuit_breakers.json.bak.$(date +%s) 2>/dev/null || true

# 1) Syntax gate
python3 -m py_compile scripts/policy_engine.py scripts/heartbeat.py scripts/binance_client.py scripts/mexc_client.py

# 2) Forced breaker scenario (OPEN expired → HALF_OPEN → probe → CLOSED)
python3 - <<'PY'
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
p=Path("state/circuit_breakers.json")
d=json.loads(p.read_text()) if p.exists() else {}
d["binance_api"]={
    "state":"open",
    "failure_count":5,
    "cooldown_until":(datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat()
}
p.write_text(json.dumps(d,indent=2))
print("✅ Forced binance_api open (expired)")
PY

python3 scripts/heartbeat.py | tail -60

python3 - <<'PY'
import sys
sys.path.insert(0, 'scripts')
from binance_client import _request
print(_request("GET","/api/v3/time", signed=False, timeout=5))
PY

cat state/circuit_breakers.json | python3 -m json.tool
```

**Pass Condition:** Breaker ends as `closed` with a recent `last_success_at`, and heartbeat reports `circuit_breakers: OK`.

---

## Phase 3 — LIVE Trial (Strict Mode)

### Safety Posture for the First Hour

1. **Start with kill switch ACTIVE**
2. **Verify health checks under LIVE rules** (NTP strict, breaker strict)
3. **Only then flip kill switch to INACTIVE** for limited operations

### Recommended Steps

```bash
cd /data/.openclaw/workspace/trading

# 1) Set LIVE mode explicitly
export SYSTEM_MODE=LIVE

# 2) Run heartbeat → must be OK (strict enforcement active)
python3 scripts/heartbeat.py | tail -60

# Expected: ntp_sync must be OK (skew ≤2s)
# If >2s or unmeasurable → status=CRITICAL (blocks trading)

# 3) Test signed Binance endpoints with small recvWindow
python3 - <<'PY'
import sys
sys.path.insert(0, 'scripts')
from binance_client import get_account_info
result = get_account_info()
if result:
    print("✅ Signed request successful")
    print(f"Balances: {len(result.get('balances', []))} assets")
else:
    print("❌ Signed request failed")
PY

# 4) Keep exposure caps + circuit breakers tight for first session
# Monitor for:
# - Clock skew remains ≤2s
# - Breakers respond correctly to failures
# - No false positives (legitimate requests blocked)
```

### One Small But Important Safeguard

**Make `SYSTEM_MODE` explicit in production** so it can't silently default:

- **In PAPER:** Set `SYSTEM_MODE=PAPER`
- **In LIVE:** Set `SYSTEM_MODE=LIVE`
- **No "implicit" mode**

Add to your environment (docker-compose.yml, .env, or systemd unit):
```bash
SYSTEM_MODE=PAPER  # or LIVE
```

---

## Current Hotfixes Applied (2026-02-21)

### Binance Client
✅ Success path always calls `reset_after_success()` (eliminates stuck OPEN)

### MEXC Client
✅ Added `reset_after_success()` method  
✅ Success path always calls `reset_after_success()` (parity with Binance)

### Heartbeat
✅ NTP check uses `_binance_time_raw()` (observational, no breaker mutations)  
✅ Mode-aware strictness:
- PAPER: ≤2s=OK, >2s=WARNING, unmeasurable=WARNING
- LIVE: ≤2s=OK, >2s=CRITICAL, unmeasurable=CRITICAL

### Current Status
```json
{
  "Overall": "OK",
  "clock_skew": "38-43ms (ideal)",
  "circuit_breakers": "all closed",
  "system_mode": "PAPER"
}
```

---

## Rollback Plan (If Needed)

```bash
cd /data/.openclaw/workspace/trading

# Find backup timestamp
ls -la scripts/*.bak.*

# Restore from backup (replace TIMESTAMP)
cp scripts/policy_engine.py.bak.TIMESTAMP scripts/policy_engine.py
cp scripts/heartbeat.py.bak.TIMESTAMP scripts/heartbeat.py
cp scripts/binance_client.py.bak.TIMESTAMP scripts/binance_client.py
cp scripts/mexc_client.py.bak.TIMESTAMP scripts/mexc_client.py
cp state/circuit_breakers.json.bak.TIMESTAMP state/circuit_breakers.json

# Verify syntax
python3 -m py_compile scripts/*.py

# Run heartbeat to confirm
python3 scripts/heartbeat.py | tail -50
```

---

## Contact & Escalation

**Owner:** Salim Al-Barami  
**Timezone:** Asia/Qatar (GMT+3)  
**Mode:** PAPER (conservative risk tolerance)  
**Promotion to LIVE:** SSH access required (by Salim only)

---

**Last Updated:** 2026-02-21 05:36 GMT+8  
**Next Review:** After 24h PAPER soak (2026-02-22 05:36 GMT+8)
