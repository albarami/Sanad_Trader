# Autonomy Gap Fixes — v3.1 Quality Loop

**Date:** 2026-02-23  
**Status:** ✅ DEPLOYED  
**Root Cause:** Hot path approving trades that cold path (Judge) rejects at 99% confidence

---

## Problem Analysis

### Pattern Detected
- **10/10 consecutive executions** rejected by Judge at 90-99% confidence
- Examples: USDT/USDT, YOGURT (2 holders, 99.48% top10), BUTTCOIN, CC, etc.
- Root cause: Hot path safety gates too shallow, missing data treated as "OK"

### Why Hot Path Approved These Trades
1. **Stablecoins (USDT):** High volume, cross-sourced, low apparent risk → approved
2. **Self-pairs (USDT/USDT):** Symbol field mismatch, no universal gate
3. **Missing holder data (YOGURT):** Assumed OK instead of fail-closed
4. **No learning from Judge rejects:** PnL-only learning (~0% loss = weak signal)

---

## Solution: 5 Autonomy Fixes

### Fix 1: Universal Safety Gates (Cannot Be Bypassed)
**File:** `scripts/fast_decision_engine.py` → `stage_1_hard_safety_gates()`

**New gates run BEFORE `if HAS_HARD_GATES: return`:**

1. **Self-pair blocking**
   - Detects: `USDT/USDT`, `BTC/BTC`, etc.
   - Reason: `BLOCK_SELF_PAIR`

2. **Stablecoin blocking (symbol-based)**
   - Symbols: USDT, USDC, DAI, BUSD, USDD, TUSD, FRAX, USDP, GUSD, PAX
   - Handles field variance: `token`, `symbol`, `pair`, `pair_symbol`, `ticker`, `market`
   - Reason: `BLOCK_STABLECOIN`

3. **Stablecoin blocking (address-based)**
   - Solana mainnet: Es9vMFr..., EPjFWdd..., EjmyN6q..., AJ1W9A9...
   - Reason: `BLOCK_STABLECOIN_ADDR`

4. **Missing holder data (fail-closed)**
   - If `holder_count == 0` AND `top10_pct == 0` → microcaps fail-closed
   - Majors (BTC, ETH, SOL) exempt
   - Reason: `BLOCK_MISSING_HOLDER_DATA`

5. **Holder concentration gates**
   - `holder_count < 10` → `BLOCK_HOLDER_COUNT_CRITICAL`
   - `top10_pct > 95%` → `BLOCK_TOP10_CONCENTRATION`

**Test results:**
```
Test 1 (USDT/USDT self-pair): False BLOCK_SELF_PAIR
Test 2 (USDC stablecoin): False BLOCK_STABLECOIN
Test 3 (BTC/BTC self-pair): False BLOCK_SELF_PAIR
Test 4 (missing holder data): False BLOCK_MISSING_HOLDER_DATA
Test 5 (valid BTC): True None
```

---

### Fix 2: Quality Circuit Breaker (Autonomous Safe Mode)
**File:** `scripts/quality_circuit_breaker.py` (NEW)

**Triggers safe mode when:**
- Last 10 executed trades: `reject_rate > 50%` OR `catastrophic_rejects ≥ 2`

**Safe mode actions:**
- Writes `config/safe_mode.flag` with expiry timestamp (JSON)
- Blocks EXECUTE in `stage_4_policy_engine()` (universal gate check)
- Auto-expires after 1 hour cooldown
- Requires synchronous cold path for next 5 trades after expiry

**Current status:**
- **🚨 SAFE MODE ACTIVE** (triggered 2026-02-22 22:44 UTC)
- **Stats:** 10/10 rejects (100%), 10 catastrophic
- **Expires:** 2026-02-22 23:44 UTC

**Cron schedule:** Every 10min (job ID: `qcb-v3-10min`)

---

### Fix 3: Strong Negative Reward for Judge Rejects
**File:** `scripts/learning_loop.py` → `process_closed_position()`

**Judge REJECT ≥85% confidence triggers:**
1. **Thompson Sampling penalty:** `beta += 3.0` (instead of 1.0) → 3x loss penalty
2. **UCB1 source penalty:** `reward = -2.0` (instead of 0.0) → strong negative signal

**Why it matters:**
- **Before:** Judge REJECT with ~0% PnL → bandit sees "meh, tiny loss"
- **After:** Judge REJECT = "invalid trade, major policy failure" → hard penalty
- **Learns from:** Quality failures, not just market losses

**Example:**
```python
# Old behavior: 
if is_win: beta += 0.0
else: beta += 1.0  # Small penalty for 0% PnL loss

# New behavior:
if judge_override:
    beta += 3.0  # STRONG penalty for catastrophic reject
    reward = -2.0  # UCB1 source gets heavy penalty
```

---

### Fix 4: Safe Mode Gate in Policy Engine
**File:** `scripts/fast_decision_engine.py` → `stage_4_policy_engine()`

**Universal gate (runs before all other gates):**
- Checks `config/safe_mode.flag` existence + expiry
- If active: Returns `gate_failed=-1, reason="SAFE_MODE_ACTIVE"`
- Blocks all EXECUTE decisions during safe mode

---

### Fix 5: Cron Integration
**File:** `/data/.openclaw/cron/jobs.json`

**New job added:**
```json
{
  "id": "qcb-v3-10min",
  "name": "Quality Circuit Breaker",
  "schedule": {"kind": "every", "everyMs": 600000},
  "payload": {
    "text": "cd /data/.openclaw/workspace/trading && python3 scripts/quality_circuit_breaker.py >> logs/quality_circuit_breaker_cron.log 2>&1"
  }
}
```

**Runs every 10 minutes** — autonomous monitoring, no human intervention required.

---

## Autonomy Achieved: 3 Closed-Loop Controls

1. **Liveness Loop (Watchdog)**
   - Process health, stuck jobs, memory leaks
   - Auto-restarts, lock clearing, OOM prevention

2. **Quality Loop (Circuit Breaker)** ← NEW
   - Reject rate monitoring, safe mode activation
   - Auto-expires, cooldown, sync cold path requirement

3. **Learning Loop (Bandit + UCB1)**
   - Strategy/source adaptation from PnL + Judge feedback
   - Strong negative rewards for catastrophic trades

---

## What Changed: Before vs After

| Issue | Before | After |
|-------|--------|-------|
| **USDT/USDT execution** | Hot path approved | BLOCK_SELF_PAIR (universal) |
| **Stablecoin trades** | Treated as high-volume alpha | BLOCK_STABLECOIN (all variants) |
| **Missing holder data** | Assumed OK | Fail-closed (BLOCK_MISSING_HOLDER_DATA) |
| **Judge reject pattern** | Continued until manual intervention | Auto-triggers safe mode (1h cooldown) |
| **Learning from rejects** | PnL-only (tiny loss, weak signal) | 3x beta penalty + -2.0 reward (strong signal) |

---

## Production Verification

### Safe Mode Triggered
```
[2026-02-22T22:44:45.640330+00:00] 🚨 SAFE MODE ACTIVATED until 2026-02-22T23:44:45.640330+00:00
[2026-02-22T22:44:45.640467+00:00] Stats: {
  "lookback_count": 10,
  "reject_count": 10,
  "catastrophic_count": 10,
  "reject_rate": 1.0,
  "reject_rate_threshold": 0.5,
  "catastrophic_threshold": 2
}
```

### Safe Mode Active Check
```
[2026-02-22T22:48:51.760469+00:00] ⏳ SAFE MODE active, expires in 56min
[2026-02-22T22:48:51.760506+00:00] Safe mode already active, skipping new check
```

### Force-Closed Positions (Judge REJECT)
```
CC...: pnl=-0.00% Judge=REJECT 99% reason=CATASTROPHIC_REJECT
Cm6fNnMk...: pnl=-0.00% Judge=REJECT 99% reason=CATASTROPHIC_REJECT
Es9vMFrz...: pnl=-0.00% Judge=REJECT 90% reason=CATASTROPHIC_REJECT
AjiZEguX...: pnl=-0.00% Judge=REJECT 99% reason=CATASTROPHIC_REJECT
kMKX8hBa...: pnl=-0.00% Judge=REJECT 99% reason=CATASTROPHIC_REJECT
```

---

## Next Steps

### Immediate (Deployed)
- [x] Universal safety gates (self-pair, stablecoin, holder data)
- [x] Quality circuit breaker script
- [x] Strong negative reward for Judge rejects
- [x] Safe mode gate in policy engine
- [x] Cron job wired (every 10min)

### Future Enhancements
- [ ] Source quarantine: Auto-disable sources with 4/5 rejects in 24h
- [ ] Dynamic threshold tuning based on market regime
- [ ] Strategy weight adaptation from learning loop stats
- [ ] Gate threshold optimization from false positive/negative analysis

---

## Autonomous Evolution Confirmation

> "Make it truly autonomous: the missing closed-loop controls" — Your friend

**✅ ACHIEVED:**
- The system now **self-heals from policy failures** without human intervention
- **Organic creature evolution:** Detects quality degradation → safe mode → learns → adapts
- **No more babysitting:** 100% reject rate triggers automatic 1h pause, not manual shutdown

**This is the autonomy loop closing.**

---

## Files Modified

1. `scripts/fast_decision_engine.py` — Universal gates + safe mode check
2. `scripts/learning_loop.py` — Strong negative reward for Judge rejects
3. `scripts/quality_circuit_breaker.py` — NEW autonomous safe mode script
4. `/data/.openclaw/cron/jobs.json` — Added quality circuit breaker job
5. `config/safe_mode.flag` — Auto-generated during safe mode (JSON)
6. `MEMORY.md` — Logged deployment + status

---

## Evidence of Self-Healing

**Pattern:**
1. Hot path executes 10 trades
2. Judge rejects all 10 at 99% confidence
3. Quality circuit breaker detects 100% reject rate
4. **Auto-triggers safe mode** (blocks further executions)
5. Learning loop applies 3x penalties to strategies/sources
6. Safe mode auto-expires after 1h
7. Next 5 trades require synchronous cold path
8. If quality improves, system resumes normal operation

**This is organic autonomy: detect → pause → learn → adapt → resume.**

No human clicked "stop trading" — the organism did it itself.
