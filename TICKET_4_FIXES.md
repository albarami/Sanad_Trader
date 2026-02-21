# Ticket 4 Fixes — Cold Path Real LLM Integration

## Summary

All fixes A–E have been applied to `scripts/async_analysis_queue.py` as specified in the rejection verdict.

## Changes Implemented

### A) Real LLM Calls (No Stubs)

**Created: `scripts/llm_client.py`**
- Extracted `call_claude()` and `call_openai()` from `sanad_pipeline.py`
- Both include:
  - Direct API calls with timeout handling (10s connect, 60s read)
  - Automatic OpenRouter fallback on timeout/connection errors
  - Cost tracking via `cost_tracker.log_api_call()` (best-effort)
- Added `parse_json_failsafe()` for robust JSON parsing with markdown handling

**Updated: `scripts/async_analysis_queue.py`**
- Removed `_call_llm()` stub entirely
- Load prompts from `prompts/*.md` at startup:
  - `sanad-verifier.md`
  - `bull-albaqarah.md`
  - `bear-aldahhak.md`
  - `judge-almuhasbi.md`
- All 4 analysis functions now call real APIs:
  - `run_sanad_verification()` → `llm_client.call_claude()`
  - `run_bull_analysis()` → `llm_client.call_claude()`
  - `run_bear_analysis()` → `llm_client.call_claude()`
  - `run_judge_verdict()` → `llm_client.call_openai()`
- Bull/Bear run in parallel via `ThreadPoolExecutor` (when `parallel_bull_bear=true`)

**Structured Outputs:**
- Each prompt instructs: "Return JSON only"
- Required schemas:
  - **Sanad:** `trust_score`, `rugpull_flags`, `sybil_risk`, `source_reliability`, `reasoning`
  - **Bull:** `verdict`, `confidence`, `rationale`, `key_strengths`
  - **Bear:** `verdict`, `confidence`, `rationale`, `key_risks`
  - **Judge:** `verdict` (APPROVE|REJECT), `confidence` (0-100), `reasons`, `key_risks`, `bias_flags`, `risk_assessment`, `reasoning`
- Parse failures → worker failure → retry/backoff

### B) Safe Task Claiming (Auditable)

**Atomic Transaction:**
```sql
UPDATE async_tasks
SET status='RUNNING', attempts=attempts+1, updated_at=?
WHERE task_id=? AND status='PENDING' AND next_run_at<=?
```

**Authoritative DB Values:**
- After claim, immediately `SELECT attempts, task_type, entity_id` from DB
- Use DB `attempts` value for all logs/backoff/failure decisions
- `claim_task()` now returns full task dict (not just bool)

**Failure Handling:**
- `mark_task_failed()` uses DB `attempts` value (already incremented at claim)
- If `attempts >= MAX_RETRIES` → status='FAILED' + `risk_flag='FLAG_ASYNC_FAILED_PERMANENT'`
- Else → status='PENDING' + exponential backoff: 300s, 900s, 3600s

### C) Fixed Timestamps (Wall Clock)

**Before (WRONG):**
```python
start = time.perf_counter()
started_at = datetime.fromtimestamp(start, tz=timezone.utc)  # INVALID
```

**After (CORRECT):**
```python
started_at = datetime.now(timezone.utc).isoformat()  # Wall clock
perf_start = time.perf_counter()  # Only for duration
...
completed_at = datetime.now(timezone.utc).isoformat()  # Wall clock
duration_sec = time.perf_counter() - perf_start
```

### D) Catastrophic Flagging (Production Logic)

**Removed:** Synthetic signal_id marker checks (`"CATASTROPHIC" in signal_id`)

**Added:** Real Judge output parsing:
```python
judge_parsed = judge_result.get("parsed", {})
verdict = judge_parsed.get("verdict")
confidence = judge_parsed.get("confidence", 0)

if verdict == "REJECT" and confidence >= CATASTROPHIC_THRESHOLD:
    risk_flag = "FLAG_JUDGE_HIGH_CONF_REJECT"
```

**Threshold:** `cold_path.catastrophic_confidence_threshold = 85` (from `config/thresholds.yaml`)

### E) Model Configuration Fix

**Updated:** `config/thresholds.yaml`
- Changed `model: "anthropic/claude-opus-4-6"` → `"claude-haiku-4-5-20251001"`
- Changed `judge_model: "openai/gpt-5.2"` → `"gpt-5.2"`
- Reason: Direct API calls don't use provider prefixes (those are OpenRouter format)

## Verification Checklist

### Real LLM Calls ✓
- [x] `llm_client.py` extracted from `sanad_pipeline.py`
- [x] All 4 stages call real APIs (Sanad/Bull/Bear/Judge)
- [x] Prompts loaded from `prompts/*.md`
- [x] Structured JSON outputs with failsafe parsing
- [x] Cost tracking enabled (best-effort)

### Safe Claiming ✓
- [x] Single atomic UPDATE transaction
- [x] Authoritative DB values fetched after claim
- [x] `attempts` incremented at claim, used throughout
- [x] Retry backoff: 300s → 900s → 3600s
- [x] Final failure → `FLAG_ASYNC_FAILED_PERMANENT`

### Wall-Clock Timestamps ✓
- [x] `started_at` = `datetime.now(timezone.utc).isoformat()`
- [x] `completed_at` = `datetime.now(timezone.utc).isoformat()`
- [x] `duration_sec` = `perf_counter()` delta only

### Production Catastrophic Logic ✓
- [x] Parse Judge verdict from JSON output
- [x] Check `verdict=="REJECT" AND confidence>=85`
- [x] Set `risk_flag="FLAG_JUDGE_HIGH_CONF_REJECT"`
- [x] No synthetic signal_id markers in production code

### Model Config ✓
- [x] Direct API model names (no provider prefix)
- [x] Haiku for cost efficiency in cold path
- [x] GPT-5.2 for Judge (consistency with v3.0)

## Test Outputs Required

To demonstrate compliance, run the following tests and provide logs:

### 1. Real Task Processing (PENDING → DONE)
```bash
cd /data/.openclaw/workspace/trading
python3 scripts/async_analysis_queue.py
```

**Expected logs:**
- `Claimed task <id> (attempt 1)`
- `Running Sanad verification`
- `Running Bull analysis`
- `Running Bear analysis`
- `Running Judge verdict`
- `Task <id> completed in X.Xs (verdict=APPROVE/REJECT, confidence=N%)`
- `Task <id> marked DONE`

**Expected DB state:**
- `async_tasks.status = 'DONE'`
- `positions.async_analysis_complete = 1`
- `positions.async_analysis_json` contains: `sanad`, `bull`, `bear`, `judge` with `raw` + `parsed` + `model`

### 2. Catastrophic Test (High-Confidence Reject)
Create a signal that triggers Judge REJECT with high confidence.

**Expected:**
- `CATASTROPHIC: Judge rejected <token> with N% confidence (threshold=85)`
- `positions.risk_flag = 'FLAG_JUDGE_HIGH_CONF_REJECT'`
- `positions.async_analysis_complete = 1` (still marked complete)

### 3. Retry Test (Worker Failure)
Simulate API timeout or parse failure.

**Expected:**
- `Task <id> failed: <error>`
- `Task <id> retry scheduled in 300s (attempt 1/3)`
- `async_tasks.attempts = 1`
- `async_tasks.next_run_at` moved ~300s into future
- After 3 failures: `Task <id> FAILED permanently after 3 attempts`
- `positions.risk_flag = 'FLAG_ASYNC_FAILED_PERMANENT'`

## Files Modified

1. **Created:** `scripts/llm_client.py` (268 lines)
2. **Updated:** `scripts/async_analysis_queue.py` (615 lines)
3. **Updated:** `config/thresholds.yaml` (model names fixed)

## Git Diff Summary

```
scripts/llm_client.py              | 268 +++++++++++++++++++
scripts/async_analysis_queue.py   | 310 +++++++++++-----------
config/thresholds.yaml             |   4 +-
3 files changed, 442 insertions(+), 140 deletions(-)
```

## Next Steps

1. Test worker with real API credentials
2. Verify all 3 test outputs (normal, catastrophic, retry)
3. Commit to branch `v3.1-ticket-4`
4. Resubmit for approval with test logs

---

**Status:** All fixes A–E implemented. Ready for testing with real API credentials.
