#!/usr/bin/env python3
"""
Signal Normalizer — Canonical Source Key Generation

Problem: Signal sources come in as human-readable strings:
- "DexScreener boost (100x) + Birdeye trending"
- "CoinGecko trending #15"
- "coingecko_trending"
- "Birdeye trending"

This breaks UCB1 learning because keys don't match across signals.

Solution: Normalize all sources to canonical keys: {provider}:{variant}
- "dexscreener:boost"
- "coingecko:trending"
- "birdeye:trending"
- "onchain:whale_alert"

Usage:
    from signal_normalizer import canonical_source
    
    result = canonical_source("DexScreener boost (100x) + Birdeye trending")
    # → {"provider": "dexscreener", "variant": "boost", "source_key": "dexscreener:boost"}
    
    result = canonical_source("CoinGecko trending #15")
    # → {"provider": "coingecko", "variant": "trending", "source_key": "coingecko:trending"}
"""

import re
from typing import Dict, List

# Supported providers
PROVIDERS = {
    "coingecko": ["coingecko", "cg", "gecko"],
    "dexscreener": ["dexscreener", "dex", "screener"],
    "birdeye": ["birdeye", "bird"],
    "onchain": ["onchain", "helius", "solscan"],
    "telegram": ["telegram", "tg", "ct"],
    "sentiment": ["sentiment", "fud", "hype"],
    "pumpfun": ["pumpfun", "pump"],
    "binance": ["binance", "bnb", "cex"],
}

# Supported variants
VARIANTS = {
    "trending": ["trending", "trend", "hot"],
    "boost": ["boost", "boosted", "promoted"],
    "gainers": ["gainers", "gainer", "winner", "top"],
    "whale_alert": ["whale", "whale_alert", "large_tx"],
    "new_listing": ["new_listing", "new", "launch", "ilo"],
    "community_takeover": ["community_takeover", "cto", "takeover"],
    "meme_radar": ["meme_radar", "meme", "radar"],
    "volume_spike": ["volume", "volume_spike", "vol"],
}


def canonical_source(raw_source: str) -> Dict[str, str]:
    """
    Parse raw source string and return canonical representation.
    
    Args:
        raw_source: Raw source string (e.g., "DexScreener boost (100x) + Birdeye trending")
    
    Returns:
        {
            "provider": "dexscreener",
            "variant": "boost",
            "source_key": "dexscreener:boost",
            "raw": original raw_source
        }
    
    Rules:
        - If multiple providers detected (e.g., "DexScreener + Birdeye"), use first provider
        - If no variant detected, use "general"
        - Case-insensitive matching
    """
    raw_lower = raw_source.lower()
    
    # Detect provider
    detected_provider = None
    for canonical, aliases in PROVIDERS.items():
        for alias in aliases:
            if alias in raw_lower:
                detected_provider = canonical
                break
        if detected_provider:
            break
    
    if not detected_provider:
        detected_provider = "unknown"
    
    # Detect variant
    detected_variant = None
    for canonical, aliases in VARIANTS.items():
        for alias in aliases:
            if alias in raw_lower:
                detected_variant = canonical
                break
        if detected_variant:
            break
    
    if not detected_variant:
        detected_variant = "general"
    
    source_key = f"{detected_provider}:{detected_variant}"
    
    return {
        "provider": detected_provider,
        "variant": detected_variant,
        "source_key": source_key,
        "raw": raw_source,
    }


def canonical_sources_multi(raw_source: str) -> List[Dict[str, str]]:
    """
    Parse compound sources (e.g., "DexScreener + Birdeye").
    Returns list of canonical source dicts.
    
    Args:
        raw_source: Raw source string
    
    Returns:
        List of canonical source dicts
    """
    # Split on common delimiters
    parts = re.split(r'\s*[+&,]\s*', raw_source)
    
    results = []
    seen_keys = set()
    
    for part in parts:
        if not part.strip():
            continue
        
        canonical = canonical_source(part)
        # Deduplicate
        if canonical["source_key"] not in seen_keys:
            results.append(canonical)
            seen_keys.add(canonical["source_key"])
    
    return results if results else [canonical_source(raw_source)]


def test():
    """Test canonical_source with real examples."""
    test_cases = [
        "DexScreener boost (100x) + Birdeye trending",
        "CoinGecko trending #15",
        "coingecko_trending",
        "Birdeye trending",
        "Birdeye meme gainer",
        "DexScreener boost (50x) + Birdeye meme gainer + Birdeye meme gainer",
        "onchain_whale_alert",
        "telegram_ct_chatter",
        "pumpfun_graduation",
    ]
    
    print("=== CANONICAL SOURCE KEY TESTS ===\n")
    
    for raw in test_cases:
        result = canonical_source(raw)
        print(f"Raw: {raw}")
        print(f"  → {result['source_key']} (provider={result['provider']}, variant={result['variant']})")
    
    print("\n=== MULTI-SOURCE TESTS ===\n")
    
    multi_cases = [
        "DexScreener boost (100x) + Birdeye trending",
        "coingecko + birdeye + onchain",
    ]
    
    for raw in multi_cases:
        results = canonical_sources_multi(raw)
        print(f"Raw: {raw}")
        for r in results:
            print(f"  → {r['source_key']}")


if __name__ == "__main__":
    test()
