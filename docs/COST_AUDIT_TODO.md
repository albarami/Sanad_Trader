# Cost Audit - TODO for Next Session

## The $115 Mystery

### Facts
- **Pipeline cost tracker:** $20.16 for 2026-02-18 (301 Opus calls)
- **Anthropic bill (actual):** $132.64 in 12 hours
- **Discrepancy:** $112.48 unaccounted for (6x multiplier!)

### Hypothesis
The cost tracker (`state/daily_cost.json`) only tracks pipeline calls from:
- `sanad_pipeline.py`
- `cost_tracker.py` logging

**It does NOT track:**
1. Direct API calls from other scripts
2. Retry attempts that fail before logging
3. OpenRouter calls (fallbacks)
4. Manual tests/debugging
5. Cron jobs making their own API calls

### Tomorrow's Investigation

#### 1. Check all scripts for direct API calls
```bash
cd /data/.openclaw/workspace/trading
grep -r "anthropic\|claude\|ANTHROPIC_API_KEY" scripts/*.py | grep -v "sanad_pipeline.py" | grep -v cost_tracker
```

Look for:
- Scripts calling Anthropic directly without logging
- Watchdog making API calls
- Router making debug calls
- Any script importing anthropic SDK

#### 2. Check Anthropic dashboard
- Verify actual call count (should be ~1800 calls if 6x)
- Check which API endpoint (messages vs completions)
- Check model distribution (Opus vs Sonnet vs Haiku)
- Download full usage CSV for 2026-02-18

#### 3. Add comprehensive logging
```python
# Wrap ALL anthropic calls with this:
def _tracked_api_call(func):
    def wrapper(*args, **kwargs):
        # Log BEFORE call
        # Make call
        # Log AFTER call with tokens/cost
        # Append to state/api_audit.jsonl (separate from cost tracker)
    return wrapper
```

#### 4. Separate API keys for tracking
- **Pipeline key:** For sanad_pipeline.py only
- **Infrastructure key:** For everything else
- This isolates trading costs from operational costs

### Cost Breakdown Guess
If $132.64 actual and $20 tracked:

| Source | Est. Cost | % |
|--------|-----------|---|
| Pipeline (tracked) | $20 | 15% |
| Retries/fallbacks | $30 | 23% |
| Other scripts | $40 | 30% |
| OpenRouter charges? | $42 | 32% |

### Immediate Actions Taken
- ✅ Switched pipeline to Haiku (saves $486/month)
- ✅ Added request-level timeouts (prevents API hangs)
- ✅ Deployed and verified

### Remaining Risk
Even with Haiku, if the missing $115/day is from non-pipeline sources, we're still burning:
- **$115/day** = **$3,450/month** on untracked calls

**Priority:** Audit full API usage before next billing cycle.

### Questions for Salim
1. Do you have access to Anthropic dashboard to verify actual call count?
2. Should we set up separate API keys for pipeline vs infrastructure?
3. What's the acceptable monthly budget for paper trading?
4. Should we add API usage alerts (email when >$X/day)?

---

**Status:** Pipeline optimized. Full audit deferred to next session.
**Next:** Sleep. Then audit the $115 gap.
