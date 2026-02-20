#!/usr/bin/env python3
"""
CEX Price Provider — Exchange-Agnostic Pricing with Fallback Chain
Eliminates "Binance is down" excuse by implementing deterministic fallback.

Priority order:
1. Binance (if circuit breaker closed)
2. MEXC (if circuit breaker closed)  
3. Local price cache (if fresh < 2 minutes)
4. Signal price (last resort from signal data)
5. Fail closed (return None only if ALL sources unavailable)

Used by:
- Stage 6 (Policy Engine) for price/spread/slippage
- Stage 7 (Execution) for order placement
- Any module requiring CEX price data
"""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
STATE_DIR = BASE_DIR / "state"
CACHE_FILE = STATE_DIR / "price_cache.json"
CIRCUIT_BREAKERS_FILE = STATE_DIR / "circuit_breakers.json"

# Cache TTL (seconds)
CACHE_TTL_SEC = 120  # 2 minutes

# ---------------------------------------------------------------------------
# Circuit Breaker Check
# ---------------------------------------------------------------------------
def _is_breaker_closed(exchange: str) -> bool:
    """Check if circuit breaker is closed (exchange available)."""
    try:
        if not CIRCUIT_BREAKERS_FILE.exists():
            return True  # No breaker file = assume available
        
        breakers = json.load(open(CIRCUIT_BREAKERS_FILE))
        breaker = breakers.get(f"{exchange}_api", {})
        state = breaker.get("state", "closed")
        return state == "closed"
    except:
        return True  # Default to available on error


# ---------------------------------------------------------------------------
# Exchange Clients
# ---------------------------------------------------------------------------
def _get_binance_price(symbol: str) -> Optional[float]:
    """Get price from Binance API."""
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR / "scripts"))
        from binance_client import get_price
        
        price = get_price(symbol)
        if price and price > 0:
            return float(price)
    except Exception as e:
        print(f"[CEX_PRICE] Binance failed: {e}")
    return None


def _get_mexc_price(symbol: str) -> Optional[float]:
    """Get price from MEXC API."""
    try:
        import requests
        
        # MEXC uses same symbol format (BTCUSDT, ETHUSDT, etc.)
        resp = requests.get(
            f"https://api.mexc.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=(5, 10)
        )
        
        if resp.status_code == 200:
            data = resp.json()
            price = float(data.get("price", 0))
            if price > 0:
                return price
    except Exception as e:
        print(f"[CEX_PRICE] MEXC failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Price Cache
# ---------------------------------------------------------------------------
def _get_cached_price(symbol: str) -> Optional[float]:
    """Get price from local cache if fresh."""
    try:
        if not CACHE_FILE.exists():
            return None
        
        cache = json.load(open(CACHE_FILE))
        entry = cache.get(symbol)
        
        if not entry:
            return None
        
        timestamp = entry.get("timestamp")
        price = entry.get("price")
        
        if not timestamp or not price:
            return None
        
        # Check freshness
        cache_time = datetime.fromisoformat(timestamp)
        age_sec = (datetime.now(timezone.utc) - cache_time).total_seconds()
        
        if age_sec < CACHE_TTL_SEC:
            return float(price)
    except Exception as e:
        print(f"[CEX_PRICE] Cache read failed: {e}")
    return None


def _write_cache(symbol: str, price: float, source: str):
    """Write price to cache."""
    try:
        cache = {}
        if CACHE_FILE.exists():
            try:
                cache = json.load(open(CACHE_FILE))
            except:
                pass
        
        cache[symbol] = {
            "price": price,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"[CEX_PRICE] Cache write failed: {e}")


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------
def get_price(
    symbol: str,
    fallback_price: Optional[float] = None,
    preferred_exchange: Optional[str] = None
) -> Optional[float]:
    """
    Get CEX price with fallback chain.
    
    Args:
        symbol: Trading symbol (e.g., "BTCUSDT", "ETHUSDT")
        fallback_price: Last resort price from signal data
        preferred_exchange: Try this exchange first ("binance" or "mexc")
    
    Returns:
        Price as float, or None if all sources failed
    """
    # Build priority list
    exchanges = []
    if preferred_exchange == "mexc":
        exchanges = ["mexc", "binance"]
    else:
        exchanges = ["binance", "mexc"]
    
    # Try exchanges in priority order
    for exchange in exchanges:
        if not _is_breaker_closed(exchange):
            print(f"[CEX_PRICE] {exchange.capitalize()} circuit breaker open - skipping")
            continue
        
        if exchange == "binance":
            price = _get_binance_price(symbol)
        elif exchange == "mexc":
            price = _get_mexc_price(symbol)
        else:
            continue
        
        if price:
            _write_cache(symbol, price, exchange)
            return price
    
    # Try cache
    cached_price = _get_cached_price(symbol)
    if cached_price:
        print(f"[CEX_PRICE] Using cached price for {symbol}: ${cached_price}")
        return cached_price
    
    # Try fallback price
    if fallback_price and fallback_price > 0:
        print(f"[CEX_PRICE] Using fallback price for {symbol}: ${fallback_price}")
        return fallback_price
    
    # All sources failed
    print(f"[CEX_PRICE] ALL SOURCES FAILED for {symbol}")
    return None


def get_orderbook(
    symbol: str,
    preferred_exchange: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Get order book with fallback.
    
    Returns:
        {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        or None if unavailable
    """
    exchanges = []
    if preferred_exchange == "mexc":
        exchanges = ["mexc", "binance"]
    else:
        exchanges = ["binance", "mexc"]
    
    for exchange in exchanges:
        if not _is_breaker_closed(exchange):
            continue
        
        try:
            if exchange == "binance":
                import sys
                sys.path.insert(0, str(BASE_DIR / "scripts"))
                from binance_client import get_order_book
                book = get_order_book(symbol, limit=20)
                if book:
                    return book
            
            elif exchange == "mexc":
                import requests
                resp = requests.get(
                    f"https://api.mexc.com/api/v3/depth",
                    params={"symbol": symbol, "limit": 20},
                    timeout=(5, 10)
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "bids": data.get("bids", []),
                        "asks": data.get("asks", [])
                    }
        except Exception as e:
            print(f"[CEX_PRICE] {exchange.capitalize()} orderbook failed: {e}")
            continue
    
    return None


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------
def get_spread_bps(symbol: str) -> Optional[float]:
    """Get bid-ask spread in basis points."""
    book = get_orderbook(symbol)
    if not book:
        return None
    
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    
    if not bids or not asks:
        return None
    
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_bps = (spread / mid) * 10000
        return spread_bps
    except:
        return None


def get_slippage_estimate(
    symbol: str,
    quantity_usd: float,
    side: str = "BUY"
) -> Optional[float]:
    """
    Estimate slippage percentage for a given order size.
    
    Args:
        symbol: Trading symbol
        quantity_usd: Order size in USD
        side: "BUY" or "SELL"
    
    Returns:
        Estimated slippage as percentage (e.g., 0.15 for 0.15%)
    """
    book = get_orderbook(symbol)
    if not book:
        # Conservative default when orderbook unavailable
        return 0.5  # 50 bps = 0.5%
    
    try:
        levels = book.get("asks" if side == "BUY" else "bids", [])
        if not levels:
            return 0.5
        
        cumulative_usd = 0
        weighted_price = 0
        total_qty = 0
        
        for price_str, qty_str in levels:
            price = float(price_str)
            qty = float(qty_str)
            level_usd = price * qty
            
            if cumulative_usd + level_usd >= quantity_usd:
                # This level fills the order
                remaining = quantity_usd - cumulative_usd
                remaining_qty = remaining / price
                weighted_price += price * remaining_qty
                total_qty += remaining_qty
                break
            else:
                weighted_price += price * qty
                total_qty += qty
                cumulative_usd += level_usd
        
        if total_qty > 0:
            avg_price = weighted_price / total_qty
            ref_price = float(levels[0][0])
            slippage_pct = abs((avg_price - ref_price) / ref_price) * 100
            return slippage_pct
        else:
            return 0.5
    except:
        return 0.5


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python3 cex_price_provider.py SYMBOL [FALLBACK_PRICE]")
        sys.exit(1)
    
    symbol = sys.argv[1]
    fallback = float(sys.argv[2]) if len(sys.argv) > 2 else None
    
    print(f"\nTesting CEX price provider for {symbol}")
    print("="*60)
    
    price = get_price(symbol, fallback_price=fallback)
    print(f"\nPrice: ${price}" if price else "\nPrice: UNAVAILABLE")
    
    spread = get_spread_bps(symbol)
    print(f"Spread: {spread:.2f} bps" if spread else "Spread: UNAVAILABLE")
    
    slippage = get_slippage_estimate(symbol, quantity_usd=1000, side="BUY")
    print(f"Slippage (1K USD): {slippage:.4f}%" if slippage else "Slippage: UNAVAILABLE")
