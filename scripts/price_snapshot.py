#!/usr/bin/env python3
"""
Sanad Trader v3.0 — Price & Volume Snapshot Cron

Table 6 Row 1: Every 3 minutes, deterministic Python.
Fetches prices for tracked tokens from Binance.
Updates price_cache.json and price_history.json.
Updates cron_health.json with last run timestamp.

This is a data-plane task — deterministic Python, NOT an LLM.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
STATE_DIR = BASE_DIR / "state"
CONFIG_DIR = BASE_DIR / "config"

sys.path.insert(0, str(BASE_DIR / "scripts"))
import binance_client


def load_watchlist():
    """
    Load list of symbols to track.
    Starts with core pairs. Expands as strategies add tokens.
    """
    watchlist_path = CONFIG_DIR / "watchlist.json"
    try:
        with open(watchlist_path, "r") as f:
            data = json.load(f)
        return data.get("symbols", [])
    except (FileNotFoundError, json.JSONDecodeError):
        # Default watchlist — core pairs for initial monitoring
        return [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
            "DOGEUSDT", "PEPEUSDT", "SHIBUSDT", "WIFUSDT",
            "BONKUSDT", "FLOKIUSDT",
        ]


def run_snapshot():
    """Main snapshot function."""
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"[PRICE SNAPSHOT] Starting at {timestamp}")

    # Load watchlist
    symbols = load_watchlist()
    print(f"[PRICE SNAPSHOT] Tracking {len(symbols)} symbols")

    # Fetch prices using Binance client
    success = binance_client.snapshot_prices(symbols)

    if success:
        print(f"[PRICE SNAPSHOT] Complete")
    else:
        print(f"[PRICE SNAPSHOT] WARNING: No prices fetched")

    # Run health check while we're here
    health = binance_client.health_check()
    print(f"[PRICE SNAPSHOT] Binance health: reachable={health['api_reachable']}, auth={health['authenticated']}")

    return success


if __name__ == "__main__":
    success = run_snapshot()
    sys.exit(0 if success else 1)
