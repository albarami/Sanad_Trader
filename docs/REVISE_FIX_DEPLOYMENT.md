# Judge REVISE Fix Deployment
## Feb 20, 2026 14:00 GMT+8 — Commit 2e2f561

### STATUS: ✅ DEPLOYED (Pending Verification)

---

## Problem Statement

**Rejection Funnel (Today):**
```
35 signals ingested
├── 15 reached Judge
│   ├── 2 APPROVED ✅ (executed)
│   ├── 13 REVISE ⚠️ (87% rejection rate - BOTTLENECK)
│   └── 1 REJECTED
└── 19 policy-blocked
```

**Root Cause:**
- Judge returns REVISE for 87% of signals (13/15)
- REVISE means "has merit but needs refinement"
- Policy Engine Gate 15 blocks ALL REVISE trades
- Result: Wasting learning opportunities on marginal signals

---

## Solution Deployed

### **Option A: Treat REVISE as APPROVE in Paper Mode**

**Rationale:**
- Paper mode priority = learning data, not perfection
- REVISE signals passed 14 safety gates (Sanad, policy, etc.)
- Small position size = low risk, high learning value
- Live mode still requires APPROVE (no change)

---

## Changes Made

### 1. Policy Engine (`policy_engine.py` - Gate 15)

**Before:**
```python
if audit_verdict == "REVISE":
    return False, "Al-Muhasbi verdict: REVISE — logged for review, not executable"
```

**After:**
```python
if audit_verdict == "REVISE":
    # Paper mode: Treat REVISE as APPROVE for learning
    if config.get("mode", "paper").lower() == "paper":
        return True, "PAPER PROBE: Al-Muhasbi REVISE treated as APPROVE (learning mode, will micro-size)"
    else:
        # Live mode: REVISE still non-executable
        return False, "Al-Muhasbi verdict: REVISE — logged for review, not executable in live mode"
```

**Impact:**
- Paper mode: REVISE → PASS (executes with micro-sizing)
- Live mode: REVISE → BLOCK (unchanged, requires human review)

---

### 2. Micro-Sizing (`sanad_pipeline.py` - Stage 7)

**Added logic:**
```python
# Check if this is a REVISE probe
verdict = judge_result.get("verdict", "REJECT")
is_revise_probe = (verdict == "REVISE" and mode == "paper")

# Apply micro-sizing for REVISE probes
if is_revise_probe:
    PAPER_REVISE_PROBE_USD = 25  # Cap at $25 (vs $200 standard)
    position_usd = min(base_position_usd, PAPER_REVISE_PROBE_USD)
    execution_mode = "paper_probe_revise"
else:
    position_usd = base_position_usd
    execution_mode = "paper_standard"
```

**Sizing comparison:**
- **APPROVE trades:** $100-200 (regime-adjusted)
- **REVISE probes:** $25 (fixed micro-size)
- **Risk reduction:** 87.5% smaller position for marginal signals

---

### 3. Position Tracking (`sanad_pipeline.py` - _add_position)

**Added fields:**
```python
new_position = {
    ...
    "execution_mode": execution_mode,  # "paper_probe_revise" or "paper_standard"
    "side": signal.get("direction", "LONG").upper(),  # Support SHORT
    ...
}
```

**Benefits:**
- Track REVISE probes separately in analytics
- Measure: REVISE win rate vs APPROVE win rate
- Isolate probe performance in UCB1 learning

---

## Expected Impact

### Before Fix:
- 35 signals → 2 executed (5.7% conversion)
- 13 REVISE signals wasted (87% rejection at Judge)
- ~2-3 executions per day

### After Fix:
- 35 signals → 15 executed (42.9% conversion)
- 13 REVISE probes at $25 each = $325 total exposure
- ~10-15 executions per day (5-10x increase)

### Risk Profile:
- **Standard trades:** $100-200 × 2 = $200-400 exposure
- **REVISE probes:** $25 × 13 = $325 exposure
- **Total:** $525-725 (vs $10,000 equity = 5-7% exposure)

---

## Verification Checklist

### ⏳ Phase 1: Gate 15 Behavior (Next router run)

Wait for next signal to reach Judge with REVISE verdict:

```bash
cd /data/.openclaw/workspace/trading && \
tail -f logs/signal_router.log | grep -E "REVISE|PAPER PROBE|Gate 15"
```

**Expected:**
- Policy Engine outputs: `"PAPER PROBE: Al-Muhasbi REVISE treated as APPROVE"`
- Pipeline result: `APPROVE` (not `REJECT`)
- Signal proceeds to execution

---

### ⏳ Phase 2: Micro-Sizing (Check execution logs)

```bash
cd /data/.openclaw/workspace/trading && \
tail -100 logs/signal_router.log | grep -E "REVISE PROBE|Micro-sizing|execution_mode"
```

**Expected:**
- Log message: `"REVISE PROBE: Micro-sizing $200 → $25 (learning mode)"`
- Position size: $25 (not $100-200)

---

### ⏳ Phase 3: Position Tracking

```bash
cd /data/.openclaw/workspace/trading && \
python3 -c "
import json
with open('state/positions.json') as f:
    pos = json.load(f).get('positions', [])
    probes = [p for p in pos if p.get('execution_mode') == 'paper_probe_revise']
    print(f'{len(probes)} REVISE probe positions')
    for p in probes:
        print(f'  {p[\"token\"]}: \${p.get(\"position_usd\", 0):.2f} ({p.get(\"strategy_name\", \"?\")})')
"
```

**Expected:**
- execution_mode field present
- position_usd = $25 for probes
- Separate count from standard trades

---

### ⏳ Phase 4: Rejection Funnel Change (After 24h)

```bash
cd /data/.openclaw/workspace/trading && cat state/rejection_funnel.json
```

**Expected before:**
```json
{
  "judge_revised": 13,
  "judge_approved": 2,
  "executed": 2
}
```

**Expected after:**
```json
{
  "judge_revised": 13,
  "judge_approved": 2,
  "executed": 15  // ← All REVISE now execute
}
```

---

## Monitoring Commands

### Real-time REVISE probe tracking:
```bash
cd /data/.openclaw/workspace/trading && \
watch -n 30 "
echo '=== REJECTION FUNNEL ===' && \
python3 -c \"
import json
with open('state/rejection_funnel.json') as f:
    funnel = json.load(f)
    print(f'Judge APPROVED: {funnel.get(\"judge_approved\", 0)}')
    print(f'Judge REVISE: {funnel.get(\"judge_revised\", 0)}')
    print(f'Executed: {funnel.get(\"executed\", 0)}')
    approval_rate = funnel.get('executed', 0) / max(funnel.get('judge_approved', 1) + funnel.get('judge_revised', 1), 1)
    print(f'Conversion: {approval_rate*100:.1f}%')
\" && \
echo '' && \
echo '=== PROBE POSITIONS ===' && \
python3 -c \"
import json
with open('state/positions.json') as f:
    pos = json.load(f).get('positions', [])
    probes = [p for p in pos if p.get('execution_mode') == 'paper_probe_revise']
    standard = [p for p in pos if p.get('execution_mode') == 'paper_standard']
    print(f'Standard trades: {len([p for p in standard if p.get(\"status\")==\"OPEN\"])} open, {len([p for p in standard if p.get(\"status\")==\"CLOSED\"])} closed')
    print(f'REVISE probes: {len([p for p in probes if p.get(\"status\")==\"OPEN\")])} open, {len([p for p in probes if p.get(\"status\")==\"CLOSED\")])} closed')
\"
"
```

### Check last 5 REVISE executions:
```bash
cd /data/.openclaw/workspace/trading && \
python3 -c "
import json
with open('state/positions.json') as f:
    pos = json.load(f).get('positions', [])
    probes = [p for p in pos if p.get('execution_mode') == 'paper_probe_revise']
    print(f'Last 5 REVISE probes:')
    for p in probes[-5:]:
        status = p.get('status', '?')
        pnl = p.get('pnl_pct', 0) * 100 if status == 'CLOSED' else 0
        size = p.get('position_usd', 0)
        print(f'  {p[\"token\"]}: \${size:.2f}, {status}, P&L: {pnl:+.1f}%')
"
```

---

## Success Criteria (24h)

- [ ] Gate 15 passes REVISE verdicts (paper mode)
- [ ] REVISE trades execute at $25 size (not $100-200)
- [ ] execution_mode="paper_probe_revise" tracked in positions
- [ ] Rejection funnel shows executed ≈ judge_approved + judge_revised
- [ ] No crashes on REVISE execution
- [ ] Standard APPROVE trades still execute normally ($100-200)

**After 24h:** Compare:
- REVISE probe win rate vs APPROVE win rate
- REVISE probe execution count (should be ~10-15/day)
- Portfolio exposure (should be 5-10% including probes)

---

## Rollback Plan (If Needed)

If REVISE probes cause issues:

```bash
cd /data/.openclaw/workspace/trading && git revert 2e2f561
```

Or selective disable by editing `policy_engine.py`:
```python
# Change line 641-645 back to:
if audit_verdict == "REVISE":
    return False, "Al-Muhasbi verdict: REVISE — logged for review, not executable"
```

---

## Related Changes

This deployment works in conjunction with:
- **SHORT strategies** (commit 917a831) - Enables trading both directions
- **Confidence=0 bug** (still pending fix) - Some signals still blocked by confidence threshold

---

## Next Steps

1. **Monitor next 3 router cycles** (30 minutes)
   - Verify REVISE → APPROVE conversion
   - Check micro-sizing applied correctly

2. **After 24h:** Analyze probe performance
   - Compare: REVISE probe win rate vs APPROVE win rate
   - Decide: Keep $25 size or adjust to $50-$75

3. **After 1 week:** Tune based on data
   - If REVISE probes profitable: Increase size
   - If REVISE probes unprofitable: Keep at $25 or disable
   - Update Judge prompt to reduce REVISE rate if still high

---

## Philosophy

**Paper mode is for learning, not perfection.**

- APPROVE trades = high conviction ($100-200)
- REVISE probes = marginal signals ($25)
- Combined = rich dataset for UCB1/Thompson Sampling
- Live mode unchanged = requires APPROVE only

**The goal:** Build a learning machine, not a perfect trader.
