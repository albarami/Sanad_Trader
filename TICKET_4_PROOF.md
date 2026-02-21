# Ticket 4 Acceptance Proof — Cold Path Real LLM Integration

## Summary

All fixes A–E have been implemented and **tested end-to-end** with real API calls. Both required test cases (E1 normal + E2 catastrophic) passed successfully.

---

## FIX A: Strict Structured JSON for ALL Cold-Path Agents

### A1 — Prompt Contract (Mandatory)

Added `JSON_CONTRACT` suffix to all prompts:

```python
JSON_CONTRACT = """

CRITICAL OUTPUT FORMAT:
Return ONLY a single JSON object.
No markdown. No prose. No code fences.
The JSON object must match the schema provided above exactly.
"""
```

Applied to:
- Sanad verification: `SANAD_PROMPT + JSON_CONTRACT`
- Bull analysis: `BULL_PROMPT + JSON_CONTRACT`
- Bear analysis: `BEAR_PROMPT + JSON_CONTRACT`
- Judge verdict: `JUDGE_PROMPT + JSON_CONTRACT`

Each user message includes explicit JSON schema examples.

### A2 — Robust JSON Extraction + Validation

Implemented `llm_client.extract_json_object(raw_text)`:
- Finds first top-level `{...}` object (handles leading/trailing prose)
- Strips markdown code blocks (`\`\`\`json`)
- Returns parsed dict or None

**Judge-specific validation:**
```python
if "verdict" not in parsed:
    raise ValueError("ERR_JUDGE_PARSE: Judge JSON missing verdict field")

if parsed["verdict"] not in ["APPROVE", "REJECT"]:
    raise ValueError(f"ERR_JUDGE_PARSE: Invalid verdict value: {parsed['verdict']}")

if "confidence" not in parsed:
    raise ValueError("ERR_JUDGE_PARSE: Judge JSON missing confidence field")

if not isinstance(parsed["confidence"], (int, float)):
    raise ValueError(f"ERR_JUDGE_PARSE: Invalid confidence type: {type(parsed['confidence'])}")

confidence = int(parsed["confidence"])
if not (0 <= confidence <= 100):
    raise ValueError(f"ERR_JUDGE_PARSE: confidence out of range: {confidence}")
```

**Critical:** If `confidence` is missing or invalid, the worker raises `ERR_JUDGE_PARSE` and triggers retry.

---

## FIX B: Atomic Claim + Authoritative Attempts

### Single Atomic Transaction

```python
def claim_task(task_id: str) -> dict:
    # Atomic UPDATE
    cursor = conn.execute("""
        UPDATE async_tasks
        SET status = 'RUNNING',
            attempts = attempts + 1,
            updated_at = ?
        WHERE task_id = ? 
          AND status = 'PENDING'
          AND next_run_at <= ?
    """, (now_iso, task_id, now_iso))
    
    if cursor.rowcount == 0:
        return None
    
    # Fetch authoritative DB values AFTER claim
    row = conn.execute("""
        SELECT task_id, entity_id, task_type, attempts, created_at
        FROM async_tasks
        WHERE task_id = ?
    """, (task_id,)).fetchone()
    
    return dict(row)  # Contains DB attempts value
```

### Backoff Schedule (Exact)

```python
RETRY_DELAYS = {
    1: 300,   # 5 minutes
    2: 900,   # 15 minutes
    3: 3600,  # 60 minutes
}

def mark_task_failed(task_id, error_code, error_msg, attempts):
    if attempts >= MAX_RETRIES:
        # status='FAILED' + FLAG_ASYNC_FAILED_PERMANENT
    else:
        delay_sec = RETRY_DELAYS.get(attempts, 3600)
        # status='PENDING' + next_run_at = now + delay_sec
```

**No double-increment:** `attempts` is incremented ONLY in the atomic claim. Failure handling uses the authoritative DB value.

---

## FIX C: Debuggable Judge Failures

### Error Codes

All failures use structured error codes:
- `ERR_JUDGE_PARSE` — Judge JSON missing required fields or invalid format
- `ERR_JSON_PARSE` — Failed to extract JSON from any agent
- `ERR_VALIDATION` — Validation failure (e.g., missing trust_score)
- `ERR_WORKER` — API timeout, network error, or worker crash

### Raw LLM Dump

Created `_dump_raw_llm(task_id, stage, raw_text)`:
```python
def _dump_raw_llm(task_id: str, stage: str, raw_text: str):
    filename = LLM_RAW_DIR / f"{stage}_{task_id}.txt"
    with open(filename, "w") as f:
        f.write(f"Task: {task_id}\n")
        f.write(f"Stage: {stage}\n")
        f.write(f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n")
        f.write("=" * 60 + "\n")
        f.write(raw_text)
```

Called on **every** JSON parse failure:
```python
if not parsed:
    _dump_raw_llm(task_id, "judge", raw)
    raise ValueError("ERR_JUDGE_PARSE: Failed to extract JSON")
```

Dumps saved to: `logs/llm_raw/judge_<task_id>.txt`

---

## FIX D: Production Catastrophic Logic

### Judge JSON Fields ONLY

```python
# FIX D: Catastrophic flagging from Judge JSON ONLY
risk_flag = None
judge_parsed = judge_result.get("parsed", {})
verdict = judge_parsed.get("verdict")
confidence = judge_parsed.get("confidence", 0)

if verdict == "REJECT" and confidence >= CATASTROPHIC_THRESHOLD:
    risk_flag = "FLAG_JUDGE_HIGH_CONF_REJECT"
    _log(f"CATASTROPHIC: Judge rejected {token_symbol} with {confidence}% confidence (threshold={CATASTROPHIC_THRESHOLD})")
```

**No synthetic markers.** No `signal_id` checks. Production logic reads Judge output exclusively.

---

## FIX E: Acceptance Proof (MANDATORY)

### E1 — Real DONE Case (Normal Approval)

**Setup:**
- Position: BTC @ $95,000
- Size: $1,000
- Chain: BTC
- Strategy: momentum_flip

**Worker Log:**
```
[2026-02-21T20:40:05.754886+00:00] Claimed task de754278-b044-49a6-8409-ff20342d0eb8 (attempt 1)
[2026-02-21T20:40:05.755993+00:00] Running Cold Path for BTC
[2026-02-21T20:40:05.756037+00:00] Running Sanad verification for position 237e85df-1df2-4dec-a1a8-60a36fe41c9c
[2026-02-21T20:40:11.740015+00:00] Running Bull analysis
[2026-02-21T20:40:11.740194+00:00] Running Bear analysis
[2026-02-21T20:40:21.704584+00:00] Running Judge verdict
[2026-02-21T20:40:34.952590+00:00] Task de754278-b044-49a6-8409-ff20342d0eb8 completed in 29.2s (verdict=REJECT, confidence=78%)
[2026-02-21T20:40:34.954373+00:00] Task de754278-b044-49a6-8409-ff20342d0eb8 marked DONE
```

**DB State After:**
```
E1 Task Status:
  status: DONE
  attempts: 1

E1 Position State:
  async_analysis_complete: 1
  risk_flag: None

E1 Analysis JSON Structure:
  Keys: ['sanad', 'bull', 'bear', 'judge', 'meta']
  Sanad trust_score: 100
  Bull verdict: BUY
  Bear verdict: SKIP
  Judge verdict: REJECT
  Judge confidence: 78
  Judge model: gpt-5.2
```

**✅ E1 PROOF:**
- Task went `PENDING → RUNNING → DONE` in single attempt
- `positions.async_analysis_complete = 1`
- `positions.async_analysis_json` contains all 4 stages with valid parsed JSON
- Judge verdict: REJECT with 78% confidence (below catastrophic threshold)
- No `risk_flag` set (confidence < 85%)

---

### E2 — Catastrophic Case (High-Confidence Reject)

**Setup:**
- Position: 0xSUSPICIOUS_MEMECOIN_NO_LIQUIDITY
- Price: $0.00000001 (extremely low)
- Size: $10,000 (large position on sketchy token)
- Chain: BSC (higher rug risk)
- Strategy: degen_meme

**Worker Log:**
```
[2026-02-21T20:41:40.339674+00:00] Claimed task 34b8ba73-327e-4eff-bf08-5a7ed39e64c9 (attempt 1)
[2026-02-21T20:41:40.340857+00:00] Running Cold Path for 0xSUSPICIOUS_MEMECOIN_NO_LIQUIDITY
[2026-02-21T20:41:40.340902+00:00] Running Sanad verification for position ab5337d7-8f9a-40c3-9693-52db3a14d589
[2026-02-21T20:41:47.032677+00:00] Running Bull analysis
[2026-02-21T20:41:47.034661+00:00] Running Bear analysis
[2026-02-21T20:41:56.402593+00:00] Running Judge verdict
[2026-02-21T20:42:07.481578+00:00] CATASTROPHIC: Judge rejected 0xSUSPICIOUS_MEMECOIN_NO_LIQUIDITY with 100% confidence (threshold=85)
[2026-02-21T20:42:07.488731+00:00] Task 34b8ba73-327e-4eff-bf08-5a7ed39e64c9 completed in 27.1s (verdict=REJECT, confidence=100%)
[2026-02-21T20:42:07.492407+00:00] Task 34b8ba73-327e-4eff-bf08-5a7ed39e64c9 marked DONE
```

**DB State After:**
```
E2 Task Status:
  status: DONE
  attempts: 1

E2 Position State:
  async_analysis_complete: 1
  risk_flag: FLAG_JUDGE_HIGH_CONF_REJECT

E2 Analysis JSON Structure:
  Judge verdict: REJECT
  Judge confidence: 100
  Judge reasoning: This trade fails the non-negotiable gates: Sanad trust_score is 0 and there are multiple rugpull fla...
```

**✅ E2 PROOF:**
- Task went `PENDING → RUNNING → DONE` in single attempt
- `positions.async_analysis_complete = 1`
- Judge verdict: REJECT with 100% confidence (≥ catastrophic threshold)
- `positions.risk_flag = FLAG_JUDGE_HIGH_CONF_REJECT` (correctly set by production logic)
- Catastrophic log message emitted

---

## Files Modified

1. **scripts/llm_client.py** — Updated with `extract_json_object()` and robust validation
2. **scripts/async_analysis_queue.py** — All fixes A–E implemented:
   - Strict JSON contracts (A1)
   - Robust extraction + validation (A2)
   - Atomic claim + authoritative attempts (B)
   - Error codes + raw LLM dumps (C)
   - Production catastrophic logic (D)

## Git Commit

**Branch:** `v3.1-ticket-4`  
**Commit:** `54adb5c`  
**Message:** "Ticket 4 v2: Strict JSON contracts + consistent attempts + debuggable failures"

---

## Summary Table

| Test | Token | Judge Verdict | Confidence | Risk Flag | Status |
|------|-------|---------------|------------|-----------|--------|
| E1   | BTC   | REJECT        | 78%        | None      | DONE   |
| E2   | 0xSUSP... | REJECT    | 100%       | FLAG_JUDGE_HIGH_CONF_REJECT | DONE |

Both tasks completed successfully with real API calls, valid persisted analysis, and correct catastrophic flagging.

---

## Acceptance Criteria Met

- ✅ **A1:** Strict JSON contracts appended to all prompts
- ✅ **A2:** Robust JSON extraction with Judge confidence validation
- ✅ **B:** Atomic claim with authoritative DB attempts value
- ✅ **C:** Error codes + raw LLM dumps for debuggability
- ✅ **D:** Production catastrophic logic from Judge JSON only
- ✅ **E1:** Real task reached DONE with valid persisted analysis
- ✅ **E2:** Catastrophic case triggered FLAG_JUDGE_HIGH_CONF_REJECT

**Status:** Ready for approval.
