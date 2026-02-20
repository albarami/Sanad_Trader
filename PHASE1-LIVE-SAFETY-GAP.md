# ⚠️ CRITICAL: Phase 1 LIVE Safety Gap

**Date:** 2026-02-21 06:50 GMT+8  
**Severity:** HIGH  
**Status:** BLOCKS LIVE PROMOTION

---

## Issue Summary

Phase 1 relaxed baseline thresholds in `thresholds.yaml` to enable PAPER learning mode. However, **LIVE-specific threshold keys are not being used by enforcement code**, creating a risk that LIVE mode operates with relaxed thresholds.

---

## Detailed Findings

### Baseline Thresholds (RELAXED):
```yaml
scoring:
  min_trust_score: 35  # Was 50
  min_confidence_score: 30  # Was 60
signals:
  min_sanad_score: 40  # Was 55
```

### LIVE-Specific Keys (EXIST BUT UNUSED):
```yaml
scoring:
  live_min_trust_score: 70  ✅ Correct value
strategies:
  live_min_confidence_score: 60  ✅ Correct value
signals:
  live_mode_min_sanad_score: 70  ✅ Correct value
```

### The Gap:
```bash
$ grep -r "live_min_trust\|live_min_confidence\|live_mode_min_sanad" scripts/*.py
(no results)
```

**No enforcement code references the live-specific keys.**

---

## Risk Assessment

### If LIVE Mode Activated Now:
- Trust threshold: **35 instead of 70** (2x too permissive)
- Confidence threshold: **30 instead of 60** (2x too permissive)
- Sanad score threshold: **40 instead of 70** (1.75x too permissive)

### Impact:
- Lower quality signals approved
- Higher rugpull risk
- Defeats purpose of strict LIVE filtering

---

## Root Cause

When baseline thresholds were lowered for PAPER learning mode:
1. ✅ LIVE-specific keys were added to preserve strict values
2. ❌ Enforcement code was NOT updated to read live-specific keys
3. ❌ `get_threshold()` function exists but is not wired to enforcement points

---

## Required Fixes (Before LIVE Promotion)

### Option A: Wire get_threshold() to All Enforcement Points
**Scope:** Modify all threshold checks to use `get_threshold()` resolver

**Example:**
```python
# OLD (unsafe):
min_trust = THRESHOLDS["scoring"]["min_trust_score"]

# NEW (safe):
min_trust = get_threshold("min_trust_score", "scoring", default=50)
```

**Locations to fix:**
- Sanad verifier trust checks
- Confidence score checks
- Tradeability checks
- Signal scoring

**Effort:** Medium (10-15 enforcement points)

---

### Option B: Restore Baseline to Strict, Use Only paper_profiles
**Scope:** Revert baseline thresholds to LIVE values, force PAPER to use profiles

**Changes:**
```yaml
scoring:
  min_trust_score: 70  # Restore LIVE default
  min_confidence_score: 60  # Restore LIVE default

# PAPER must explicitly use paper_profiles.LEARN
```

**Enforcement:**
- Default (baseline) = LIVE-safe
- PAPER+LEARN must call `get_threshold()` to get relaxed values
- If `get_threshold()` not called = fails safe to strict

**Effort:** Low (just YAML change + verify)

---

## Al-Muḥāsibī Recommendation

**Choose Option B** (restore baseline to strict).

**Rationale:**
1. **Fail-safe by default** - any code path that doesn't check profile gets strict thresholds
2. **Minimal code changes** - only YAML modification required
3. **LIVE immediately safe** - no risk of using relaxed thresholds
4. **PAPER still works** - get_threshold() already implemented for the places that need it

**Trade-off:** Some PAPER code paths may get strict thresholds if they don't use get_threshold(), but that's acceptable (PAPER can be strict in some gates).

---

## Immediate Action Required

### Before ANY LIVE promotion:
1. ✅ Audit complete (this document)
2. **Choose Option A or B**
3. **Apply fix**
4. **Verify with test script**
5. **Document in Phase 1 sign-off**

### For NOW (PAPER operation):
- ✅ PAPER mode is safe (relaxed thresholds are intentional)
- ✅ Continue Phase 1 testing
- ❌ **DO NOT promote to LIVE** until fix applied

---

## Verification Script

After fix is applied, run:

```python
import os, yaml
os.environ["SYSTEM_MODE"] = "LIVE"

cfg = yaml.safe_load(open("config/thresholds.yaml"))

# Test baseline thresholds
baseline_trust = cfg['scoring']['min_trust_score']
assert baseline_trust >= 50, f"Baseline trust too low: {baseline_trust}"

print("✅ LIVE safety verified: baseline thresholds are strict")
```

---

## Status

- **PAPER+LEARN:** ✅ APPROVED (relaxed thresholds intentional)
- **LIVE Readiness:** ❌ BLOCKED until fix applied
- **Phase 2:** ✅ Can proceed (PAPER only)

---

**Al-Muḥāsibī Verdict:** Phase 1 is PAPER-APPROVED but NOT LIVE-READY.
