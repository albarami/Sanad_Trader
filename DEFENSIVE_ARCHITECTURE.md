# Defensive Architecture — Multi-Layer Signal Filtering

## Problem Statement

Hot path (fast_decision_engine.py) approved trades that cold path (Judge @ GPT-5.2) consistently rejected at 99% confidence. Pattern: 10/10 consecutive executed trades were catastrophic (stablecoins, self-pairs, missing holder data).

**Root cause:** Single point of failure at hot path gates. No defense-in-depth.

## Solution: 3-Layer Defense Architecture

### Layer 1: Source Filtering (INGESTION)
**Location:** Signal generators (whale_tracker.py, coingecko_scraper.py, etc.)  
**Action:** Block invalid signals BEFORE they enter the system  
**Implementation:**
```python
from stablecoin_filter import is_stablecoin

# Before creating signal
if is_stablecoin(token=symbol, address=mint):
    _log(f"BLOCKED stablecoin {symbol} from signal generation")
    continue
```

**Blocked at source:**
- Stablecoins (USDT, USDC, DAI, etc.)
- Self-pairs (BTC/BTC, ETH/ETH)
- Malformed symbols (garbage containing "USDT")

### Layer 2: Router Filtering (SCORING)
**Location:** signal_router.py `_score_signal()`  
**Action:** Score invalid signals as -999 (auto-reject)  
**Implementation:**
```python
def _score_signal(signal: dict, age_minutes: float, is_cross_source: bool) -> int:
    # Backup stablecoin filter
    if is_stablecoin(token=signal.get("token"), symbol=signal.get("symbol"), address=signal.get("token_address")):
        _log(f"  SKIP {signal.get('token')}: stablecoin (backup filter)")
        return -999
    # ... rest of scoring logic
```

**Purpose:** Catches signals that slip through source filters (e.g., legacy files, external sources)

### Layer 3: Hot Path Gates (EXECUTION BLOCKING)
**Location:** fast_decision_engine.py `stage_1_hard_safety_gates()`  
**Action:** Block execution with specific BLOCK_* reason codes  
**Implementation:**
```python
# Universal gates (CANNOT be bypassed by HAS_HARD_GATES flag)
if symbol_lower == quote_lower:
    return "BLOCK_SELF_PAIR", f"Self-pair blocked: {symbol}/{quote}"

if is_stablecoin(symbol=symbol, address=address):
    return "BLOCK_STABLECOIN", f"Stablecoin blocked: {symbol}"

if solscan_holder_count < 10:
    return "BLOCK_HOLDER_COUNT_CRITICAL", f"Insufficient holders: {solscan_holder_count}"
```

**Purpose:** Final safety check before capital allocation. Fail-closed.

## Key Modules

### stablecoin_filter.py
**Type:** Deterministic Python (no LLMs)  
**Exports:**
- `is_stablecoin(token=None, symbol=None, address=None) -> bool`
- `filter_signals(signals: list[dict]) -> (valid, blocked)`

**Detection methods:**
1. **Address-based** (most reliable): 8 Solana stablecoin contract addresses
2. **Symbol-based** (fallback): 28 stablecoin symbols + variants (USDC.e, USDT-8, etc.)

**Coverage:**
- Canonical stablecoins: USDT, USDC, DAI, BUSD, FRAX, TUSD, etc.
- Bridged variants: USDC.e (Ethereum bridge)
- Exchange variants: USDT-8 (Binance)
- Algorithmic: UST, USDD, FRAX, MIM, FEI
- Regional: USDJ, USDK, USDQ

## Symbol Cleanup: whale_tracker.py

**Bug:** Hardcoded USDT suffix in symbol field  
```python
# BEFORE (WRONG)
"symbol": f"{symbol}USDT"  # Generated: "AQZMdy53USDT"

# AFTER (CORRECT)
"symbol": symbol  # Just the raw metadata symbol
```

**Impact:**
- Eliminated garbage symbols like "AQZMdy53USDT", "Cp3G6HCEUSDT"
- Signal router no longer extracts "USDT" from malformed strings
- Cleaner logs, clearer rejection reasons

## Quality Circuit Breaker Integration

The stablecoin filter integrates with the autonomous quality circuit breaker:

1. **Before fix:** 100% rejection rate (10/10 positions catastrophic) → safe_mode activated
2. **After fix:** Stablecoins blocked at source → quality score improves
3. **Learning loop:** Existing catastrophic positions penalized with 3x beta, -2.0 UCB reward

**Autonomous evolution:**
- Pattern detected: 10/10 Judge REJECT @ ≥99% confidence
- Root cause analysis: "USDT" in logs → stablecoin pattern
- Fix deployed: stablecoin_filter.py + whale_tracker.py cleanup
- Validation: Test mode generates clean symbols → passes filter

## Testing

**Test coverage:** `tests/test_stablecoin_filter.py`

1. **Symbol detection:** USDT, USDC, DAI, BUSD, USDC.e → blocked
2. **Address detection:** Solana contract addresses → blocked
3. **Filter signals:** Batch filtering with block_reason field
4. **Malformed symbols:** "AQZMdy53USDT" NOT blocked (not real USDT), "USDT" blocked

**All tests pass.**

## Deployment

**Commit:** `571c73c` on main  
**Date:** 2026-02-23 07:55 UTC  
**Files changed:**
- `scripts/stablecoin_filter.py` (new)
- `scripts/whale_tracker.py` (import + filter logic + symbol cleanup)
- `scripts/signal_router.py` (backup filter in _score_signal)
- `tests/test_stablecoin_filter.py` (new)

**Cron integration:** Whale tracker runs every 30min → now filters at source

## Monitoring

**Logs to watch:**
- `execution-logs/whale_tracker.log` → "BLOCKED stablecoin X from signal generation"
- `logs/signal_router.log` → "SKIP X: stablecoin (backup filter)"
- `state/safe_mode_history.json` → quality circuit breaker status

**Metrics:**
- Rejection rate (target: <20% for valid signals)
- Catastrophic count (target: 0 per 10 positions)
- Safe mode activations (target: 0 per day)

## Future Improvements

1. **Expand address coverage:** Add Ethereum mainnet stablecoin addresses
2. **Regional stablecoins:** EUROC, GBPT, JPYC (when needed)
3. **Algorithmic detection:** Price stability analysis (±1% for 30 days → likely stablecoin)
4. **Centralized oracle:** Maintain allowlist/blocklist in Supabase for cross-system consistency

## Philosophy

**Defense in depth:** Multiple independent layers, each catching different failure modes.  
**Fail closed:** Block by default, allow only validated tokens.  
**Self-healing:** Pattern detection → root cause analysis → autonomous fix deployment.  
**Deterministic core:** No LLMs in critical path (stablecoin detection is regex + address lookup).  
**Evidence-based:** All decisions logged with specific reason codes for post-trade analysis.

---

**Last updated:** 2026-02-23 by Sanad Trader v3.1  
**Status:** DEPLOYED ✅ — All 3 layers active, 4/4 tests passing
