#!/usr/bin/env python3
"""
Signal Normalizer — Sprint 11.1.5 Unblock

Converts all signal formats into the canonical schema defined in data-dictionary.md.

Problem: 7 different signal sources use 7 different schemas.
Solution: One normalizer that maps them all to the canonical format.

Canonical Signal Schema:
  token, source, direction, chain, token_address, thesis, timestamp,
  score, volume_24h, market_cap, price_change_24h, symbol

Called by: signal_router.py, replay_engine.py, sanad_pipeline.py
"""

import json
import os
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
SIGNALS_DIR = BASE_DIR / "signals"
sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[NORMALIZE] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────
# Canonical Schema
# ─────────────────────────────────────────────────────────

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


def normalize_signal(raw: dict, source_hint: str = "") -> dict | None:
    """
    Normalize any signal format to canonical schema.
    Returns None if signal is not normalizable (metadata-only, empty).
    """
    if not raw or not isinstance(raw, dict):
        return None

    # Detect source type
    source_type = source_hint or _detect_source(raw)

    # Route to specific normalizer
    normalizers = {
        "meme_radar": _normalize_meme_radar,
        "sentiment": _normalize_sentiment,
        "onchain": _normalize_onchain,
        "telegram": _normalize_telegram,
        "birdeye": _normalize_birdeye_wrapper,
        "coingecko": _normalize_coingecko_wrapper,
        "dexscreener": _normalize_dexscreener_wrapper,
        "rugcheck": _normalize_rugcheck_wrapper,
        "fear_greed": _normalize_fear_greed,
    }

    normalizer = normalizers.get(source_type, _normalize_generic)
    result = normalizer(raw)

    if result is None:
        return None

    # Ensure all canonical fields present
    canonical = {**CANONICAL_DEFAULTS, **result}

    # Validate required fields
    if not canonical["token"] or not canonical["source"]:
        return None

    # Default direction if missing
    if not canonical["direction"]:
        canonical["direction"] = _infer_direction(canonical)

    # Ensure timestamp
    if not canonical["timestamp"]:
        canonical["timestamp"] = raw.get("timestamp", _now().isoformat())

    # Clean up
    canonical["token"] = canonical["token"].upper().strip()
    canonical["source"] = canonical["source"].lower().strip()
    canonical["direction"] = canonical["direction"].upper().strip()
    canonical["chain"] = canonical["chain"].lower().strip()

    # Build symbol if missing
    if not canonical["symbol"] and canonical["token"]:
        canonical["symbol"] = canonical["token"] + "USDT"

    return canonical


# ─────────────────────────────────────────────────────────
# Source Detection
# ─────────────────────────────────────────────────────────

def _detect_source(raw: dict) -> str:
    """Detect signal source from its structure."""
    keys = set(raw.keys())

    # Wrapper files (have nested 'signals' array)
    if "signals" in keys:
        if "trending_raw" in keys or "meme_tokens_raw" in keys:
            return "birdeye"
        if "global" in keys or "top_gainers" in keys:
            return "coingecko"
        if "boosts_top" in keys or "community_takeovers" in keys:
            return "dexscreener"
        if "safety_checks" in keys:
            return "rugcheck"

    # Direct signal files
    if "signal_score" in keys and "scores_breakdown" in keys:
        return "meme_radar"
    if "sentiment_data" in keys or "shift_from_previous" in keys:
        return "sentiment"
    if "onchain_data" in keys:
        return "onchain"
    if "signal_strength" in keys or "source_group" in keys:
        return "telegram"
    if "classification" in keys and "regime" in keys:
        return "fear_greed"

    # Check source field
    src = raw.get("source", "")
    if "meme_radar" in str(src):
        return "meme_radar"
    if "telegram" in str(src):
        return "telegram"
    if "coingecko" in str(src):
        return "coingecko"
    if "dexscreener" in str(src):
        return "dexscreener"
    if "birdeye" in str(src):
        return "birdeye"

    return "generic"


# ─────────────────────────────────────────────────────────
# Source-Specific Normalizers
# ─────────────────────────────────────────────────────────

def _normalize_meme_radar(raw: dict) -> dict:
    """meme_radar: token, symbol, source, signal_score, volume_usd_24h, market_cap"""
    return {
        "token": raw.get("token", ""),
        "symbol": raw.get("symbol", ""),
        "source": raw.get("source", "meme_radar"),
        "source_detail": raw.get("source_detail", ""),
        "direction": _infer_direction_from_thesis(raw.get("thesis", "")),
        "thesis": raw.get("thesis", ""),
        "score": raw.get("signal_score", 0),
        "volume_24h": raw.get("volume_usd_24h", 0),
        "market_cap": raw.get("market_cap", 0),
        "timestamp": raw.get("timestamp", ""),
        "chain": _infer_chain(raw.get("symbol", "")),
    }


def _normalize_sentiment(raw: dict) -> dict:
    """sentiment: token, symbol, source, signal_score, sentiment_data"""
    sentiment = raw.get("sentiment_data", {})
    score = raw.get("signal_score", 0)

    # Extract sentiment direction
    overall = sentiment.get("overall_sentiment", "")
    if isinstance(overall, (int, float)):
        direction = "LONG" if overall > 50 else "SHORT"
    elif isinstance(overall, str):
        direction = "LONG" if overall.lower() in ("bullish", "positive") else "SHORT"
    else:
        direction = _infer_direction_from_thesis(raw.get("thesis", ""))

    return {
        "token": raw.get("token", ""),
        "symbol": raw.get("symbol", ""),
        "source": raw.get("source", "sentiment_scanner"),
        "source_detail": raw.get("source_detail", ""),
        "direction": direction,
        "thesis": raw.get("thesis", ""),
        "score": score,
        "timestamp": raw.get("timestamp", ""),
        "chain": _infer_chain(raw.get("symbol", "")),
    }


def _normalize_onchain(raw: dict) -> dict:
    """onchain: token, source, signal_score, onchain_data"""
    onchain = raw.get("onchain_data", {})

    return {
        "token": raw.get("token", ""),
        "source": raw.get("source", "onchain_analytics"),
        "source_detail": raw.get("source_detail", ""),
        "direction": _infer_direction_from_thesis(raw.get("thesis", "")),
        "thesis": raw.get("thesis", ""),
        "score": raw.get("signal_score", 0),
        "volume_24h": onchain.get("volume_24h", onchain.get("exchange_net_flow", 0)),
        "timestamp": raw.get("timestamp", ""),
        "chain": "bitcoin" if raw.get("token", "").upper() == "BTC" else "",
    }


def _normalize_telegram(raw: dict) -> dict:
    """telegram: source, source_group, token, chain, signal_strength"""
    return {
        "token": raw.get("token", ""),
        "source": "telegram_sniffer",
        "source_detail": raw.get("source_group", ""),
        "direction": _infer_direction_from_message(raw.get("message_preview", "")),
        "chain": raw.get("chain", ""),
        "token_address": _extract_address(raw),
        "thesis": raw.get("message_preview", ""),
        "score": raw.get("signal_strength", 0),
        "timestamp": raw.get("timestamp", ""),
    }


def _normalize_birdeye_wrapper(raw: dict) -> list | None:
    """birdeye wrapper: extract from nested signals array."""
    signals = raw.get("signals", [])
    if not signals:
        return None  # Metadata-only wrapper
    # Return first signal (caller should iterate)
    return _normalize_inner_signal(signals[0], "birdeye")


def _normalize_coingecko_wrapper(raw: dict) -> list | None:
    """coingecko wrapper: extract from nested signals array."""
    signals = raw.get("signals", [])
    if not signals:
        return None
    return _normalize_inner_signal(signals[0], "coingecko")


def _normalize_dexscreener_wrapper(raw: dict) -> list | None:
    """dexscreener wrapper: extract from nested signals array."""
    signals = raw.get("signals", [])
    if not signals:
        return None
    return _normalize_inner_signal(signals[0], "dexscreener")


def _normalize_rugcheck_wrapper(raw: dict) -> dict | None:
    """rugcheck: metadata only — not a tradeable signal."""
    return None


def _normalize_fear_greed(raw: dict) -> dict | None:
    """fear_greed: metadata only — not a tradeable signal."""
    return None


def _normalize_inner_signal(sig: dict, source: str) -> dict:
    """Normalize a signal from inside a wrapper's signals array."""
    return {
        "token": sig.get("token", sig.get("symbol", sig.get("name", ""))),
        "symbol": sig.get("symbol", sig.get("pair", "")),
        "source": source,
        "source_detail": sig.get("source_detail", sig.get("source", "")),
        "direction": sig.get("direction", _infer_direction_from_thesis(sig.get("thesis", sig.get("reason", "")))),
        "chain": sig.get("chain", sig.get("network", "")),
        "token_address": sig.get("token_address", sig.get("address", sig.get("contract", ""))),
        "thesis": sig.get("thesis", sig.get("reason", sig.get("description", ""))),
        "score": sig.get("score", sig.get("signal_score", sig.get("strength", 0))),
        "volume_24h": sig.get("volume_24h", sig.get("volume", sig.get("volumeUSD", 0))),
        "market_cap": sig.get("market_cap", sig.get("marketCap", sig.get("mcap", 0))),
        "price_change_24h": sig.get("price_change_24h", sig.get("priceChange24h", 0)),
        "liquidity": sig.get("liquidity", sig.get("liquidityUSD", 0)),
        "timestamp": sig.get("timestamp", _now().isoformat()),
    }


def _normalize_generic(raw: dict) -> dict:
    """Fallback normalizer — map common field variants."""
    return {
        "token": raw.get("token", raw.get("symbol", raw.get("name", raw.get("coin", "")))),
        "symbol": raw.get("symbol", raw.get("pair", "")),
        "source": raw.get("source", "unknown"),
        "source_detail": raw.get("source_detail", ""),
        "direction": raw.get("direction", _infer_direction_from_thesis(raw.get("thesis", ""))),
        "chain": raw.get("chain", raw.get("network", "")),
        "token_address": raw.get("token_address", raw.get("address", raw.get("contract", raw.get("mint", "")))),
        "thesis": raw.get("thesis", raw.get("reason", raw.get("description", ""))),
        "score": raw.get("score", raw.get("signal_score", raw.get("strength", raw.get("signal_strength", 0)))),
        "volume_24h": raw.get("volume_24h", raw.get("volume_usd_24h", raw.get("volume", 0))),
        "market_cap": raw.get("market_cap", raw.get("marketCap", raw.get("mcap", 0))),
        "price_change_24h": raw.get("price_change_24h", raw.get("priceChange24h", 0)),
        "timestamp": raw.get("timestamp", ""),
    }


# ─────────────────────────────────────────────────────────
# Direction Inference
# ─────────────────────────────────────────────────────────

def _infer_direction(signal: dict) -> str:
    """Infer direction from available data."""
    # From thesis
    thesis = signal.get("thesis", "")
    if thesis:
        d = _infer_direction_from_thesis(thesis)
        if d:
            return d

    # From price change
    pct = signal.get("price_change_24h", 0)
    if pct and pct > 5:
        return "LONG"  # Momentum
    if pct and pct < -10:
        return "SHORT"  # Breakdown

    return "LONG"  # Default: most meme signals are buy signals


def _infer_direction_from_thesis(thesis: str) -> str:
    """Infer LONG/SHORT from thesis text."""
    if not thesis:
        return "LONG"

    lower = thesis.lower()
    long_words = ["bullish", "buy", "long", "accumulation", "surge", "spike",
                  "breakout", "pump", "moon", "rally", "upside", "growth",
                  "gain", "whale buy", "increasing volume", "strong momentum"]
    short_words = ["bearish", "sell", "short", "dump", "crash", "decline",
                   "breakdown", "rug", "scam", "exit", "distribution",
                   "whale sell", "decreasing"]

    long_count = sum(1 for w in long_words if w in lower)
    short_count = sum(1 for w in short_words if w in lower)

    if long_count > short_count:
        return "LONG"
    if short_count > long_count:
        return "SHORT"
    return "LONG"


def _infer_direction_from_message(msg: str) -> str:
    """Infer direction from telegram message."""
    return _infer_direction_from_thesis(msg)


# ─────────────────────────────────────────────────────────
# Chain Inference
# ─────────────────────────────────────────────────────────

SOLANA_TOKENS = {"SOL", "BONK", "WIF", "MYRO", "BOME", "JUP", "RAY", "ORCA", "MANGO"}
ETH_TOKENS = {"ETH", "PEPE", "SHIB", "UNI", "AAVE", "LINK", "MKR"}
BSC_TOKENS = {"BNB", "CAKE", "FLOKI"}


def _infer_chain(symbol: str) -> str:
    """Infer chain from token symbol."""
    token = symbol.upper().replace("USDT", "").replace("USD", "").replace("BUSD", "")
    if token in SOLANA_TOKENS:
        return "solana"
    if token in ETH_TOKENS:
        return "ethereum"
    if token in BSC_TOKENS:
        return "binance"
    if token == "BTC":
        return "bitcoin"
    return ""


def _extract_address(raw: dict) -> str:
    """Extract token address from various field names."""
    for key in ["token_address", "address", "contract", "mint", "contract_address"]:
        val = raw.get(key, "")
        if val and len(str(val)) > 20:
            return str(val)

    # Check if token field itself is an address
    token = raw.get("token", "")
    if len(token) > 30 and not token.isalpha():
        return token

    return ""


# ─────────────────────────────────────────────────────────
# Batch Processing
# ─────────────────────────────────────────────────────────

def normalize_file(filepath: str | Path) -> list:
    """Normalize all signals in a file (handles wrappers with nested arrays)."""
    filepath = Path(filepath)

    try:
        with open(filepath) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    source_hint = filepath.parent.name
    results = []

    # Check if it's a wrapper with nested signals
    if "signals" in raw and isinstance(raw["signals"], list):
        for sig in raw["signals"]:
            normalized = normalize_signal(sig, source_hint)
            if normalized:
                results.append(normalized)
    else:
        # Direct signal file
        normalized = normalize_signal(raw, source_hint)
        if normalized:
            results.append(normalized)

    return results


def normalize_all_signals(days: int = 30) -> list:
    """Normalize all signals from disk."""
    all_signals = []

    if not SIGNALS_DIR.exists():
        return all_signals

    for subdir in SIGNALS_DIR.iterdir():
        if not subdir.is_dir():
            continue
        for f in subdir.glob("*.json"):
            normalized = normalize_file(f)
            all_signals.extend(normalized)

    all_signals.sort(key=lambda s: s.get("timestamp", ""))
    return all_signals


# ─────────────────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    _log("=== SIGNAL NORMALIZER TEST ===")

    # Test all signals from disk
    signals = normalize_all_signals()
    _log(f"Normalized {len(signals)} signals from disk")

    # Stats
    sources = {}
    directions = {}
    chains = {}
    with_token = 0
    with_score = 0
    with_direction = 0

    for s in signals:
        src = s["source"]
        sources[src] = sources.get(src, 0) + 1
        d = s["direction"]
        directions[d] = directions.get(d, 0) + 1
        c = s.get("chain", "") or "unknown"
        chains[c] = chains.get(c, 0) + 1
        if s["token"]:
            with_token += 1
        if s["score"]:
            with_score += 1
        if s["direction"]:
            with_direction += 1

    print(f"\n  Total normalized: {len(signals)}")
    print(f"  With token: {with_token}")
    print(f"  With score: {with_score}")
    print(f"  With direction: {with_direction}")

    print(f"\n  By source:")
    for src, count in sorted(sources.items(), key=lambda x: -x[1]):
        print(f"    {src}: {count}")

    print(f"\n  By direction:")
    for d, count in directions.items():
        print(f"    {d}: {count}")

    print(f"\n  By chain:")
    for c, count in sorted(chains.items(), key=lambda x: -x[1]):
        print(f"    {c}: {count}")

    # Show a few samples
    print(f"\n  Sample normalized signals:")
    for s in signals[:3]:
        print(f"    {s['token']} | {s['source']} | {s['direction']} | score={s['score']} | chain={s['chain']}")

    # Now replay through fast check
    print(f"\n  Running replay with normalized signals...")
    try:
        from replay_engine import replay_fast
        results = replay_fast(signals[:100] if len(signals) > 100 else signals)
        print(f"\n  Replay pass rate: {results['pass_rate']*100:.1f}% ({results['passed_stage1']}/{results['total_signals']})")
        if results["blocked_reasons"]:
            print(f"  Block reasons:")
            for reason, count in sorted(results["blocked_reasons"].items(), key=lambda x: -x[1]):
                print(f"    {reason}: {count}")
    except Exception as e:
        print(f"  Replay error: {e}")

    _log("=== TEST COMPLETE ===")
