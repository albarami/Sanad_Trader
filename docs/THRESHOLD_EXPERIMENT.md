# Threshold Experiment — Feb 21, 2026

## Goal
Test system learning with borderline-quality tokens that were being blocked.

## Problem Identified
- 406 signals/hour from 58 unique tokens
- Only 0 APPROVE, 8 REJECT in last 2 hours
- Most rejections: Sanad trust 18-72, various rugpull flags
- Prefilter blocking many DexScreener signals (liquidity $0, age 0h, holders 0)

## Hypothesis
Lower thresholds temporarily will:
1. Allow borderline tokens (trust 35-50) to reach pipeline
2. Generate learning data for UCB1 and Genius Memory
3. Test if Judge/Bull/Bear debate catches quality issues
4. Prove whether we're being too conservative

## Changes Made (Commit 65ca7cd)

### 1. Sanad Trust Threshold
- **Before:** min_trust_score: 50
- **After:** min_trust_score: 35
- **Impact:** Allows LOBSTAR (trust=42), GROKIUS (trust=42), TRUMP (trust=42-62)

### 2. Sanad Score Threshold  
- **Before:** min_sanad_score: 55
- **After:** min_sanad_score: 40
- **Impact:** Lowers bar for signal admission

### 3. DexScreener Prefilter
- **Liquidity:** $200K → $100K
- **Age:** 24h → 6h
- **Holders:** 1000 → 500
- **RugCheck:** 50 → 40
- **Impact:** PUNCH, ALIENS, GDIG, HOUSE, EPSTEIN may now pass prefilter

## Borderline Candidates Now Eligible

1. **LOBSTAR** (trust=42)
   - ⚠️ extreme_infancy
   - ⚠️ honeypot_warning_round_trip_loss_19pct
   
2. **GROKIUS** (trust=42)
   - ⚠️ extreme_infancy
   - ⚠️ thin_liquidity_danger

3. **TRUMP** (trust=42-62)
   - ⚠️ concentrated_holders_top10_91_percent
   - ⚠️ honeypot_caution_19_percent_loss

4. **CHILLGUY** (trust=62)
   - ⚠️ honeypot_detected
   - ⚠️ holder_concentration_concern

5. **MUSHU** (trust=72)
   - ⚠️ thin_liquidity_vs_volume
   - ⚠️ concentrated_top10_holders

## Safety Rails Still Active

✅ Judge still reviews all signals  
✅ Bull/Bear debate still happens  
✅ Policy Engine gates still enforced  
✅ Position limits unchanged (max 5 concurrent)  
✅ Stop-loss/take-profit rules unchanged  
✅ Paper mode only (no real money risk)

## Expected Outcomes

**Good Case:**
- 2-4 trades execute on borderline tokens
- Judge approves with lower confidence (30-50%)
- Some small wins/losses generate learning data
- UCB1 updates source grades
- System learns which "borderline" signals are actually good

**Bad Case:**
- Judge still rejects most (too many rugpull flags)
- Any approved trades hit stop-loss quickly
- System learns these sources/patterns are unreliable
- We restore original thresholds

## Monitoring Plan

Watch next 2-6 hours for:
1. **Increased prefilter pass rate** (fewer "DexScreener boost failed")
2. **Sanad CAUTION instead of BLOCK** (trust 35-50)
3. **Judge verdicts** (APPROVE/REVISE/REJECT ratios)
4. **Execution outcomes** (if any trades approved)
5. **Cost tracking** (should stay under $65/day limit)

## Rollback Trigger

Restore original thresholds if:
- Daily cost approaches $60 (leaving $5 buffer)
- >10 executions in 6 hours (spam indicator)
- All experimental trades hit stop-loss within 2 hours
- Judge approval rate >50% (too permissive)

## Success Metrics

**Experiment succeeds if:**
- 1-5 trades execute (proves system not too conservative)
- Mix of APPROVE/REVISE/REJECT (Judge still filtering)
- At least 1 trade positive PnL (borderline CAN work)
- Learning loop captures data (genius memory updated)

**Experiment fails if:**
- Zero trades execute (Judge still too strict)
- All trades negative PnL <-5% (quality too low)
- Cost spike >$30 in 6 hours (inefficient)

## Restoration Plan

To restore original thresholds:

```bash
# Revert changes
git revert 65ca7cd

# Or manually restore:
# config/thresholds.yaml:
#   scoring.min_trust_score: 50
#   signals.min_sanad_score: 55
#   signals.paper_mode_min_sanad_score: 55
# scripts/signal_router.py:
#   liquidity < 200000
#   token_age_h < 24
#   holder_count < 1000
#   rugcheck_score < 50

git add config/thresholds.yaml scripts/signal_router.py
git commit -m "RESTORE: Original thresholds after experiment"
git push origin main
```

## Timeline

- **Start:** 2026-02-21 01:30 UTC+8
- **Monitor:** Next 2-6 hours
- **Review:** 2026-02-21 07:30 UTC+8 (6 hours)
- **Decide:** Keep or restore based on metrics

---

**Status:** 🧪 EXPERIMENT ACTIVE  
**Next Check:** 2026-02-21 03:30 UTC+8 (2 hours)
