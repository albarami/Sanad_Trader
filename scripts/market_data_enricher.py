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

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

# Import existing clients
try:
    from binance_client import get_ticker_24h, _request as binance_request
except ImportError:
    get_ticker_24h = None
    binance_request = None

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
    """Enrich Binance-listed major using Binance API."""
    if not get_ticker_24h:
        return signal  # Binance client not available
    
    token = signal.get("token", "").upper()
    symbol = token + "USDT"  # Standard quote currency
    
    try:
        ticker = get_ticker_24h(symbol)
        if ticker:
            # Update with Binance data (authoritative for CEX pairs)
            signal["volume_24h"] = float(ticker.get("quoteVolume", 0))  # Volume in USDT
            signal["price_change_24h"] = float(ticker.get("priceChangePercent", 0))
            signal["current_price"] = float(ticker.get("lastPrice", signal.get("current_price", 0)))
            signal["chain"] = "binance"  # Confirm chain
            
            # Calculate 1-hour price change from klines
            price_1h = _get_binance_1h_change(symbol)
            if price_1h is not None:
                signal["price_change_1h"] = price_1h
                
    except Exception as e:
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
    """
    Enrich Solana token using Birdeye API.
    
    TODO: Requires Birdeye API key and client.
    For now, return signal unchanged (Solana enrichment Sprint 4).
    """
    # Placeholder for Birdeye integration
    # Would call: GET /defi/token_overview?address={CA}
    # Returns: price, volume24h, liquidity, priceChange24hPercent, priceChange1hPercent
    
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
