#!/usr/bin/env python3
"""
Exchange Router — Sprint 4.4.5
Routes orders to Binance vs MEXC based on token listing.
Deterministic Python. No LLMs.

Logic:
1. Token on Binance → use Binance (lower fees, deeper book)
2. Not on Binance, on MEXC → use MEXC
3. Solana token not on CEX → DEX route (future)
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[ROUTER] {ts} {msg}", flush=True)


_binance_cache = None


def _get_binance_symbols() -> set:
    global _binance_cache
    if _binance_cache is not None:
        return _binance_cache
    try:
        import json
        state_path = SCRIPT_DIR.parent / "state" / "known_binance_symbols.json"
        with open(state_path) as f:
            data = json.load(f)
        _binance_cache = set(data.get("symbols", []))
        return _binance_cache
    except Exception:
        return set()


def _check_binance_live(symbol: str) -> float | None:
    try:
        import binance_client
        return binance_client.get_price(symbol)
    except Exception:
        return None


def _check_mexc_live(symbol: str) -> float | None:
    try:
        import mexc_client
        return mexc_client.get_price(symbol)
    except Exception:
        return None


def route(symbol: str, chain: str = "unknown") -> dict:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    # Check Binance first (preferred — lower fees, deeper liquidity)
    binance_symbols = _get_binance_symbols()
    if symbol in binance_symbols:
        _log(f"{symbol} → BINANCE (in known symbols)")
        return {"exchange": "binance", "reason": "Listed on Binance", "symbol": symbol, "chain": chain}

    # If cache miss, try live
    price = _check_binance_live(symbol)
    if price and price > 0:
        _log(f"{symbol} → BINANCE (live check, ${price:.6f})")
        return {"exchange": "binance", "reason": "Listed on Binance (live)", "symbol": symbol, "chain": chain}

    # Check MEXC
    price = _check_mexc_live(symbol)
    if price and price > 0:
        _log(f"{symbol} → MEXC (${price:.6f})")
        return {"exchange": "mexc", "reason": "Not on Binance, listed on MEXC", "symbol": symbol, "chain": chain}

    # Solana → DEX
    if chain.lower() == "solana":
        _log(f"{symbol} → DEX (Solana, not on CEX)")
        return {"exchange": "dex", "reason": "Solana token — Jupiter/Raydium", "symbol": symbol, "chain": chain}

    _log(f"{symbol} → NONE")
    return {"exchange": "none", "reason": "Not found on any exchange", "symbol": symbol, "chain": chain}


def get_client(exchange: str):
    if exchange == "binance":
        import binance_client
        return binance_client
    elif exchange == "mexc":
        import mexc_client
        return mexc_client
    return None


if __name__ == "__main__":
    _log("=== EXCHANGE ROUTER TEST ===")
    tests = [
        ("BTCUSDT", "unknown"),
        ("ETHUSDT", "ethereum"),
        ("PEPEUSDT", "ethereum"),
        ("WIFUSDT", "solana"),
        ("FAKECOINUSDT", "solana"),
    ]
    for sym, chain in tests:
        result = route(sym, chain)
        print(f"  {sym} ({chain}) → {result['exchange']}: {result['reason']}")
