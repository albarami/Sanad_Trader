#!/usr/bin/env python3
"""
Solscan API Client — On-chain verification for Solana tokens.
Provides holder distribution, metadata, and transfer activity.
"""

import os
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

SOLSCAN_API_KEY = os.environ.get("SOLSCAN_API_KEY")
BASE_URL = "https://pro-api.solscan.io/v2.0"

STATE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent)) / "state"
CACHE_FILE = STATE_DIR / "solscan_cache.json"


def _log(msg):
    print(f"[SOLSCAN] {msg}")


def _load_cache():
    if CACHE_FILE.exists():
        try:
            return json.load(open(CACHE_FILE))
        except Exception:
            return {}
    return {}


def _save_cache(cache):
    try:
        json.dump(cache, open(CACHE_FILE, "w"), indent=2)
    except Exception as e:
        _log(f"Cache save failed: {e}")


def get_token_meta(token_address: str) -> dict:
    """
    Get token metadata from Solscan.
    Returns: {
        "symbol": str,
        "name": str,
        "decimals": int,
        "supply": float,
        "holder_count": int,
        "verified": bool
    }
    """
    if not SOLSCAN_API_KEY:
        _log("No SOLSCAN_API_KEY — skipping")
        return {}
    
    cache = _load_cache()
    cache_key = f"meta_{token_address}"
    
    # Use cache if < 5 min old
    if cache_key in cache:
        cached = cache[cache_key]
        if cached.get("timestamp", 0) > (time.time() - 300):
            return cached.get("data", {})
    
    url = f"{BASE_URL}/token/meta?address={token_address}"
    headers = {
        "token": SOLSCAN_API_KEY,
        "Accept": "application/json"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            
            if result.get("success"):
                data = result.get("data", {})
                parsed = {
                    "symbol": data.get("symbol", ""),
                    "name": data.get("name", ""),
                    "decimals": data.get("decimals", 9),
                    "supply": float(data.get("supply", 0)) / (10 ** data.get("decimals", 9)),
                    "holder_count": data.get("holder", 0),
                    "verified": data.get("tag") == "verified"
                }
                
                # Cache result
                cache[cache_key] = {
                    "timestamp": time.time(),
                    "data": parsed
                }
                _save_cache(cache)
                
                return parsed
            else:
                _log(f"Token meta failed: {result.get('message', 'unknown')}")
                return {}
                
    except urllib.error.HTTPError as e:
        _log(f"HTTP {e.code}: {e.reason}")
        return {}
    except Exception as e:
        _log(f"Token meta error: {e}")
        return {}


def get_holder_distribution(token_address: str, limit: int = 10) -> dict:
    """
    Get top holder distribution.
    Returns: {
        "holder_count": int,
        "top_10_pct": float,
        "top_holders": [{"address": str, "amount": float, "pct": float}, ...]
    }
    """
    if not SOLSCAN_API_KEY:
        return {}
    
    # First get token supply from meta API
    meta = get_token_meta(token_address)
    if not meta or not meta.get("supply"):
        _log(f"Could not get token supply for holder distribution")
        return {}
    
    total_supply = float(meta.get("supply", 0))
    decimals = int(meta.get("decimals", 9))
    
    url = f"{BASE_URL}/token/holders?address={token_address}&page=1&page_size={limit}"
    headers = {
        "token": SOLSCAN_API_KEY,
        "Accept": "application/json"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            
            if result.get("success"):
                data = result.get("data", {})
                items = data.get("items", [])
                
                holders = []
                top_10_amount = 0.0
                
                for item in items:
                    # Amount from API is in raw token units (needs to be divided by 10^decimals)
                    raw_amount = float(item.get("amount", 0))
                    amount = raw_amount / (10 ** decimals)
                    pct = (amount / total_supply * 100) if total_supply > 0 else 0
                    holders.append({
                        "address": item.get("owner", ""),
                        "amount": amount,
                        "pct": round(pct, 2)
                    })
                    top_10_amount += amount
                
                top_10_pct = (top_10_amount / total_supply * 100) if total_supply > 0 else 0
                
                return {
                    "holder_count": data.get("total", 0),
                    "top_10_pct": round(top_10_pct, 2),
                    "top_holders": holders
                }
            else:
                _log(f"Holder query failed: {result.get('message', 'unknown')}")
                return {}
                
    except Exception as e:
        _log(f"Holder distribution error: {e}")
        return {}


def get_recent_transfers(token_address: str, limit: int = 50) -> dict:
    """
    Get recent token transfer activity.
    Returns: {
        "transfer_count": int,
        "unique_addresses": int,
        "avg_amount": float,
        "last_24h_volume": float
    }
    """
    if not SOLSCAN_API_KEY:
        return {}
    
    url = f"{BASE_URL}/token/transfer?address={token_address}&page=1&page_size={limit}"
    headers = {
        "token": SOLSCAN_API_KEY,
        "Accept": "application/json"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            
            if result.get("success"):
                data = result.get("data", {})
                items = data.get("items", [])
                
                unique_addrs = set()
                total_amount = 0.0
                
                for item in items:
                    unique_addrs.add(item.get("from_address", ""))
                    unique_addrs.add(item.get("to_address", ""))
                    total_amount += float(item.get("amount", 0))
                
                avg_amount = total_amount / len(items) if items else 0
                
                return {
                    "transfer_count": len(items),
                    "unique_addresses": len(unique_addrs),
                    "avg_amount": round(avg_amount, 4),
                    "last_24h_volume": round(total_amount, 2)
                }
            else:
                return {}
                
    except Exception as e:
        _log(f"Transfer activity error: {e}")
        return {}


def enrich_signal_with_solscan(signal: dict) -> dict:
    """
    Enrich a signal with Solscan on-chain data.
    Adds: holder_count, top_10_pct, verified, transfer_activity.
    """
    token_address = signal.get("contract_address") or signal.get("address") or signal.get("token_address")
    
    if not token_address:
        _log(f"No contract address for {signal.get('token', '?')} — skipping Solscan")
        return signal
    
    _log(f"Enriching {signal.get('token')} with Solscan data...")
    
    # Get metadata
    meta = get_token_meta(token_address)
    if meta:
        signal["solscan_holder_count"] = meta.get("holder_count", 0)
        signal["solscan_verified"] = meta.get("verified", False)
        signal["solscan_supply"] = meta.get("supply", 0)
    
    # Get holder distribution
    holders = get_holder_distribution(token_address, limit=10)
    if holders:
        signal["solscan_top_10_pct"] = holders.get("top_10_pct", 0)
        signal["solscan_concentration"] = "HIGH" if holders.get("top_10_pct", 0) > 50 else "MEDIUM" if holders.get("top_10_pct", 0) > 25 else "LOW"
    
    # Get transfer activity
    transfers = get_recent_transfers(token_address, limit=50)
    if transfers:
        signal["solscan_transfer_count"] = transfers.get("transfer_count", 0)
        signal["solscan_unique_addresses"] = transfers.get("unique_addresses", 0)
        signal["solscan_24h_volume"] = transfers.get("last_24h_volume", 0)
    
    _log(f"Solscan enrichment complete: holders={signal.get('solscan_holder_count', 0)}, top10={signal.get('solscan_top_10_pct', 0)}%, verified={signal.get('solscan_verified', False)}")
    
    return signal


if __name__ == "__main__":
    import sys
    import time
    
    if len(sys.argv) < 2:
        print("Usage: python3 solscan_client.py <token_address>")
        sys.exit(1)
    
    addr = sys.argv[1]
    
    print(f"\n=== Solscan Query: {addr} ===\n")
    
    meta = get_token_meta(addr)
    print("Metadata:", json.dumps(meta, indent=2))
    
    holders = get_holder_distribution(addr, limit=10)
    print("\nTop Holders:", json.dumps(holders, indent=2))
    
    transfers = get_recent_transfers(addr, limit=20)
    print("\nRecent Transfers:", json.dumps(transfers, indent=2))
