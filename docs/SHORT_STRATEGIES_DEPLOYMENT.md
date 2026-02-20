# SHORT Strategies Deployment
## Feb 20, 2026 — Commit 917a831

### STATUS: ✅ DEPLOYED (Pending Verification)

---

## What Was Changed

### 1. Strategy Registry (`strategy_registry.py`)
Added 3 new SHORT strategies:

| Strategy | Direction | Entry Conditions | Exit | Description |
|----------|-----------|------------------|------|-------------|
| **whale-distribution-fade** | SHORT | 2+ whales selling, <5% 1h move | SL=5%, TP=10%, 48h | Fade tokens when tracked whales distribute |
| **bear-momentum** | SHORT | -5% to -30% drop, Fear<25 | SL=4%, TP=8%, 24h | Short weakness in bear regime |
| **mean-reversion-short** | SHORT | RSI>70, above BB upper, Fear<40 | SL=3%, TP=5%, 24h | Fade overbought bounces in bear |

**Total active strategies:** 12 (9 LONG + 3 SHORT)

---

### 2. Position Monitor (`position_monitor.py`)
Added SHORT P&L calculations (inverse of LONG):

#### Stop Loss:
- **LONG:** Exit when price drops BELOW `entry * (1 - stop_pct)`
- **SHORT:** Exit when price rises ABOVE `entry * (1 + stop_pct)`

#### Take Profit:
- **LONG:** Exit when price rises ABOVE `entry * (1 + tp_pct)`
- **SHORT:** Exit when price drops BELOW `entry * (1 - tp_pct)`

#### Trailing Stop:
- **LONG:** Track high-water mark (HWM), exit if price drops from HWM
- **SHORT:** Track low-water mark (LWM), exit if price rises from LWM

#### P&L Calculation:
```python
if side == "SHORT":
    pnl_pct = (entry - current_price) / entry  # Profit when price drops
    pnl_usd = (entry - current_price) * qty
else:
    pnl_pct = (current_price - entry) / entry  # Profit when price rises
    pnl_usd = (current_price - entry) * qty
```

---

### 3. Whale Tracker (`whale_tracker.py`)
Now generates SHORT signals from distribution alerts:

**Before:** Distribution alerts were write-only (alerts.json)  
**After:** Distribution alerts → SHORT signals to `signals/onchain/`

**Signal Generation Logic:**
- When 2+ tracked whales sell same token within 6h window
- Confidence: 50 + (whale_count * 10) → 60-80 confidence
- Strategy hint: `whale-distribution-fade`
- Direction: **SHORT**

**Example Signal:**
```json
{
  "token": "So111111",
  "source": "whale_distribution",
  "chain": "solana",
  "direction": "SHORT",
  "signal_strength": 70,
  "thesis": "4 tracked whales distributing (JAMAL, CUPSEY2...). Fade strength on distribution.",
  "strategy_hint": "whale-distribution-fade",
  "distribution_whale_count": 4,
  "confidence": 70
}
```

---

## Verification Checklist

### ✅ Phase 1: Strategy Registration (DONE)
- [x] 3 SHORT strategies added to registry
- [x] `python3 scripts/strategy_registry.py` runs without errors
- [x] Output shows 12 strategies (9 LONG + 3 SHORT)

### ⏳ Phase 2: Signal Generation (PENDING)
Wait for next whale_tracker run (every 5 minutes):
```bash
cd /data/.openclaw/workspace/trading && \
tail -f logs/signal_router.log | grep -E "SHORT|whale_distribution|distribution-fade"
```

**Expected:**
- Whale tracker generates SHORT signals from current distribution alerts
- Signal files appear in `signals/onchain/whale_tracker_*.json` with `"direction": "SHORT"`

### ⏳ Phase 3: Router Acceptance (PENDING)
Wait for router to pick up SHORT signal:
```bash
cd /data/.openclaw/workspace/trading && \
tail -100 logs/signal_router.log | grep -E "Selected.*SHORT|direction.*SHORT|whale-distribution-fade"
```

**Expected:**
- Router selects SHORT signals for pipeline (not filtered out)
- Strategy matching succeeds (finds `whale-distribution-fade`)

### ⏳ Phase 4: Pipeline Processing (PENDING)
Wait for pipeline to process SHORT signal:
```bash
cd /data/.openclaw/workspace/trading && \
tail -200 logs/signal_router.log | grep -B5 -A5 "SHORT"
```

**Expected:**
- Pipeline accepts SHORT direction
- Bull/Bear debate handles SHORT thesis
- Judge evaluates SHORT trade plan

### ⏳ Phase 5: Execution (PENDING)
If approved, check for SHORT position:
```bash
cd /data/.openclaw/workspace/trading && \
python3 -c "
import json
with open('state/positions.json') as f:
    pos = json.load(f).get('positions', [])
    shorts = [p for p in pos if p.get('side') == 'SHORT' and p.get('status') == 'OPEN']
    print(f'{len(shorts)} SHORT positions:')
    for p in shorts:
        print(f'  {p[\"token\"]}: Entry \${p[\"entry_price\"]} ({p.get(\"strategy_name\", \"?\")})')
"
```

**Expected:**
- At least 1 SHORT position opened
- side="SHORT" field present
- P&L calculation correct (profit when price drops)

### ⏳ Phase 6: Exit Handling (PENDING)
Wait for SHORT position to hit exit condition:
```bash
cd /data/.openclaw/workspace/trading && \
python3 -c "
import json
with open('state/positions.json') as f:
    pos = json.load(f).get('positions', [])
    closed_shorts = [p for p in pos if p.get('side') == 'SHORT' and p.get('status') == 'CLOSED']
    print(f'{len(closed_shorts)} closed SHORT positions:')
    for p in closed_shorts[-3:]:
        print(f'  {p[\"token\"]}: {p.get(\"exit_reason\", \"?\")} | P&L: {p.get(\"pnl_pct\", 0)*100:+.1f}%')
"
```

**Expected:**
- Stop loss triggers when price goes UP (inverse of LONG)
- Take profit triggers when price goes DOWN (inverse of LONG)
- Trailing stop tracks LOW-water mark (not high)
- P&L is positive when exit_price < entry_price

---

## Known Risks & Limitations

### 1. No Live Exchange SHORT Support
- Paper mode: SHORT positions are simulated (P&L calculated correctly)
- Live mode: Would need exchange-specific SHORT/margin APIs
- **Action:** Keep paper-only until SHORT execution wired for live

### 2. Whale Tracker Signal Quality
- Distribution signals based on 6h window
- May fire false positives during whale rotation (sell A, buy B)
- Min 2 whales selling same token helps reduce noise

### 3. Judge Rejection Risk
- Judge may reject SHORT trades as "unproven strategy"
- Monitor REVISE rate specifically for SHORT signals
- May need Judge prompt update to accept SHORT direction

### 4. Data Enrichment
- SHORT signals need same price/volume/liquidity data as LONG
- Router enrichment should be direction-agnostic
- Verify tradeability scoring doesn't penalize SHORT

---

## Monitoring Commands

### Real-time SHORT signal flow:
```bash
cd /data/.openclaw/workspace/trading && \
watch -n 30 "
echo '=== SHORT SIGNALS GENERATED ===' && \
find signals -name '*.json' -mmin -60 -exec grep -l '\"direction\": \"SHORT\"' {} \; | wc -l && \
echo '' && \
echo '=== SHORT POSITIONS ===' && \
python3 -c \"
import json
with open('state/positions.json') as f:
    pos = json.load(f).get('positions', [])
    shorts = [p for p in pos if p.get('side') == 'SHORT']
    print(f'Open: {len([p for p in shorts if p.get(\"status\")==\"OPEN\"])}')
    print(f'Closed: {len([p for p in shorts if p.get(\"status\")==\"CLOSED\"])}')
\" && \
echo '' && \
echo '=== REJECTION FUNNEL ===' && \
python3 -c \"
import json
with open('state/rejection_funnel.json') as f:
    funnel = json.load(f)
    print(f'Executed: {funnel.get(\"executed\", 0)}')
    print(f'Judge REVISE: {funnel.get(\"judge_revised\", 0)}')
\"
"
```

### Check last 5 SHORT signals generated:
```bash
cd /data/.openclaw/workspace/trading && \
find signals -name "*.json" -mmin -60 -exec sh -c '
    grep -q "\"direction\": \"SHORT\"" "$1" && echo "$1"
' _ {} \; | tail -5 | while read f; do
    echo "=== $f ===" && cat "$f" | python3 -m json.tool | head -20 && echo ""
done
```

---

## Rollback Plan (If Needed)

If SHORT strategies cause issues:

```bash
cd /data/.openclaw/workspace/trading && git revert 917a831
```

Or selective disable:
```python
# In scripts/strategy_registry.py, set active=False:
"whale-distribution-fade": {
    ...
    "active": False,  # ← Disable
},
```

---

## Success Criteria (24h Test)

- [ ] Whale tracker generates 5+ SHORT signals from distribution alerts
- [ ] Router accepts SHORT signals (not filtered)
- [ ] Pipeline processes SHORT signals (no crashes)
- [ ] At least 1 SHORT position opened (if signals approved)
- [ ] SHORT P&L calculated correctly (profit when price drops)
- [ ] Exit logic works (stop-loss triggers on UP move)
- [ ] No regression on LONG strategies (still executing normally)
- [ ] Rejection funnel shows SHORT signals reaching Judge

**After 24h:** Review rejection_funnel.json and compare:
- SHORT approval rate vs LONG approval rate
- SHORT execution crashes vs LONG execution crashes
- Judge REVISE reasons for SHORT vs LONG

---

## Next Steps After Verification

1. **If SHORT signals never generated:**
   - Check whale_distribution_alerts.json has 2+ whales
   - Verify whale_tracker.py actually ran (check cron logs)

2. **If SHORT signals rejected at router:**
   - Check signal_router.py for hardcoded LONG filters
   - Verify strategy matching accepts direction="SHORT"

3. **If SHORT approved but execution crashes:**
   - Check sanad_pipeline.py Stage 7 for side assumptions
   - Verify paper trade executor handles SHORT

4. **If Judge rejects all SHORT:**
   - Tune Judge prompt to accept SHORT direction
   - Or treat REVISE → APPROVE for SHORT in paper mode

---

## Related Issues

This deployment addresses:
- **Issue #1:** Whale distribution alerts were wasted (no trading action)
- **Issue #2:** System is LONG-only (can't profit in EXTREME_FEAR=7 regime)
- **Issue #3:** No hedging capability (all positions correlated)

**Remaining work:**
- Judge REVISE → APPROVE tuning (separate issue, see ACTION_PLAN.md)
- Confidence=0 bug fix (separate issue)
- Live exchange SHORT execution (Sprint 11+)
