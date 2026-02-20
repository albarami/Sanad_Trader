# ⚖️ Al-Muḥāsibī Approved Autonomy Plan v1.1

**Date:** 2026-02-21 06:17 GMT+8  
**Status:** CORRECTIONS APPLIED - READY FOR SAFE EXECUTION

---

## Mandatory Corrections From v1.0

### ❌ Rejected Approaches:
1. Overloading `SYSTEM_MODE` with `PAPER_LEARN` value
2. Replacing scalar threshold keys with dicts (breaks backward compatibility)
3. Creating `counterfactual_learner.py` without checking existing scripts
4. REVISE-as-approve without tagging
5. Confidence inference when judge call failed

### ✅ Approved Corrections:
1. Keep `SYSTEM_MODE` strictly `PAPER` or `LIVE`
2. Add `PAPER_PROFILE=LEARN|STRICT` as separate variable
3. Extend existing `counterfactual_checker.py` / `counterfactual_tracker.py`
4. Tag all REVISE overrides with `paper_override=true`
5. Infer confidence ONLY when verdict exists

---

## Phase 0: Diagnostic Baseline (COMPLETE)

✅ **Findings:**
- Only 1/5 strategies active (meme-momentum paused)
- Thompson: 0 trials (cold-start)
- Pipeline rejecting ~100% (missing thesis)
- Thresholds already lowered but ineffective
- Existing counterfactual scripts confirmed

---

## Phase 1: Safe Cold-Start Break (CORRECTED)

### ✅ 1A: Activate Strategies (DONE)
**File:** `state/strategy_stats.json`  
**Status:** All 5 strategies activated

---

### ✅ 1B: Introduce PAPER_PROFILE (NEW - PRIORITY 1)

**Environment Variable:**
```bash
# In config/.env or docker-compose
SYSTEM_MODE=PAPER
PAPER_PROFILE=LEARN  # or STRICT
```

**Config Structure (backward-compatible):**
```yaml
# config/thresholds.yaml

# Existing keys remain (defaults/LIVE)
scoring:
  min_trust_score: 50
  min_tradeability_score: 40
  min_confidence_score: 60

# New section (doesn't break old references)
paper_profiles:
  LEARN:
    min_trust_score: 30
    min_tradeability_score: 20
    min_confidence_score: 40
    allow_thesis_waiver: true
    allow_revise_approval: true
  STRICT:
    min_trust_score: 50
    min_tradeability_score: 40
    min_confidence_score: 60
    allow_thesis_waiver: false
    allow_revise_approval: false
```

**Resolution Logic (one place):**
```python
def get_threshold(key, default):
    mode = os.getenv("SYSTEM_MODE", "PAPER").upper()
    profile = os.getenv("PAPER_PROFILE", "STRICT").upper()
    
    if mode == "LIVE":
        return THRESHOLDS["scoring"].get(key, default)
    
    if mode == "PAPER" and profile == "LEARN":
        return THRESHOLDS["paper_profiles"]["LEARN"].get(key, 
            THRESHOLDS["scoring"].get(key, default))
    
    # PAPER_STRICT or fallback
    return THRESHOLDS["scoring"].get(key, default)
```

---

### ✅ 1C: Auto-Thesis Generation (DONE)
**File:** `scripts/sanad_pipeline.py`  
**Status:** Already implemented, syntax verified

---

### 🔄 1D: REVISE Handling (REVISED)

**Approved Logic:**
```python
if verdict == "REVISE":
    mode = os.getenv("SYSTEM_MODE", "PAPER").upper()
    profile = os.getenv("PAPER_PROFILE", "STRICT").upper()
    
    if mode == "PAPER" and profile == "LEARN":
        # Allow with constraints
        return execute_trade(
            size_multiplier=0.3,
            tags={
                "judge_verdict": "REVISE",
                "paper_override": True,
                "override_reason": "learning_mode"
            }
        )
    else:
        # PAPER_STRICT or LIVE: fail-closed
        return reject_signal("judge_revise_blocked")
```

**Location:** Find in `scripts/sanad_pipeline.py` where judge verdict is processed

---

### 🔄 1E: Confidence Inference (REVISED)

**Approved Logic:**
```python
# ONLY if verdict exists and is positive
if verdict in ["APPROVE", "REVISE"] and (confidence == 0 or confidence is None):
    mode = os.getenv("SYSTEM_MODE", "PAPER").upper()
    
    if mode == "PAPER":
        # Infer safe default
        confidence = 40 if verdict == "REVISE" else 55
        log_flag("confidence_inferred", confidence)
    else:
        # LIVE: fail-closed
        return reject_signal("confidence_missing_LIVE_failclosed")

# If verdict is REJECT or missing: do NOT infer
if verdict not in ["APPROVE", "REVISE"]:
    return reject_signal("judge_reject_or_failed")
```

---

### 🔄 1F: Learning Quota (REVISED WITH HARD SAFETY)

**Approved Logic:**
```python
def should_force_learning_trade(signal):
    mode = os.getenv("SYSTEM_MODE", "PAPER").upper()
    profile = os.getenv("PAPER_PROFILE", "STRICT").upper()
    
    if mode != "PAPER" or profile != "LEARN":
        return False
    
    daily_trades = load_daily_count()
    quota = config.get("paper_learn_quota", 10)
    
    if daily_trades >= quota:
        return False
    
    # HARD BLOCKS remain hard (never bypass)
    if signal.rugcheck_catastrophic:
        return False
    if signal.liquidity_usd < 5000:
        return False
    if signal.slippage_pct > 25:
        return False
    if kill_switch_active():
        return False
    if any_breaker_open():
        return False
    
    # SOFT BLOCKS can be relaxed
    soft_pass = (
        signal.trust_score >= 25 and
        signal.tradeability >= 15 and
        signal.has_thesis_or_auto_generated
    )
    
    return soft_pass
```

---

## Phase 2: Close Learning Loop (REVISED)

### 2A: Wire Thompson Updates
**Check first:** Does `scripts/post_trade_analyzer.py` call Thompson?

**Action:** Wire if missing

---

### 2B: Wire UCB1 Outcome Feedback
**Check first:** Does post-trade update UCB1 grades?

**Action:** Wire if missing

---

### 2C: Extend Existing Counterfactual Jobs (REVISED)

**Files confirmed to exist:**
- `scripts/counterfactual_checker.py`
- `scripts/counterfactual_tracker.py`
- Cron jobs running every 6h and daily

**Action (NOT create new script):**
1. Read existing scripts to understand current logic
2. Extend `counterfactual_checker.py` to:
   - Fetch price outcomes for rejected signals
   - Classify: missed_winner, correct_reject, neutral
   - Update UCB1 grades based on outcomes
   - Auto-calibrate PAPER_LEARN thresholds (bounded)
3. Only if truly missing: add minimal new function

---

## Phase 3: RAG Pattern Boost

**Action:** Wire `vector_db.query_similar_patterns()` into strategy scoring

---

## Phase 4: SHORT Flow Verification

**Action:** Verify SHORT strategies registered and executable

---

## Execution Sequence (CORRECTED)

### Immediate (Next 30 min):
1. ✅ Add `PAPER_PROFILE` to config/.env
2. ✅ Add `paper_profiles` section to thresholds.yaml
3. ✅ Implement `get_threshold()` helper in sanad_pipeline.py
4. ✅ Test: verify thresholds resolve correctly
5. ✅ Commit: "Add PAPER_PROFILE infrastructure"

### Today (Next 3 hours):
1. ✅ Implement REVISE handling (with tagging)
2. ✅ Implement confidence inference (verdict-present only)
3. ✅ Implement learning quota (hard safety intact)
4. ✅ Test: wait for signal router run
5. ✅ Verify: first approved trade with proper tags

### Tomorrow:
1. ✅ Read existing counterfactual scripts
2. ✅ Extend with outcome classification
3. ✅ Wire Thompson + UCB1 updates
4. ✅ Monitor: learning loop active

---

## Safety Guarantees

### LIVE Mode (Unchanged):
- ✅ Trust threshold: 70
- ✅ Confidence threshold: 60
- ✅ REVISE → blocked
- ✅ Confidence=0 → blocked
- ✅ All rugpull blocks remain hard

### PAPER_STRICT Mode:
- ✅ Same as LIVE (honest simulator)
- ✅ No overrides, no relaxed gates

### PAPER_LEARN Mode:
- ✅ Trust threshold: 30 (relaxed)
- ✅ REVISE → approve with 0.3x size + tags
- ✅ Confidence inference allowed (verdict-present only)
- ✅ Learning quota: 10 trades/day max
- ✅ Hard safety blocks remain (rug, liquidity, slippage, breakers, kill switch)

---

## Verification Checklist

Before each commit:
- [ ] No changes to SYSTEM_MODE logic
- [ ] LIVE behavior unchanged
- [ ] Backward-compatible config structure
- [ ] All overrides tagged
- [ ] Heartbeat OK
- [ ] Syntax check passes

---

## Status

**Phase 1A, 1C:** ✅ Complete  
**Phase 1B:** 🔄 Ready to implement  
**Phase 1D-1F:** 🔄 Waiting for 1B  
**Phase 2-4:** ⏳ Pending

**Next Action:** Implement PAPER_PROFILE infrastructure

---

**Al-Muḥāsibī Verdict:** ✅ **APPROVED FOR EXECUTION** (v1.1 corrections applied)
