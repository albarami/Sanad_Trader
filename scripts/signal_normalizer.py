#!/usr/bin/env python3
"""
Signal Normalizer — Unified Module

Part 1: Schema Normalization (normalize_signal)
Converts 7 different signal source formats → canonical schema

Part 2: Source Key Canonicalization (canonical_source)
Converts messy source strings → consistent provider:variant keys for UCB1
"""

import json
import os
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
sys.path.insert(0, str(SCRIPT_DIR))


# ═════════════════════════════════════════════════════════
# PART 1: SCHEMA NORMALIZATION
# ═════════════════════════════════════════════════════════

REQUIRED_FIELDS = {"token", "source", "direction", "timestamp"}

CANONICAL_DEFAULTS = {
    "token": "",
    "source": "",
    "direction": "LONG",
    "chain": "",
    "token_address": "",
    "thesis": "",
    "timestamp": "",
    "score": 0,
    "volume_24h": 0,
    "market_cap": 0,
    "price_change_24h": 0,
    "symbol": "",
    "holder_count": 0,
    "liquidity": 0,
    "source_detail": "",
}

# Binance-listed majors (for automatic chain detection)
BINANCE_MAJORS = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "MATIC", "AVAX",
    "LINK", "UNI", "ATOM", "LTC", "ETC", "XLM", "ALGO", "VET", "ICP", "FIL",
    "TRX", "APT", "ARB", "OP", "NEAR", "SUI", "SEI", "PEPE", "SHIB", "WIF",
    "BONK", "FLOKI", "FET", "GRT", "SAND", "MANA", "AXS", "IMX", "GALA",
}

def _detect_chain(token: str) -> str:
    """Detect chain from token symbol."""
    if token.upper() in BINANCE_MAJORS:
        return "binance"
    # Length check for Solana contract addresses
    if len(token) > 30:  # Likely a Solana address
        return "solana"
    return "unknown"


def _detect_source(raw: dict) -> str:
    """Detect signal source type from structure."""
    if "fear_greed_index" in raw:
        return "fear_greed"
    if "narrative" in raw and "momentum" in raw:
        return "sentiment"
    if "volume_24h_usd" in raw or "birdeye_rank" in raw:
        return "birdeye"
    if "coingecko_rank" in raw or "gecko_terminal" in raw:
        return "coingecko"
    if "dexscreener_pair" in raw or "boost_count" in raw:
        return "dexscreener"
    if "rugcheck_score" in raw:
        return "rugcheck"
    if "chain" in raw and "token_address" in raw:
        return "onchain"
    if "telegram_mentions" in raw or "ct_sentiment" in raw:
        return "telegram"
    return "generic"


def normalize_signal(raw: dict, source_hint: str = "") -> dict | None:
    """
    Normalize any signal format to canonical schema.
    Returns None if signal is not normalizable.
    """
    if not raw or not isinstance(raw, dict):
        return None

    source_type = source_hint or _detect_source(raw)

    # Route to normalizer
    normalizers = {
        "sentiment": lambda r: {
            "token": r.get("token", ""),
            "source": "sentiment",
            "direction": "LONG",
            "thesis": r.get("narrative", ""),
            "timestamp": r.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "score": int(r.get("momentum", 0) * 100),
        },
        "birdeye": lambda r: {
            "token": r.get("symbol", ""),
            "source": "birdeye",
            "direction": "LONG",
            "volume_24h": r.get("volume_24h_usd", 0),
            "price_change_24h": r.get("price_change_24h_percent", 0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "coingecko": lambda r: {
            "token": r.get("token", r.get("symbol", "")).upper(),
            "source": "coingecko",
            "direction": "LONG",
            "chain": _detect_chain(r.get("token", r.get("symbol", "")).upper()),
            "market_cap": r.get("market_cap", 0),
            "volume_24h": r.get("volume_24h", r.get("total_volume", 0)),
            "price_change_24h": r.get("price_change_24h_pct", r.get("price_change_percentage_24h", 0)),
            "price_change_1h": r.get("price_change_1h_pct", 0),
            "current_price": r.get("current_price", 0),
            "timestamp": r.get("timestamp", datetime.now(timezone.utc).isoformat()),
        },
        "dexscreener": lambda r: {
            "token": r.get("baseToken", {}).get("symbol", ""),
            "source": "dexscreener",
            "direction": "LONG",
            "token_address": r.get("baseToken", {}).get("address", ""),
            "chain": r.get("chainId", "solana"),
            "liquidity": r.get("liquidity", {}).get("usd", 0),
            "volume_24h": r.get("volume", {}).get("h24", 0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "onchain": lambda r: {
            "token": r.get("token", ""),
            "source": "onchain",
            "direction": "LONG",
            "token_address": r.get("token_address", ""),
            "chain": r.get("chain", "solana"),
            "holder_count": r.get("holder_count", 0),
            "timestamp": r.get("timestamp", datetime.now(timezone.utc).isoformat()),
        },
        "generic": lambda r: {
            "token": r.get("token", r.get("symbol", "")),
            "source": r.get("source", "unknown"),
            "direction": r.get("direction", "LONG"),
            "timestamp": r.get("timestamp", datetime.now(timezone.utc).isoformat()),
        },
    }

    normalizer = normalizers.get(source_type, normalizers["generic"])
    result = normalizer(raw)

    if not result:
        return None

    # Merge with defaults
    canonical = {**CANONICAL_DEFAULTS, **result}

    # Validate
    if not canonical["token"] or not canonical["source"]:
        return None

    return canonical


# ═════════════════════════════════════════════════════════
# PART 2: SOURCE KEY CANONICALIZATION (for UCB1)
# ═════════════════════════════════════════════════════════

PROVIDERS = {
    "coingecko": ["coingecko", "cg", "gecko"],
    "dexscreener": ["dexscreener", "dex", "screener"],
    "birdeye": ["birdeye", "bird"],
    "onchain": ["onchain", "helius", "solscan", "whale_tracker"],
    "telegram": ["telegram", "tg", "ct"],
    "sentiment": ["sentiment", "fud", "hype"],
    "pumpfun": ["pumpfun", "pump"],
    "binance": ["binance", "bnb", "cex", "majors_scanner"],
}

VARIANTS = {
    "trending": ["trending", "trend", "hot"],
    "boost": ["boost", "boosted", "promoted"],
    "gainers": ["gainers", "gainer", "winner", "top"],
    "whale_alert": ["whale", "whale_alert", "whale_tracker", "large_tx"],
    "new_listing": ["new_listing", "new", "launch", "ilo"],
    "community_takeover": ["community_takeover", "cto", "takeover"],
    "meme_radar": ["meme_radar", "meme", "radar"],
    "volume_spike": ["volume", "volume_spike", "vol"],
    "ta_signal": ["majors_scanner", "ta", "technical", "mean-reversion", "trend-following", "scalping"],
}


def canonical_source(raw_source: str) -> Dict[str, str]:
    """
    Parse raw source string → canonical source key.
    
    Example:
        "DexScreener boost (100x)" → "dexscreener:boost"
        "CoinGecko trending #15" → "coingecko:trending"
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
    """Parse compound sources (e.g., 'DexScreener + Birdeye')."""
    parts = re.split(r'\s*[+&,]\s*', raw_source)
    
    results = []
    seen_keys = set()
    
    for part in parts:
        if not part.strip():
            continue
        
        canonical = canonical_source(part)
        if canonical["source_key"] not in seen_keys:
            results.append(canonical)
            seen_keys.add(canonical["source_key"])
    
    return results if results else [canonical_source(raw_source)]


# ═════════════════════════════════════════════════════════
# TESTS
# ═════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== SCHEMA NORMALIZATION TEST ===\n")
    
    test_signals = [
        {"symbol": "BTC", "volume_24h_usd": 1000000, "price_change_24h_percent": 5.2},
        {"baseToken": {"symbol": "BONK", "address": "0x123"}, "chainId": "solana", "liquidity": {"usd": 500000}},
    ]
    
    for sig in test_signals:
        normalized = normalize_signal(sig)
        if normalized:
            print(f"Token: {normalized['token']}, Source: {normalized['source']}, Volume: ${normalized['volume_24h']:,.0f}")
    
    print("\n=== SOURCE KEY CANONICALIZATION TEST ===\n")
    
    test_sources = [
        "DexScreener boost (100x) + Birdeye trending",
        "CoinGecko trending #15",
        "Birdeye meme gainer",
    ]
    
    for src in test_sources:
        result = canonical_source(src)
        print(f"{src} → {result['source_key']}")
