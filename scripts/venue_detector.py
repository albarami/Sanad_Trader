#!/usr/bin/env python3
"""
Venue Detection - Single source of truth for CEX vs DEX classification

Used by: sanad_pipeline (Stage 6 + Stage 7), paper_execution, OMS
Ensures consistent venue detection across entire execution path.
"""

# Canonical DEX exchange list
DEX_EXCHANGES = {
    "raydium", "orca", "jupiter",           # Solana DEXes
    "uniswap", "sushiswap", "curve",        # Ethereum DEXes
    "pancakeswap", "biswap",                # BSC DEXes
    "quickswap",                             # Polygon DEXes
    "trader-joe", "pangolin",               # Avalanche DEXes
    "spiritswap", "spookyswap",             # Fantom DEXes
}

def detect_venue(token_profile: dict, signal: dict = None) -> dict:
    """
    Detect venue (CEX vs DEX) from token profile.
    
    Args:
        token_profile: Token metadata from strategy_result or signal
        signal: Optional signal dict for fallback data
        
    Returns:
        dict with:
            - venue: "CEX" or "DEX"
            - exchange: best exchange name
            - is_dex: bool
            - detection_reason: why this venue was chosen
    """
    
    chain = token_profile.get("chain", "").lower()
    dex_only = token_profile.get("dex_only", False)
    exchange_list = token_profile.get("cex_names", [])  # Misnomer: includes DEXes
    
    # Normalize exchange names to lowercase
    exchange_list_lower = [ex.lower() for ex in exchange_list]
    
    # Check if ALL exchanges are DEXes
    has_dex_only = (
        bool(exchange_list_lower) and 
        all(ex in DEX_EXCHANGES for ex in exchange_list_lower)
    )
    
    # Check if ANY CEX is present
    cex_names = {"binance", "coinbase", "kraken", "bybit", "okx", "mexc", "kucoin", "gate", "huobi"}
    has_cex = any(ex in cex_names for ex in exchange_list_lower)
    
    # Detect DEX
    is_dex = False
    reason = ""
    
    if dex_only:
        is_dex = True
        reason = "token_profile.dex_only=True"
    elif has_dex_only:
        is_dex = True
        reason = f"all exchanges are DEX: {exchange_list}"
    elif chain in ("solana", "ethereum", "base", "polygon", "avalanche", "fantom", "bsc") and not has_cex:
        is_dex = True
        reason = f"chain={chain} with no CEX exchanges"
    else:
        is_dex = False
        reason = f"has CEX or unknown: {exchange_list}"
    
    # Determine exchange
    if is_dex:
        # Pick first DEX exchange, or default by chain
        if exchange_list_lower:
            exchange = exchange_list[0]  # Original case
        else:
            # Default DEX by chain
            chain_defaults = {
                "solana": "raydium",
                "ethereum": "uniswap",
                "base": "uniswap",
                "bsc": "pancakeswap",
                "polygon": "quickswap",
                "avalanche": "trader-joe",
                "fantom": "spiritswap",
            }
            exchange = chain_defaults.get(chain, "raydium")
    else:
        # CEX: prefer binance, fallback to first available
        if "binance" in exchange_list_lower:
            exchange = "binance"
        elif exchange_list:
            # Pick first non-DEX exchange
            for ex_orig, ex_lower in zip(exchange_list, exchange_list_lower):
                if ex_lower not in DEX_EXCHANGES:
                    exchange = ex_orig
                    break
            else:
                exchange = "binance"  # Fallback
        else:
            exchange = "binance"  # Default
    
    return {
        "venue": "DEX" if is_dex else "CEX",
        "exchange": exchange,
        "is_dex": is_dex,
        "detection_reason": reason,
    }


def get_price_from_decision_data(signal: dict, strategy_result: dict = None, decision_record: dict = None) -> float:
    """
    Extract price from decision data in priority order.
    Never calls external APIs.
    
    Priority:
    1. decision_record.execution.current_price
    2. decision_record.strategy.current_price
    3. strategy_result.current_price
    4. signal.price
    
    Returns:
        float price or None if not found
    """
    
    if decision_record:
        # Highest priority: already-validated price from decision record
        exec_price = decision_record.get("execution", {}).get("current_price")
        if exec_price and exec_price > 0:
            return exec_price
        
        strat_price = decision_record.get("strategy", {}).get("current_price")
        if strat_price and strat_price > 0:
            return strat_price
    
    if strategy_result:
        strat_price = strategy_result.get("current_price")
        if strat_price and strat_price > 0:
            return strat_price
    
    if signal:
        # Try multiple price field names (different sources use different keys)
        for price_key in ["price", "current_price", "current_price_usd", "lastPrice", "price_usd"]:
            signal_price = signal.get(price_key)
            if signal_price and isinstance(signal_price, (int, float)) and signal_price > 0:
                return signal_price
    
    return None


if __name__ == "__main__":
    # Test cases
    print("Testing venue detection...\n")
    
    # Test 1: DEX-only token (BP on Raydium)
    bp_profile = {
        "chain": "solana",
        "dex_only": False,
        "cex_names": ["raydium"],
    }
    result = detect_venue(bp_profile)
    print(f"BP (Raydium): {result}")
    assert result["venue"] == "DEX", "BP should be DEX"
    assert result["exchange"] == "raydium", "BP should use Raydium"
    
    # Test 2: CEX token (BTC)
    btc_profile = {
        "chain": "",
        "dex_only": False,
        "cex_names": ["binance", "coinbase", "kraken"],
    }
    result = detect_venue(btc_profile)
    print(f"BTC (CEX): {result}")
    assert result["venue"] == "CEX", "BTC should be CEX"
    assert result["exchange"] == "binance", "BTC should use Binance"
    
    # Test 3: Mixed token (has both DEX and CEX)
    mixed_profile = {
        "chain": "solana",
        "dex_only": False,
        "cex_names": ["binance", "raydium"],
    }
    result = detect_venue(mixed_profile)
    print(f"Mixed (CEX+DEX): {result}")
    assert result["venue"] == "CEX", "Mixed should prefer CEX"
    assert result["exchange"] == "binance", "Mixed should use Binance"
    
    # Test 4: Solana token with no exchanges listed
    sol_unknown = {
        "chain": "solana",
        "dex_only": False,
        "cex_names": [],
    }
    result = detect_venue(sol_unknown)
    print(f"Solana unknown: {result}")
    assert result["venue"] == "DEX", "Solana with no CEX should be DEX"
    assert result["exchange"] == "raydium", "Should default to Raydium"
    
    # Test 5: Price extraction
    signal = {"price": 0.0057}
    strategy = {"current_price": 0.0058}
    decision = {"strategy": {"current_price": 0.0059}}
    
    price = get_price_from_decision_data(signal)
    print(f"\nPrice from signal: {price}")
    assert price == 0.0057
    
    price = get_price_from_decision_data(signal, strategy)
    print(f"Price from strategy: {price}")
    assert price == 0.0058
    
    price = get_price_from_decision_data(signal, strategy, decision)
    print(f"Price from decision: {price}")
    assert price == 0.0059
    
    print("\n✅ All tests passed!")
