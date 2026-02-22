#!/usr/bin/env python3
"""
Stablecoin Filter — Universal blocking for stablecoins across all signal sources.
Deterministic Python, no LLMs.
"""

# Solana stablecoin addresses (canonical)
STABLECOIN_ADDRESSES = {
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT (Tether)
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC (Circle)
    "EjmyN6qEC1Tf1JxiG1ae7UTJhUxSwk1TCWNWqxWV4J6o",  # DAI
    "AJ1W9A9N9dEMdVyoDiam2rV44gnBm2csrPDP7xqcapgX",  # BUSD (Binance USD, deprecated)
    "7kbnvuGBxxj8AG9qp8Scn56muWGaRaFqxg1FsRp3PaFT",  # UXD
    "BXXkv6z8ykpG1yuvUDPgh732wzVHB69RnB9YgSYh3itW",  # USDC (Wormhole)
    "Ea5SjE2Y6yvCeW5dYTn7PYMuW5ikXkvbGdcmSnXeaLjS",  # PAX Gold (technically not stablecoin, but not tradeable)
}

# Symbol-based stablecoin detection (case-insensitive)
STABLECOIN_SYMBOLS = {
    "USDT", "USDC", "DAI", "BUSD", "USDD", "TUSD", "FRAX",
    "USDP", "GUSD", "PAX", "UST", "LUSD", "SUSD", "CUSD",
    "UXD", "USDB", "DOLA", "USDR", "USDX", "USDS", "USD1",
    "USDJ", "USDK", "USDQ", "MIM", "FEI", "USDH", "CASH"
}

def is_stablecoin(token: str = None, symbol: str = None, address: str = None) -> bool:
    """
    Check if token is a stablecoin by address or symbol.
    Returns True if stablecoin detected.
    
    Args:
        token: Token symbol (legacy field)
        symbol: Token symbol (preferred field)
        address: Token contract address (most reliable)
    """
    # Address check (most reliable)
    if address and address in STABLECOIN_ADDRESSES:
        return True
    
    # Symbol check (fallback)
    check_symbol = symbol or token
    if check_symbol:
        check_upper = check_symbol.upper().strip()
        # Exact match
        if check_upper in STABLECOIN_SYMBOLS:
            return True
        # Starts with stablecoin name (catches USDT-8, USDC.e, etc.)
        if any(check_upper.startswith(sc) for sc in STABLECOIN_SYMBOLS):
            return True
    
    return False

def filter_signals(signals: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Filter out stablecoins from signal list.
    Returns (valid_signals, blocked_signals)
    """
    valid = []
    blocked = []
    
    for signal in signals:
        if is_stablecoin(
            token=signal.get("token"),
            symbol=signal.get("symbol"),
            address=signal.get("token_address")
        ):
            blocked.append({
                **signal,
                "block_reason": "STABLECOIN_FILTER",
                "blocked_at": signal.get("timestamp")
            })
        else:
            valid.append(signal)
    
    return valid, blocked

if __name__ == "__main__":
    # Test cases
    tests = [
        {"token": "USDT", "address": None, "expect_block": True},
        {"token": "BTC", "address": None, "expect_block": False},
        {"token": None, "address": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "expect_block": True},
        {"token": "USDC.e", "address": None, "expect_block": True},
        {"token": "SOL", "address": None, "expect_block": False},
    ]
    
    for test in tests:
        result = is_stablecoin(token=test["token"], address=test["address"])
        status = "✓" if result == test["expect_block"] else "✗"
        print(f"{status} {test['token'] or test['address'][:8]}: blocked={result}")
