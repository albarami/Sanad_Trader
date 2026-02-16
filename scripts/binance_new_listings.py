#!/usr/bin/env python3
"""
Binance New Listing Detector — Sprint 2.1.9
Deterministic Python. No LLMs.
Checks Binance exchangeInfo for symbols not in our known list.
New USDT pairs = potential early meme coin listing signal.
"""
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
KNOWN_SYMBOLS_PATH = STATE_DIR / "known_binance_symbols.json"
SIGNALS_DIR = BASE_DIR / "signals" / "listings"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[LISTING] {ts} {msg}", flush=True)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def check_new_listings():
    """Compare current Binance symbols against known list. Return new ones."""
    import binance_client

    # Get current exchange info
    try:
        import urllib.request
        url = "https://api.binance.com/api/v3/exchangeInfo"
        req = urllib.request.Request(url, headers={"User-Agent": "SanadTrader/3.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        _log(f"ERROR fetching exchangeInfo: {e}")
        return []

    # Filter USDT trading pairs that are TRADING status
    current_symbols = set()
    for s in data.get("symbols", []):
        if (s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
                and s.get("isSpotTradingAllowed", False)):
            current_symbols.add(s["symbol"])

    _log(f"Current USDT pairs on Binance: {len(current_symbols)}")

    # Load known symbols
    known = _load_json(KNOWN_SYMBOLS_PATH, {"symbols": [], "last_checked": None})
    known_set = set(known.get("symbols", []))

    if not known_set:
        # First run — save current as baseline
        known["symbols"] = sorted(current_symbols)
        known["last_checked"] = datetime.now(timezone.utc).isoformat()
        known["count"] = len(current_symbols)
        _save_json(KNOWN_SYMBOLS_PATH, known)
        _log(f"First run: saved {len(current_symbols)} symbols as baseline")
        return []

    # Find new symbols
    new_symbols = current_symbols - known_set
    removed_symbols = known_set - current_symbols

    if new_symbols:
        _log(f"NEW LISTINGS DETECTED: {new_symbols}")

        # Generate signals for each new listing
        SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        signals = []

        for sym in new_symbols:
            token = sym.replace("USDT", "")
            signal = {
                "token": token,
                "symbol": sym,
                "source": "binance_new_listing",
                "source_detail": "New Binance USDT spot listing detected",
                "thesis": f"{token} just listed on Binance spot. CEX listing play opportunity.",
                "signal_score": 75,
                "timestamp": now.isoformat(),
            }
            filename = f"{now.strftime('%Y%m%d_%H%M')}_{token}.json"
            _save_json(SIGNALS_DIR / filename, signal)
            signals.append(signal)
            _log(f"  SIGNAL: {sym} — new Binance listing")

    if removed_symbols:
        _log(f"Delisted: {removed_symbols}")

    # Update known list
    known["symbols"] = sorted(current_symbols)
    known["last_checked"] = datetime.now(timezone.utc).isoformat()
    known["count"] = len(current_symbols)
    known["last_new"] = sorted(new_symbols) if new_symbols else known.get("last_new", [])
    known["last_removed"] = sorted(removed_symbols) if removed_symbols else known.get("last_removed", [])
    _save_json(KNOWN_SYMBOLS_PATH, known)

    return list(new_symbols)


if __name__ == "__main__":
    _log("=== BINANCE NEW LISTING CHECK ===")
    new = check_new_listings()
    if new:
        _log(f"Found {len(new)} new listings: {new}")
    else:
        _log("No new listings detected")
