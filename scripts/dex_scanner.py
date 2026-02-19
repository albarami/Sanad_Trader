#!/usr/bin/env python3
"""
DEX Scanner — Birdeye trending + volume-sorted Solana tokens.
Deterministic Python. NO LLMs.

Endpoints:
- /defi/trending_tokens/solana (hot tokens)
- /defi/tokenlist?sort_by=v24hUSD (volume leaders)

Output: signals/dexscreener/TIMESTAMP.json
"""
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from birdeye_client import _get, _log

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parents[1]))
SIGNALS_DIR = BASE_DIR / "signals" / "dexscreener"
CRON_HEALTH = BASE_DIR / "state" / "cron_health.json"

def scan_trending() -> list[dict]:
    """Get trending Solana tokens from Birdeye."""
    _log("Fetching trending tokens...")
    data = _get("/defi/token_trending", params={"sort_by": "rank", "sort_type": "asc", "limit": 20})
    
    if not data or "data" not in data:
        return []
    
    tokens = data["data"].get("tokens", [])
    signals = []
    
    for token in tokens[:20]:  # Top 20 trending
        signal = {
            "token": token.get("symbol", "UNKNOWN"),
            "token_address": token.get("address", ""),
            "chain": "solana",
            "source": "birdeye_trending",
            "current_price": token.get("price", 0),
            "price_change_24h_pct": token.get("priceChange24hPercent", 0),
            "volume_24h_usd": token.get("v24hUSD", 0),
            "liquidity_usd": token.get("liquidity", 0),
            "market_cap": token.get("mc", 0),
            "rank": token.get("rank", 999),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        signals.append(signal)
    
    _log(f"Found {len(signals)} trending tokens")
    return signals


def scan_volume_leaders() -> list[dict]:
    """Get top volume Solana tokens from Birdeye."""
    _log("Fetching volume leaders...")
    params = {
        "sort_by": "v24hUSD",
        "sort_type": "desc",
        "offset": 0,
        "limit": 50,
        "min_liquidity": 50000,  # $50k minimum liquidity filter
    }
    
    data = _get("/defi/tokenlist", params=params)
    
    if not data or "data" not in data:
        return []
    
    tokens = data["data"].get("tokens", [])
    signals = []
    
    for token in tokens[:20]:  # Top 20 by volume
        signal = {
            "token": token.get("symbol", "UNKNOWN"),
            "token_address": token.get("address", ""),
            "chain": "solana",
            "source": "birdeye_volume",
            "current_price": token.get("price", 0),
            "price_change_24h_pct": token.get("priceChange24hPercent", 0),
            "volume_24h_usd": token.get("v24hUSD", 0),
            "liquidity_usd": token.get("liquidity", 0),
            "market_cap": token.get("mc", 0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        signals.append(signal)
    
    _log(f"Found {len(signals)} volume leaders")
    return signals


def main():
    _log("DEX Scanner starting...")
    
    # Collect signals from both sources
    all_signals = []
    
    try:
        trending = scan_trending()
        all_signals.extend(trending)
    except Exception as e:
        _log(f"Trending scan failed: {e}")
    
    try:
        volume = scan_volume_leaders()
        all_signals.extend(volume)
    except Exception as e:
        _log(f"Volume scan failed: {e}")
    
    # Dedupe by token_address
    seen = set()
    unique_signals = []
    for sig in all_signals:
        addr = sig.get("token_address", "")
        if addr and addr not in seen:
            seen.add(addr)
            unique_signals.append(sig)
    
    _log(f"Total unique signals: {len(unique_signals)}")
    
    # Write to file
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    output_file = SIGNALS_DIR / f"{timestamp}.json"
    
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "dex_scanner",
        "count": len(unique_signals),
        "signals": unique_signals
    }
    
    output_file.write_text(json.dumps(output, indent=2))
    _log(f"Wrote {len(unique_signals)} signals to {output_file.name}")
    
    # Update cron health
    try:
        if CRON_HEALTH.exists():
            health = json.loads(CRON_HEALTH.read_text())
        else:
            health = {}
        
        health["dex_scanner"] = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "status": "OK",
            "signal_count": len(unique_signals)
        }
        
        CRON_HEALTH.write_text(json.dumps(health, indent=2))
    except Exception as e:
        _log(f"Cron health update failed: {e}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
