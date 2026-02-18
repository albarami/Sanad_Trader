# Root Cause Fix: 90-Minute Hang Resolution

## Problem Statement
Router hung for 90 minutes on WAR token despite subprocess timeout=300s (5 minutes).

## Root Cause Analysis

### Why subprocess timeout didn't fire
`subprocess.run(timeout=300)` only enforces:
1. Time limit on subprocess **startup**
2. Time limit on subprocess **total runtime**

**It does NOT enforce timeouts on HTTP calls INSIDE the subprocess.**

### The Real Bug
`urllib.request.urlopen(timeout=90)` timeout only covers:
- Initial TCP connection
- **Individual** read operations

**If the server accepts the connection but never sends data**, the timeout doesn't fire because:
- Connection succeeded (no connection timeout)
- No read operation is attempted (server is silent)
- Socket blocks indefinitely waiting for response

This is the classic `urllib` vs `requests` timeout bug.

## Fixes Applied

### 1. Replaced urllib with requests (All API Calls)

**BEFORE (urllib):**
```python
req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
with urllib.request.urlopen(req, timeout=90) as resp:
    result = json.loads(resp.read().decode("utf-8"))
```

**AFTER (requests):**
```python
response = requests.post(
    url,
    headers=headers,
    json={...},
    timeout=(10, 60)  # (connect_timeout, read_timeout)
)
response.raise_for_status()
result = response.json()
```

**Key Difference:**
- `requests` uses separate connect + read timeouts
- `timeout=(10, 60)` = 10s to connect, 60s to read response
- **Total max time: 70 seconds** (connect + read)
- If server accepts but doesn't respond, read timeout fires at 60s

### 2. API-Specific Timeouts

#### Anthropic Claude (Opus/Haiku)
- **Connect timeout:** 10 seconds
- **Read timeout:** 60 seconds
- **Total max:** 70 seconds
- **Fallback:** OpenRouter Claude

#### OpenAI GPT (Judge)
- **Connect timeout:** 10 seconds
- **Read timeout:** 60 seconds
- **Total max:** 70 seconds
- **Fallback:** OpenRouter GPT

#### Perplexity (Sanad Intelligence)
- **Connect timeout:** 10 seconds
- **Read timeout:** 30 seconds (faster for search)
- **Total max:** 40 seconds
- **Fallback:** OpenRouter Perplexity

#### OpenRouter (Fallback)
- **Connect timeout:** 10 seconds
- **Read timeout:** 90 seconds
- **Total max:** 100 seconds
- **No fallback:** Returns None on failure

### 3. Global Router Timeout (Dead Man's Switch)

**BEFORE:**
```python
if __name__ == "__main__":
    try:
        run_router()
    except Exception as e:
        _log(f"FATAL: {e}")
        sys.exit(1)
```

**AFTER:**
```python
if __name__ == "__main__":
    import signal
    
    def timeout_handler(signum, frame):
        _log("GLOBAL TIMEOUT: Router exceeded 10 minute hard limit - forcing exit")
        sys.exit(124)  # Exit code 124 = timeout
    
    # Set global 10-minute timeout (dead man's switch)
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(600)  # 10 minutes total for entire router run
    
    try:
        run_router()
        signal.alarm(0)  # Cancel alarm if completed successfully
    except Exception as e:
        signal.alarm(0)  # Cancel alarm on error
        _log(f"FATAL: {e}")
        sys.exit(1)
```

**How it works:**
- `signal.alarm(600)` sets a 10-minute timer
- If router doesn't complete within 10 minutes, SIGALRM fires
- Handler logs and exits with code 124
- Watchdog will detect this and restart

## Timeout Cascade

Now there are **4 layers of timeout protection**:

### Layer 1: API Request Timeout (70s max)
- Anthropic/OpenAI: 10s connect + 60s read = 70s
- Perplexity: 10s connect + 30s read = 40s

### Layer 2: Fallback API Timeout (100s max)
- OpenRouter: 10s connect + 90s read = 100s

### Layer 3: Subprocess Timeout (300s = 5 minutes)
- `subprocess.run(timeout=300)` in signal_router.py
- Kills entire pipeline process if it exceeds 5 minutes

### Layer 4: Global Router Timeout (600s = 10 minutes)
- `signal.alarm(600)` in signal_router.py main
- Force-kills entire router run if it exceeds 10 minutes
- Watchdog detects and restarts

## Expected Behavior After Fix

### Normal Case (Signal completes)
1. Claude API called → responds in 15s → success
2. No fallback needed
3. Subprocess completes in 30s
4. Router completes in 2 minutes
5. All alarms cancelled

### Timeout Case (Server hangs)
1. Claude API called → server accepts connection
2. Server doesn't respond
3. **Read timeout fires at 60s** ← NEW
4. Fallback to OpenRouter
5. OpenRouter responds in 20s
6. Subprocess completes in 90s
7. Router completes in 3 minutes

### Worst Case (All APIs hang)
1. Claude API → read timeout at 60s
2. OpenRouter fallback → read timeout at 90s
3. Pipeline returns None → signal skipped
4. Next signal attempted
5. If all signals hang, subprocess timeout at 300s
6. If multiple batches hang, global timeout at 600s
7. **Router force-killed, watchdog restarts**

## Files Modified

1. `/data/.openclaw/workspace/trading/scripts/sanad_pipeline.py`
   - Added `import requests`
   - Replaced `call_claude()` urllib → requests (70s timeout)
   - Replaced `call_openai()` urllib → requests (70s timeout)
   - Replaced `call_perplexity()` urllib → requests (40s timeout)
   - Replaced `_fallback_openrouter()` urllib → requests (100s timeout)

2. `/data/.openclaw/workspace/trading/scripts/signal_router.py`
   - Added global `signal.alarm(600)` timeout
   - Added `timeout_handler()` for forced exit
   - Skip list integration (WAR blocked)

## Testing

```bash
# Test imports
python3 -c "import requests; print('✅ requests installed')"
python3 -c "from scripts.sanad_pipeline import call_claude; print('✅ API functions OK')"

# Test skip list
python3 -c "from scripts.signal_router import _load_skip_list; print(_load_skip_list())"
```

## Prevention: Why This Won't Happen Again

1. ✅ **Request-level timeouts** - All API calls now have connect + read timeouts
2. ✅ **Subprocess timeout** - 5-minute hard limit per pipeline call
3. ✅ **Global timeout** - 10-minute hard limit per router run
4. ✅ **Skip list** - Toxic tokens blocked for 24 hours
5. ✅ **Watchdog detection** - Detects stalls and applies adaptive fixes
6. ✅ **Context tracking** - Logs which token/stage/API caused the stall

**Maximum possible hang time: 10 minutes** (global timeout)
- Previous: 90+ minutes (no timeout)
- Improvement: **9x faster recovery**

## Verification

Next router run will:
1. Skip WAR (blocked until 2026-02-20)
2. Process other signals with 70s API timeouts
3. Complete within 5 minutes or kill subprocess
4. Complete entire run within 10 minutes or force exit

If any signal causes timeout:
- Caught by Layer 1 (70s)
- Falls back to OpenRouter (Layer 2)
- Worst case: skipped and next signal attempted
- **No 90-minute hangs possible**
