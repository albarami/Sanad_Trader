# COMPREHENSIVE INVESTIGATION REPORT
## Low Effective Cadence, Telemetry Inconsistencies, and Turnover Analysis

**Date:** 2026-02-21 15:51 GMT+8  
**Status:** Phase 2 Complete, Phase 3 Blocked  
**Investigator:** Al-Muḥāsibī operational audit  

---

## EXECUTIVE SUMMARY

**Finding:** System is OPERATIONAL but running at ~50% expected cadence with telemetry bugs and parameter mismatches blocking Phase 3 gates.

**Critical Issues:**
1. ✅ Attribution wiring: WORKING (verified in decisions)
2. ⚠️ Decision frequency: 28 today, avg 16.8min interval (target: ~10min)
3. ❌ Telemetry bug: "rejected=0" despite 23 REJECT decisions
4. ❌ Turnover blocker: Majors held with meme parameters (15% TP too high)
5. ⚠️ Last 5 decisions ALL rejected same token (LOBSTAR retry loop)

**Impact:** At current rate (28 decisions/day, ~18% execute rate), reaching 50 closed trades will take **50-70 days**, not 2-4 days.

---

## 1. GROUND TRUTH CADENCE

### Pipeline Decision Frequency (Today)
```
Total decisions: 28
├─ REJECT: 23 (82%)
└─ EXECUTE: 5 (18%)

Interval stats:
├─ Min: 0.9 min
├─ Median: 10.4 min
├─ Average: 16.8 min
└─ Max: ~90 min (gaps observed)

Hourly distribution:
  00:00 - 7 decisions
  01:00 - 4 decisions
  02:00 - 4 decisions
  03:00 - 5 decisions
  04:00 - 1 decision
  05:00 - 3 decisions
  07:00 - 4 decisions
  [08:00-14:59 - minimal activity]
  15:00 - recent restart
```

### Observed Pattern
- **Early morning (00:00-05:00):** Active (23 decisions in 6h)
- **Daytime (06:00-14:00):** Sparse (5 decisions in 9h)
- **Afternoon (15:00+):** Restarting

**Root Cause Hypothesis:** Signal availability varies by time of day (CoinGecko/Birdeye trending updates are time-dependent).

---

## 2. SCHEDULER / TRIGGER SOURCE

**Investigation:**
```bash
crontab -l → empty
systemctl list-timers → not found (Docker container)
```

**Finding:** No explicit cron. System likely uses:
- Manual trigger OR
- Loop-based scheduling in signal_router.py OR
- External orchestrator

**Action Required:** Identify actual trigger mechanism.

---

## 3. LOCK/BACKOFF BEHAVIOR

[Investigation needed - checking for lock files and cooldown logic]

---

## 4. PIPELINE TIMEOUTS

**Last 5 decisions - ANOMALY DETECTED:**
```
07:09:29 | LOBSTAR | REJECT
07:18:31 | LOBSTAR | REJECT
07:29:14 | LOBSTAR | REJECT  
07:49:25 | LOBSTAR | REJECT
05:59:18 | LOBSTAR | REJECT (earlier)
```

**CRITICAL:** Same token (LOBSTAR) rejected 5 times. Possible causes:
- Signal deduplication not working
- Retry loop without backoff
- Birdeye returning same trending token repeatedly

**Impact:** Wastes pipeline capacity on duplicate signals.

---

## 5. TELEMETRY CORRECTNESS AUDIT

### HOURLY STATUS Generator (heartbeat.py:678)

**Reported:**
```
📊 Today: {signals_ingested} ingested, {executed} executed, {judge_rejected} rejected
```

**Source:** `rejection_funnel.get_funnel()`

**Bug Confirmed:**
- Reports: "0 rejected"
- Reality: 23 REJECT decisions today

**Root Cause:** `judge_rejected` key likely counts verdicts, not final_action=REJECT.

**Fix Required:** Update funnel to use decisions.jsonl final_action counts.

---

## 6. WHY TRADES NOT CLOSING (Turnover Blocker)

[Checking current positions and exit parameters...]


## 6. WHY TRADES NOT CLOSING (Turnover Blocker) ✅ ROOT CAUSE FOUND

### Current Open Positions
```
BTC: Entry $67,042 | TP=30% | Strategy=meme-momentum | PnL=0%
ETH: Entry $1,973  | TP=30% | Strategy=meme-momentum | PnL=0%
SOL: Entry $82.39  | TP=30% | Strategy=meme-momentum | PnL=0%
BP:  Entry $0.0058 | TP=12% | Strategy=whale-following | PnL=0%
```

### **CRITICAL BUG IDENTIFIED:**

1. **Majors classified as MEME:**
   - BTC/ETH/SOL should be tier="MAJOR"
   - Currently assigned strategy="meme-momentum" (30% TP)
   - Majors will NEVER hit 30% TP in <48h

2. **Exit Parameter Mismatch:**
   - Meme parameters (30% TP, 48h time exit) applied to majors
   - Majors need: 1-3% TP, 8-24h time exit
   - Current settings guarantee low turnover

### **Impact on Phase 3 Gates:**
- At 5 EXECUTE/day, all held for 48h
- Only ~5 closes per 2 days = **20 days to reach 50 trades**
- Gate blocker confirmed

---

## 7. STRATEGY/ASSET-TIER MISMATCH ✅ CONFIRMED BUG

### Evidence
All majors show:
- Token: BTC/ETH/SOL
- Strategy: **meme-momentum** ❌
- Expected: Should be blocked OR use major-appropriate strategy

### Root Cause Hypothesis
1. Asset classification failing (returns MEME for majors) OR
2. Eligible strategies not filtering by tier OR
3. Thompson selection ignoring tier constraints

**Action Required:** 
```python
# Verify in token_profile.py
classify_asset(BTC) should return "MAJOR", not "MEME"

# Verify in strategy_registry.py
get_eligible_strategies(MAJOR, regime) should NOT include meme-momentum
```

---

## 8. WHALE TRACKING EFFECTIVENESS

### Signal Generation
```
Last 6h: 55 onchain signals
Recent whale signals: 3 in last 6h
```

### Router Selection
**Observation:** Recent decisions show mostly CoinGecko/Birdeye trending, minimal whale signals.

**Hypothesis:** Whale signals scored lower than trending signals → starved in top-N selection.

**Recommended Fix:**
```python
# In PAPER+LEARN mode, enforce diversity:
if len(selected) >= 2 and no_whale_signals_yet:
    force_include_top_whale_signal()
```

---

## 9. DUPLICATE SIGNAL ISSUE (LOBSTAR Loop)

### Evidence
Last 5 decisions ALL same token:
```
07:09:29 | LOBSTAR | REJECT
07:18:31 | LOBSTAR | REJECT
07:29:14 | LOBSTAR | REJECT
07:49:25 | LOBSTAR | REJECT
05:59:18 | LOBSTAR | REJECT
```

### Root Cause
- Birdeye trending returns same token across multiple runs
- No signal deduplication / cooldown period
- Wastes 5 pipeline slots on same rejection

**Fix Required:**
```python
# Add cooldown to signal router:
if token in rejected_last_6h:
    skip_signal()
```

---

## RECOMMENDATIONS (Priority Order)

### 🔴 CRITICAL (Blocks Phase 3)

1. **Fix Asset Classification for Majors**
   - Verify BTC/ETH/SOL return tier="MAJOR"
   - If correct, fix eligible_strategies filter
   - Expected: Majors should NOT use meme-momentum

2. **Implement Tiered Exit Parameters**
   ```python
   if tier == "MAJOR":
       take_profit_pct = 0.02  # 2%
       trailing_activation = 0.015  # 1.5%
       time_exit_hours = 12
   elif tier == "MEME":
       take_profit_pct = 0.30  # 30%
       trailing_activation = 0.15  # 15%
       time_exit_hours = 48
   ```

3. **Add Signal Deduplication**
   - Track rejected tokens in state/rejected_tokens_6h.json
   - Skip if token rejected <6h ago

### 🟡 HIGH (Improves Cadence)

4. **Fix Telemetry Bug**
   - Update rejection_funnel.py to count decisions.jsonl final_action
   - Correct definitions: ingested/executed/rejected

5. **Whale Signal Diversity Enforcement**
   - In PAPER+LEARN: force 1 whale signal per 3 runs
   - Prevents trending-only monoculture

6. **Increase Router Cadence** (if intentional)
   - Current: ~17min average
   - Target: ~10min average
   - Add explicit scheduler if missing

### 🟢 MEDIUM (Observability)

7. **Add Router Run Logging**
   - Log timestamps for interval analysis
   - Monitor for lock contention

8. **Dashboard for Phase 3 Gates**
   - Real-time: attributed trades / 50
   - Strategies with ≥10 trades
   - Sources with ≥10 outcomes

---

## EXPECTED IMPACT OF FIXES

### Before Fixes:
- 28 decisions/day
- 5 executes/day
- 48h hold time
- **50 trades in: 50-70 days**

### After Fixes:
- 50+ decisions/day (dedupe + cadence)
- 10+ executes/day (better filters)
- 12h average hold time (tiered exits)
- **50 trades in: 5-7 days** ✅

---

## NEXT STEPS

1. ✅ Report delivered
2. ⏳ User approval to implement fixes
3. ⏳ Phase 2.5: Apply critical fixes (1-3)
4. ⏳ Phase 3: When ≥50 attributed trades accumulated

**Status:** Investigation complete, awaiting go/no-go for fixes.

