# Canonical Signal Schema

**Single source of truth for signal field names across all components.**

All signals MUST use these exact field names. No variants.

## Core Fields

```python
{
    # Identity
    "token": str,              # Ticker symbol (e.g., "XRP", "BTC")
    "source": str,             # Provider (e.g., "coingecko", "birdeye")
    "chain": str,              # "binance" | "solana" | "ethereum" | "unknown"
    "token_address": str,      # Contract address (Solana/ETH tokens only)
    
    # Price & Performance
    "current_price": float,    # Current price in USD
    "price_change_1h_pct": float,   # 1-hour % change (e.g., -3.5 = -3.5%)
    "price_change_24h_pct": float,  # 24-hour % change
    
    # Volume & Liquidity
    "volume_24h_usd": float,   # 24-hour volume in USD
    "liquidity_usd": float,    # Total liquidity in USD (DEX tokens)
    "market_cap": float,       # Market cap in USD
    
    # Technical Indicators (optional, majors only)
    "indicators": {
        "rsi": float,          # 0-100
        "macd_hist": float,    # MACD histogram
        "volume_ratio": float, # Current volume / avg volume
    },
    
    # Metadata
    "timestamp": str,          # ISO 8601 (e.g., "2026-02-19T17:29:20+00:00")
    "direction": str,          # "LONG" | "SHORT"
    "confidence": str,         # "high" | "medium" | "low"
}
```

## Component Responsibilities

### 1. Scanner Output
Scanners (coingecko_client.py, birdeye_client.py, etc.) emit signals with:
- Scanner-specific field names (e.g., `volume_24h`, `price_change_24h_pct`)
- May have extra fields specific to that provider

### 2. signal_normalizer.py
**Converts scanner output → canonical schema**

Maps all scanner variants to canonical names:
```python
# CoinGecko scanner emits:
{
    "volume_24h": 2601944354,
    "price_change_24h_pct": -3.57,
}

# Normalizer outputs:
{
    "volume_24h_usd": 2601944354,
    "price_change_24h_pct": -3.57,
    "chain": "binance",  # Auto-detected
}
```

### 3. market_data_enricher.py
**Adds real-time data from APIs**

Uses canonical names for output:
```python
# Binance enrichment adds:
{
    "volume_24h_usd": 172900000,  # From Binance quoteVolume
    "price_change_24h_pct": -4.07,  # From priceChangePercent
    "price_change_1h_pct": 0.51,    # Calculated from klines
}
```

### 4. tradeability_scorer.py
**Reads canonical names ONLY**

```python
vol_24h = signal.get("volume_24h_usd", 0)
price_1h = signal.get("price_change_1h_pct", 0)
price_24h = signal.get("price_change_24h_pct", 0)
```

Legacy fallback allowed during transition:
```python
vol_24h = signal.get("volume_24h_usd", signal.get("volume_24h", 0))
```

## Why This Matters

### Before (inconsistent):
```python
# Scanner
{"volume_24h": 2600000000}

# Normalizer  
{"volume_24h": 2600000000, "price_change_24h": -3.5}

# Enricher
{"volume_24h": 172000000, "price_change_24h": -4.0}

# Scorer
price = signal.get("price_change_1h", 0)  # Expects non-_pct
volume = signal.get("volume_24h", 0)      # Gets $172M instead of $2.6B
```

**Result:** Scorer sees wrong values, scores don't match reality.

### After (canonical):
```python
# Scanner → Normalizer
{"volume_24h_usd": 2600000000, "price_change_24h_pct": -3.5}

# Enricher overwrites with Binance
{"volume_24h_usd": 172000000, "price_change_24h_pct": -4.0}

# Scorer reads
vol = signal.get("volume_24h_usd", 0)  # Gets $172M (correct Binance volume)
price = signal.get("price_change_24h_pct", 0)  # Gets -4.0% (correct)
```

**Result:** Scorer sees correct values, scores match reality.

## Field Name Reference

| Purpose | Field Name | Units | Type |
|---------|-----------|-------|------|
| Volume 24h | `volume_24h_usd` | USD | float |
| Price change 1h | `price_change_1h_pct` | Percentage | float |
| Price change 24h | `price_change_24h_pct` | Percentage | float |
| Liquidity | `liquidity_usd` | USD | float |
| Market cap | `market_cap` | USD | float |
| Current price | `current_price` | USD | float |
| Chain | `chain` | Enum | str |
| Token | `token` | Symbol | str |

## Migration Rules

When adding new fields:
1. Use canonical names from the start
2. Add `_usd` suffix for USD amounts
3. Add `_pct` suffix for percentages
4. Add `_count` suffix for counts
5. Never use ambiguous names (e.g., "volume" → "volume_24h_usd")

When deprecating old fields:
1. Keep old field for 2 weeks (legacy fallback)
2. Log warning when old field is accessed
3. Remove after transition period

## Validation

Run this test to verify schema compliance:

```bash
cd /data/.openclaw/workspace/trading
python3 << 'EOF'
import sys; sys.path.insert(0, 'scripts')
from signal_normalizer import normalize_signal
from market_data_enricher import enrich_signal
from datetime import datetime, timezone

# Test signal
test = {
    'token': 'XRP',
    'volume_24h': 2601944354,
    'price_change_24h_pct': -3.57,
}

normalized = normalize_signal(test, 'coingecko')
normalized['timestamp'] = datetime.now(timezone.utc).isoformat()
enriched = enrich_signal(normalized)

# Verify canonical fields exist
required = ['volume_24h_usd', 'price_change_24h_pct', 'chain']
missing = [f for f in required if f not in enriched or enriched[f] is None]

if missing:
    print(f'❌ FAIL: Missing canonical fields: {missing}')
    sys.exit(1)
else:
    print('✅ PASS: All canonical fields present')
EOF
```

Expected output: `✅ PASS: All canonical fields present`
