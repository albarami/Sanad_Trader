#!/usr/bin/env python3
"""
Cross-Feed Price Validator — Sprint 2.2.5
Compares Binance vs CoinGecko prices.
>2% deviation = WARNING, >5% = BLOCK.
Used by policy engine / heartbeat for data quality.
"""
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
CROSS_FEED_PATH = STATE_DIR / "cross_feed_validation.json"

# Token → CoinGecko ID mapping
TOKEN_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "DOGE": "dogecoin",
    "PEPE": "pepe",
    "SHIB": "shiba-inu",
    "WIF": "dogwifcoin",
    "BONK": "bonk",
    "FLOKI": "floki",
}

WARN_THRESHOLD = 0.02   # 2%
BLOCK_THRESHOLD = 0.05  # 5%


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[CROSS-FEED] {ts} {msg}", flush=True)


def validate_prices() -> dict:
    """Compare Binance vs CoinGecko prices for all watchlist tokens."""
    sys.path.insert(0, str(SCRIPT_DIR))

    try:
        import binance_client
    except ImportError:
        _log("ERROR: Cannot import binance_client")
        return {"status": "ERROR", "deviations": []}

    try:
        import coingecko_client
    except ImportError:
        _log("ERROR: Cannot import coingecko_client")
        return {"status": "ERROR", "deviations": []}

    results = []
    blocked = False
    warnings = 0

    for token, cg_id in TOKEN_MAP.items():
        symbol = f"{token}USDT"

        # Get Binance price
        binance_price = binance_client.get_price(symbol)
        if not binance_price:
            _log(f"  {token}: Binance price unavailable")
            continue

        # Get CoinGecko price (get_prices takes list, returns {id: {usd: ...}})
        try:
            cg_data = coingecko_client.get_prices([cg_id])
            if cg_data and isinstance(cg_data, dict):
                cg_price = cg_data.get(cg_id, {}).get("usd", 0)
            else:
                cg_price = 0
        except Exception as e:
            _log(f"  {token}: CoinGecko error — {e}")
            continue

        if not cg_price or cg_price <= 0:
            _log(f"  {token}: CoinGecko price unavailable")
            continue

        # Calculate deviation
        deviation = abs(binance_price - cg_price) / binance_price

        status = "OK"
        if deviation >= BLOCK_THRESHOLD:
            status = "BLOCK"
            blocked = True
        elif deviation >= WARN_THRESHOLD:
            status = "WARN"
            warnings += 1

        result = {
            "token": token,
            "binance": round(binance_price, 8),
            "coingecko": round(cg_price, 8),
            "deviation_pct": round(deviation * 100, 3),
            "status": status,
        }
        results.append(result)

        flag = " ⚠️" if status == "WARN" else " 🚫" if status == "BLOCK" else ""
        _log(f"  {token}: Binance=${binance_price:.6f} CG=${cg_price:.6f} dev={deviation:.3%}{flag}")

    overall = "BLOCK" if blocked else "WARN" if warnings > 0 else "OK"

    output = {
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checked": len(results),
        "warnings": warnings,
        "blocked": blocked,
        "deviations": results,
    }

    # Save state
    try:
        tmp = CROSS_FEED_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(output, f, indent=2)
        os.replace(tmp, CROSS_FEED_PATH)
    except Exception as e:
        _log(f"ERROR saving state: {e}")

    return output


if __name__ == "__main__":
    _log("=== CROSS-FEED PRICE VALIDATION ===")
    result = validate_prices()
    _log(f"=== RESULT: {result['status']} ({result['checked']} tokens, {result['warnings']} warnings) ===")
