# Data Source Strategy

**Which API to use for which data, and why.**

## Three Token Types, Three Data Strategies

### 1. Binance Majors (BTC, ETH, SOL, XRP, DOGE, etc.)

**Primary source: Binance API**

Use for:
- ✅ `volume_24h_usd` - Binance spot volume (quoteVolume field)
- ✅ `price_change_24h_pct` - Binance 24h ticker (priceChangePercent)
- ✅ `price_change_1h_pct` - Calculated from klines (current close vs 1h ago)
- ✅ `current_price` - Binance lastPrice (execution-grade)
- ✅ Order book depth - For spread/slippage estimation
- ✅ Technical indicators - RSI/MACD from klines

**Why Binance:**
- We trade on Binance → use Binance's view of the market
- Execution-grade data (what we'll actually get filled at)
- Fastest updates (1s ticker, real-time orderbook)
- Authoritative for CEX pairs

**Don't use CoinGecko for:**
- ❌ Volume (aggregates across exchanges, not our venue)
- ❌ Price (can be stale or aggregated)
- ❌ 1h changes (CoinGecko doesn't provide this)

### 2. Solana Tokens (DEX, memecoins, new launches)

**Primary sources: Birdeye + Solscan + Helius**

#### Birdeye (market data)
Use `/defi/token_overview` for:
- ✅ `price_change_1h_pct` - Birdeye tracks hourly
- ✅ `price_change_24h_pct` - Birdeye 24h change
- ✅ `volume_24h_usd` - DEX volume across Raydium/Orca/etc
- ✅ `liquidity_usd` - Total liquidity across pools
- ✅ `market_cap` - Fully diluted valuation
- ✅ Trade count - Number of swaps (activity metric)

Use `/defi/token_security` for:
- ✅ `mint_authority` - Token mint can be inflated?
- ✅ `freeze_authority` - Transfers can be frozen?
- ✅ `lp_locked_pct` - How much LP is locked
- ✅ Rugpull risk score

**Why Birdeye:**
- Aggregates all Solana DEXes (Raydium, Orca, Meteora)
- Tracks hourly price changes (CoinGecko doesn't)
- Security checks built-in (mint/freeze authority)
- Fast updates (2-5 minute lag)

#### Solscan (onchain verification)
Use public API for:
- ✅ `holder_count` - Total token holders
- ✅ `top10_holder_pct` - Concentration risk
- ✅ Token metadata - Decimals, supply, mint address
- ✅ Holder distribution - For Gini coefficient

**Why Solscan:**
- Free, no API key required
- On-chain truth (can't be spoofed)
- Holder concentration = rugpull risk

#### Helius (whale activity)
Use Enhanced Transactions API for:
- ✅ Parsed swaps - Who bought/sold, when, how much
- ✅ Whale wallet tracking - Follow smart money
- ✅ Token transfers - Accumulation/distribution patterns
- ✅ Co-buyer graphs - Find wallets buying together

**Why Helius:**
- Parsed transaction data (not raw RPC)
- Fast (optimized RPC nodes)
- Essential for whale discovery

**Don't use Binance for Solana tokens:**
- ❌ Most aren't listed on Binance yet
- ❌ If listed, Binance volume is tiny vs DEX
- ❌ Price can lag DEX by minutes during pumps

### 3. CoinGecko (Discovery Only)

**Use CoinGecko for:**
- ✅ Discovery - What's trending, what's hot
- ✅ Symbol→ID mapping - "XRP" → "ripple" (for other APIs)
- ✅ Macro context - Total market cap, BTC dominance
- ✅ Category tags - "meme", "defi", "gaming"
- ✅ Watchlist - Track specific tokens

**Don't use CoinGecko for execution:**
- ❌ Volume (aggregated, not venue-specific)
- ❌ Price (can be stale, aggregated across exchanges)
- ❌ 1h changes (not provided in most endpoints)
- ❌ Liquidity (doesn't track DEX liquidity)

**Why this matters:**
CoinGecko shows XRP volume $2.6B (all exchanges), Binance shows $172M (Binance only). We trade on Binance, so use Binance's number.

## Data Flow (Current Implementation)

```
1. Scanner (CoinGecko) emits discovery signal
   ↓
2. Normalizer detects chain (Binance major vs Solana)
   ↓
3. Enricher routes to correct API:
   - Binance majors → Binance API
   - Solana tokens → Birdeye/Solscan (TODO: Sprint 4)
   ↓
4. Scorer uses enriched canonical fields
   ↓
5. Gate filters at threshold
```

## Implementation Status

| Token Type | Discovery | Enrichment | Status |
|------------|-----------|------------|--------|
| Binance majors | CoinGecko ✅ | Binance API ✅ | **Operational** |
| Solana tokens | Birdeye ✅ | Birdeye (TODO) | **Partial** |
| Solana tokens | DexScreener ✅ | Solscan (TODO) | **Partial** |
| Whale signals | N/A | Helius ✅ | **Operational** |

## Sprint 4 TODO: Solana Token Enrichment

Add to `market_data_enricher.py`:

```python
def _enrich_solana_token(signal: dict) -> dict:
    """Enrich Solana token using Birdeye + Solscan."""
    token_address = signal.get("token_address", "")
    if not token_address:
        return signal
    
    # Birdeye market data
    try:
        birdeye_data = birdeye_client.get_token_overview(token_address)
        signal["volume_24h_usd"] = birdeye_data["volume24h"]
        signal["price_change_1h_pct"] = birdeye_data["priceChange1h"]
        signal["price_change_24h_pct"] = birdeye_data["priceChange24h"]
        signal["liquidity_usd"] = birdeye_data["liquidity"]
        signal["market_cap"] = birdeye_data["marketCap"]
    except:
        pass
    
    # Solscan holder data
    try:
        solscan_data = solscan_client.get_token_holders(token_address)
        signal["holder_count"] = solscan_data["total"]
        signal["top10_holder_pct"] = solscan_data["top10_pct"]
    except:
        pass
    
    return signal
```

## Whale Discovery Priority

Seed list → auto-expansion using:
1. **Co-buyer graph** (Helius): When seed whale buys X, find other wallets buying X in first 5-15min
2. **Front-runner discovery**: Wallets that bought before our winning trades
3. **Performance gating**: Promote candidates only after 5+ trades, 55% WR, 3% ROI

**Current status:**
- 13 seed wallets from XLSX ✅
- Whale tracker operational ✅
- Auto-discovery v2 deployed ✅
- Needs real trades to start expanding

## Rate Limits & Caching

| API | Limit | Cache TTL | Status |
|-----|-------|-----------|--------|
| Binance | 1200/min | 60s | ✅ Cached |
| Birdeye | 100/min (free tier) | 60s | TODO |
| Solscan | No official limit | 300s | TODO |
| Helius | 1000/min (paid tier) | N/A (real-time) | ✅ No cache |
| CoinGecko | 50/min (free) | N/A (discovery) | ✅ No cache needed |

## Key Principle

**Use the API that matches your execution venue:**
- Trading on Binance? Use Binance data.
- Trading on Raydium? Use Birdeye data.
- Never mix venue data for the same decision.

**Corollary:**
- CoinGecko = discovery only
- Binance = CEX execution data
- Birdeye/Solscan = DEX execution data
- Helius = wallet intelligence
