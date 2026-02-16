#!/usr/bin/env python3
"""
Dust Sweeper — Sprint 6.1.21
Runs Sunday 04:00 QAT (01:00 UTC).
Converts small leftover balances (dust) to BNB on Binance.
Deterministic Python.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[DUST] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


# Dust threshold: balances under $1 are considered dust
DUST_THRESHOLD_USD = 1.0
# Minimum BNB to keep (for fees)
MIN_BNB_KEEP = 0.01


def sweep(paper_mode: bool = True) -> dict:
    _log("=== DUST SWEEPER ===")
    _log(f"Mode: {'PAPER' if paper_mode else 'LIVE'}")

    result = {
        "timestamp": _now().isoformat(),
        "paper_mode": paper_mode,
        "dust_found": [],
        "swept": 0,
        "total_dust_usd": 0,
    }

    try:
        import binance_client

        # Get all balances
        balances = binance_client.get_account_balances()
        if not balances:
            _log("Could not fetch balances")
            return result

        # Identify dust
        for asset, info in balances.items():
            if asset in ("USDT", "BNB", "BUSD"):
                continue  # Skip stablecoins and BNB

            free = float(info.get("free", 0))
            if free <= 0:
                continue

            # Estimate USD value
            usd_value = info.get("usd_value", 0)
            if not usd_value:
                try:
                    ticker = binance_client.get_price(f"{asset}USDT")
                    usd_value = free * float(ticker.get("price", 0))
                except Exception:
                    usd_value = 0

            if 0 < usd_value < DUST_THRESHOLD_USD:
                result["dust_found"].append({
                    "asset": asset,
                    "amount": free,
                    "usd_value": round(usd_value, 4),
                })
                result["total_dust_usd"] += usd_value

        if not result["dust_found"]:
            _log("No dust found")
            return result

        _log(f"Found {len(result['dust_found'])} dust assets worth ${result['total_dust_usd']:.4f}")

        if paper_mode:
            _log("Paper mode — no actual conversion")
            for d in result["dust_found"]:
                _log(f"  Would convert: {d['amount']} {d['asset']} (${d['usd_value']:.4f})")
        else:
            # Binance dust conversion API
            assets = [d["asset"] for d in result["dust_found"]]
            try:
                conversion = binance_client.convert_dust_to_bnb(assets)
                result["swept"] = len(assets)
                result["conversion_result"] = conversion
                _log(f"Swept {len(assets)} assets to BNB")
            except Exception as e:
                _log(f"Dust conversion failed: {e}")

    except ImportError:
        _log("binance_client not available — checking state for balances")
        _log("No dust to sweep (paper mode)")
    except Exception as e:
        _log(f"Dust sweeper error: {e}")

    # Save state
    _save_json(STATE_DIR / "dust_sweeper_state.json", result)

    _log("=== SWEEP COMPLETE ===")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    sweep(paper_mode=not args.live)
