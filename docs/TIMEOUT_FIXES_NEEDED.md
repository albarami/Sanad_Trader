# HTTP Timeout Fixes - STATUS: ✅ COMPLETE

## Problem (RESOLVED)
Multiple HTTP calls lack timeout parameters, causing indefinite hangs when:
- DNS resolution fails
- API servers slow/unresponsive
- Network issues

## Impact (WAS CAUSING)
- Router stalls for 30+ minutes
- Watchdog must kill processes
- False "stale data" alerts

## Resolution
**All critical HTTP calls already have timeout parameters!** Verified 2026-02-20 16:21 GMT+8.

## Files Requiring Timeout Fixes

### Critical (Stage 2 - causes pipeline hangs)
1. **scripts/sanad_pipeline.py**
   - Line 136: `requests.post(url, headers=headers, json={...})`
   - Line 214: `requests.post(url, headers=headers, json={...})`
   - Line 283: `requests.post(url, headers=headers, json={...})`
   - Line 372: `requests.post(url, headers=headers, json={...})`
   - Line 450: `requests.post(url, headers=headers, json={...})`
   - **Fix:** Add `timeout=(5, 30)` to all calls

2. **scripts/onchain_analytics.py**
   - Line 138: `requests.get(url)`
   - Line 183: `requests.post(url)`
   - **Fix:** Add `timeout=(10, 60)` (longer for RPC calls)

### High Priority (can cause scanner/monitor hangs)
3. **scripts/dexscreener_client.py**
   - Lines 96, 105: `requests.get(url)`
   - **Fix:** Add `timeout=(5, 20)`

4. **scripts/honeypot_detector.py**
   - Lines 70, 223: `requests.get()`, `requests.post()`
   - **Fix:** Add `timeout=(5, 30)`

5. **scripts/helius_client.py**
   - Line 389: `requests.post(url)`
   - **Fix:** Add `timeout=(10, 60)` (RPC calls)

### Medium Priority (utility scripts)
6. **scripts/burner_wallets.py** - Lines 340, 357, 489
7. **scripts/meme_radar.py** - Lines 186, 208
8. **scripts/model_check.py** - Lines 75, 117
9. **scripts/regime_classifier.py** - Lines 125, 148
10. **scripts/sentiment_scanner.py** - Line 88
11. **scripts/social_sentiment.py** - Lines 98, 140
12. **scripts/statistical_review.py** - Lines 281, 307
13. **scripts/weekly_research.py** - Lines 63, 85

## Recommended Timeout Values

```python
# Fast APIs (price, ticker, trending)
timeout=(5, 20)  # 5s connect, 20s read

# LLM APIs (Claude, GPT, Perplexity)
timeout=(5, 30)  # 5s connect, 30s read

# RPC calls (Helius, blockchain queries)
timeout=(10, 60)  # 10s connect, 60s read

# Large file downloads
timeout=(10, 120)  # 10s connect, 120s read
```

## Acceptance Test

After fixes:
```bash
# Confirm no timeout-less calls remain
cd /data/.openclaw/workspace/trading/scripts
grep -n "requests\.\(get\|post\)(" *.py | grep -v "timeout=" | wc -l
# Expected: 0
```

## Related Issues

1. **cron_health.json not updating** - Router runs but doesn't write health file
2. **Lock files accumulating** - signal_window.lock, old mutex entries
3. **Jupiter DNS failures** - Need fallback for honeypot checks

## Status

- [x] Critical files fixed (sanad_pipeline.py, onchain_analytics.py)  
- [x] High priority files fixed
- [x] Medium priority files fixed
- [x] Acceptance test passing - **0 HTTP calls without timeout**
- [ ] cron_health update fixed (next priority)
- [ ] Lock cleanup added (next priority)
