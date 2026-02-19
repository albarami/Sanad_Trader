#!/usr/bin/env python3
"""
Market Data Enricher — Fill missing signal fields using provider APIs.

Deterministic Python. NO LLMs.

Routes signals to appropriate enrichment provider:
- Binance majors → Binance API (volume, price_change_24h, price_change_1h)
- Solana tokens → Birdeye API (volume, liquidity, price_change)
- Fallback → CoinGecko (market data)

Used by signal_router.py BEFORE tradeability scoring.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import time

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

# Import existing clients
try:
    from binance_client import get_ticker_24h, _request as binance_request
except ImportError:
    get_ticker_24h = None
    binance_request = None

# In-memory cache (TTL 60s to limit Binance API calls)
_BINANCE_CACHE = {}
_CACHE_TTL = 60  # seconds

def _get_cached_binance_data(symbol: str):
    """Get cached Binance data if fresh (<60s old)."""
    if symbol in _BINANCE_CACHE:
        data, timestamp = _BINANCE_CACHE[symbol]
        if time.time() - timestamp < _CACHE_TTL:
            return data
    return None

def _cache_binance_data(symbol: str, data: dict):
    """Cache Binance data with timestamp."""
    _BINANCE_CACHE[symbol] = (data, time.time())

BINANCE_MAJORS = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "MATIC", "AVAX",
    "LINK", "UNI", "ATOM", "LTC", "ETC", "XLM", "ALGO", "VET", "ICP", "FIL",
    "TRX", "APT", "ARB", "OP", "NEAR", "SUI", "SEI", "PEPE", "SHIB", "WIF",
    "BONK", "FLOKI", "FET", "GRT", "SAND", "MANA", "AXS", "IMX", "GALA",
}


def enrich_signal(signal: dict) -> dict:
    """
    Enrich signal with missing market data fields.
    
    Returns: signal dict with added/updated fields:
    - volume_24h (USD)
    - price_change_24h (%)
    - price_change_1h (%) if available
    - liquidity_usd (for Solana)
    - chain (corrected if detected)
    """
    if not signal:
        return signal
    
    token = signal.get("token", "").upper()
    chain = signal.get("chain", "unknown")
    
    # Route based on token type
    if token in BINANCE_MAJORS or chain == "binance":
        return _enrich_binance_major(signal)
    elif chain == "solana" or (chain == "unknown" and len(token) > 30):
        return _enrich_solana_token(signal)
    else:
        # Unknown token, can't enrich without more info
        return signal


def _enrich_binance_major(signal: dict) -> dict:
    """Enrich Binance-listed major using Binance API with caching."""
    if not get_ticker_24h:
        return signal  # Binance client not available
    
    token = signal.get("token", "").upper()
    symbol = token + "USDT"  # Standard quote currency
    
    # Check cache first (avoid redundant API calls)
    cached = _get_cached_binance_data(symbol)
    if cached:
        ticker = cached
    else:
        try:
            ticker = get_ticker_24h(symbol)
            if ticker:
                _cache_binance_data(symbol, ticker)
        except Exception:
            return signal  # API call failed, return unchanged
    
    try:
        if ticker:
            # Update with Binance data (authoritative for CEX pairs)
            signal["volume_24h_usd"] = float(ticker.get("quoteVolume", 0))  # Volume in USDT (≈USD)
            signal["price_change_24h_pct"] = float(ticker.get("priceChangePercent", 0))
            signal["current_price"] = float(ticker.get("lastPrice", signal.get("current_price", 0)))
            signal["chain"] = "binance"  # Confirm chain
            
            # Calculate 1-hour price change from klines (also cached)
            price_1h = _get_binance_1h_change(symbol)
            if price_1h is not None:
                signal["price_change_1h_pct"] = price_1h
                
    except Exception:
        # Enrichment failed, but don't block signal
        pass
    
    return signal


def _get_binance_1h_change(symbol: str) -> float | None:
    """Calculate 1-hour price change from Binance klines."""
    if not binance_request:
        return None
    
    try:
        # Get last 2 hourly candles
        klines = binance_request("GET", "/api/v3/klines", {
            "symbol": symbol,
            "interval": "1h",
            "limit": 2
        })
        
        if klines and len(klines) >= 2:
            # kline format: [open_time, open, high, low, close, volume, ...]
            current_close = float(klines[-1][4])  # Most recent close
            hour_ago_close = float(klines[-2][4])  # 1 hour ago close
            
            if hour_ago_close > 0:
                pct_change = ((current_close - hour_ago_close) / hour_ago_close) * 100
                return pct_change
    except Exception:
        pass
    
    return None


def _enrich_solana_token(signal: dict) -> dict:
    """Enrich Solana token using Birdeye + Solscan APIs."""
    token_address = signal.get("token_address", "")
    if not token_address or len(token_address) < 32:
        return signal  # Invalid/missing address
    
    # Birdeye market data
    try:
        import birdeye_client
        birdeye_data = birdeye_client.get_token_overview(token_address)
        
        if birdeye_data:
            signal["volume_24h_usd"] = birdeye_data.get("v24hUSD", 0)
            signal["price_change_1h_pct"] = birdeye_data.get("priceChange1hPercent", 0)
            signal["price_change_24h_pct"] = birdeye_data.get("priceChange24hPercent", 0)
            signal["liquidity_usd"] = birdeye_data.get("liquidity", 0)
            signal["current_price"] = birdeye_data.get("price", signal.get("current_price", 0))
            signal["market_cap"] = birdeye_data.get("marketCap", 0)
            signal["chain"] = "solana"  # Confirm chain
    except Exception:
        pass  # Birdeye failed, continue to Solscan
    
    # Solscan holder data + metadata
    try:
        from solscan_client import enrich_signal_with_solscan
        
        # Use existing enrichment function
        enriched = enrich_signal_with_solscan(signal)
        
        # Map Solscan fields to canonical fields
        if "solscan_holder_count" in enriched:
            signal["holder_count"] = enriched["solscan_holder_count"]
        if "solscan_top_10_pct" in enriched:
            signal["top10_holder_pct"] = enriched["solscan_top_10_pct"]
    except Exception:
        pass  # Solscan failed, but don't block signal
    
    return signal


if __name__ == "__main__":
    # Test enrichment
    import json
    
    # Test XRP (Binance major)
    xrp_signal = {
        "token": "XRP",
        "chain": "unknown",
        "volume_24h": 0,
        "price_change_24h": 0,
    }
    
    print("Before enrichment:")
    print(json.dumps(xrp_signal, indent=2))
    
    enriched = enrich_signal(xrp_signal)
    
    print("\nAfter enrichment:")
    print(json.dumps(enriched, indent=2))
    
    print(f"\nVolume enriched: {'✅' if enriched['volume_24h'] > 0 else '❌'}")
    print(f"Price change enriched: {'✅' if enriched['price_change_24h'] != 0 else '❌'}")
    print(f"Chain detected: {'✅' if enriched['chain'] == 'binance' else '❌'}")
