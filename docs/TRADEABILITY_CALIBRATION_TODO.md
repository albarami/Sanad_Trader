# Tradeability Scorer Calibration - Critical Issues

## Problem
XRP and DOGE (top-10 coins, $900M-$2.5B daily volume) score 43/100 instead of expected 70+.

## Root Cause
CoinGecko signals are missing critical market data fields:
- volume_24h: null (should be $2.5B for XRP)
- price_change_1h/24h: null
- chain: "unknown" (should be "binance" for majors)
- liquidity_usd: null

**Result:** Scorer gives 0 points for momentum/volume/liquidity (45 points lost).

## Current Scoring Breakdown for XRP
```
Momentum (0-25):    0  ← No price_change data
Volume (0-20):      0  ← No volume_24h  
Liquidity (0-20):   0  ← chain != "binance", no liquidity_usd
Timing (0-15):     15  ← Fresh signal ✓
Catalyst (0-10):    3  ← coingecko source
Anti-Crowding:     10  ← Default ✓
-------------------------
Total:             28  ← CRITICAL: Should be 70+
```

## Fixes Required

### 1. **Data Pipeline** (High Priority)
CoinGecko client (`scripts/coingecko_client.py`) IS fetching volume/price_change data, but it's getting lost somewhere between:
- coingecko_client.py writes signal → signals/coingecko/*.json
- signal_router.py reads signal → normalizes → feeds to tradeability_scorer

**Debug:**
```bash
# Check if raw CoinGecko file has data
cat signals/coingecko/$(ls -t signals/coingecko/*.json | head -1) | \
  jq '.[] | select(.symbol == "XRP") | {symbol, volume_24h, price_change_24h_pct, market_cap}'

# Check if signal_window has data
cat state/signal_window.json | \
  jq '.signals[] | select(.token == "XRP") | {token, volume_24h, price_change_24h, chain}'
```

If raw file HAS data but signal_window MISSING data → `signal_normalizer.py` bug.  
If raw file MISSING data → `coingecko_client.py` bug.

### 2. **Chain Detection** (High Priority)
Majors (BTC/ETH/SOL/XRP/DOGE) should be tagged `chain: "binance"` for instant liquidity=20 bonus.

**Fix location:** `scripts/signal_normalizer.py` normalize_signal() coingecko branch:
```python
"coingecko": lambda r: {
    "token": r.get("symbol", "").upper(),
    "source": "coingecko",
    "chain": "binance" if r.get("symbol","").upper() in ["BTC","ETH","SOL","XRP","DOGE","ADA","MATIC","AVAX","DOT","LINK"] else "unknown",  # ADD THIS
    "volume_24h": r.get("volume_24h"),  # Ensure this maps correctly
    "price_change_24h": r.get("price_change_24h_pct"),
    ...
}
```

### 3. **Momentum Calibration** (Medium Priority)
Current logic penalizes negative price action:
```python
if abs(price_1h) > 5:
    momentum_score += 10  # Currently requires POSITIVE >5% move
```

For trend-following strategies, this is correct. But for mean-reversion/oversold bounces, negative momentum can be a BUY signal (RSI <30).

**Options:**
- Keep as-is for BULL regime (chase momentum)
- Reverse for BEAR regime (fade moves, buy dips)
- Use absolute value: `if abs(price_1h) > 5` (works for both)

### 4. **Volume Calibration** (Low Priority)
Thresholds are reasonable but could be tuned:
```python
if vol_24h > 10_000_000:   # > $10M → score=20
elif vol_24h > 5_000_000:  # > $5M  → score=15
elif vol_24h > 1_000_000:  # > $1M  → score=10
```

XRP $2.5B should easily hit 20. But if `vol_24h` is null, score = 0.

### 5. **Config-Based Thresholds** (Medium Priority)
Move hardcoded thresholds to `config/thresholds.yaml`:
```yaml
tradeability:
  gate_threshold:
    BULL: 55
    BEAR_HIGH_VOL: 35
    BEAR_LOW_VOL: 40
    UNKNOWN: 40
  
  component_weights:
    momentum_max: 25
    volume_max: 20
    liquidity_max: 20
    timing_max: 15
    catalyst_max: 10
    crowding_max: 10
```

Then load in signal_router.py instead of hardcoding.

## Expected Results After Fix

### XRP (with correct data)
```
Momentum:       15  ← Has price_change_24h = -4.5% (accelerating), check 1h too
Volume:         20  ← $2.5B > $10M threshold
Liquidity:      20  ← chain = "binance" (majors auto-max)
Timing:         15  ← Fresh signal (<5min)
Catalyst:        3  ← coingecko source (low priority)
Anti-Crowding:  10  ← Default
-------------------------
Total:          83  ← GOOD: Passes threshold cleanly
```

### Solana Meme (e.g., GROKIUS)
```
Momentum:        0  ← -15% 1h (negative momentum, no reversal signal)
Volume:         10  ← $2M volume (mid-tier)
Liquidity:       5  ← $80K liquidity (thin)
Timing:         10  ← 10min old signal
Catalyst:        4  ← birdeye source
Anti-Crowding:   8  ← Moderate interest
-------------------------
Total:          37  ← GOOD: Correctly filtered out
```

## Testing
After fixes, run:
```bash
cd /data/.openclaw/workspace/trading

# Test with known good signal
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from tradeability_scorer import score_tradeability

xrp = {
    'token': 'XRP',
    'chain': 'binance',
    'volume_24h': 2500000000,
    'price_change_1h': -2.1,
    'price_change_24h': -4.5,
    'timestamp': '$(date -u +%Y-%m-%dT%H:%M:%S+00:00)',
    'source': 'coingecko',
}

score = score_tradeability(xrp)
print(f'XRP score: {score}/100')
print('Expected: 75-85')
print('Pass: ' + ('✅' if score >= 70 else '❌ FAIL'))
"
```

Expected output:
```
XRP score: 83/100
Expected: 75-85
Pass: ✅
```

## Priority Order
1. **Fix data pipeline** (XRP signals must have volume/price_change/chain fields)
2. **Add chain detection** (majors → "binance")
3. **Move thresholds to config** (stop hardcoding)
4. **Calibrate momentum** (consider mean-reversion signals)
5. **Test with 10 majors** (BTC/ETH/SOL/XRP/DOGE/etc)

Once calibrated, threshold should stay at 55 for quality control. Lower thresholds = more garbage LLM calls.
