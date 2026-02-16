#!/usr/bin/env python3
"""
Fear & Greed Index Client — Sprint 3.5
Deterministic Python. No LLMs. No API key needed.
Fetches crypto market sentiment from alternative.me.
"""

import json
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
SIGNALS_DIR = BASE_DIR / "signals" / "market"
OUTPUT_PATH = SIGNALS_DIR / "fear_greed_latest.json"

API_URL = "https://api.alternative.me/fng/"


def _log(msg: str):
    print(f"[FEAR&GREED] {msg}", flush=True)


def _regime(value: int) -> str:
    if value <= 20:
        return "EXTREME_FEAR"
    elif value <= 40:
        return "FEAR"
    elif value <= 60:
        return "NEUTRAL"
    elif value <= 80:
        return "GREED"
    else:
        return "EXTREME_GREED"


def _trend(history: list[int]) -> str:
    if len(history) < 3:
        return "stable"
    first_half = sum(history[: len(history) // 2]) / (len(history) // 2)
    second_half = sum(history[len(history) // 2 :]) / (len(history) - len(history) // 2)
    diff = second_half - first_half
    if diff > 3:
        return "rising"
    elif diff < -3:
        return "falling"
    return "stable"


def run():
    now = datetime.now(timezone.utc)
    _log(now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))

    try:
        resp = requests.get(API_URL, params={"limit": 7, "format": "json"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log(f"ERROR: {e}")
        return None

    entries = data.get("data", [])
    if not entries:
        _log("ERROR: No data returned")
        return None

    current = entries[0]
    value = int(current.get("value", 50))
    classification = current.get("value_classification", "Unknown")

    history = [int(e.get("value", 50)) for e in reversed(entries)]
    avg_7d = round(sum(history) / len(history), 1)
    trend_7d = _trend(history)
    regime = _regime(value)

    result = {
        "value": value,
        "classification": classification,
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "trend_7d": trend_7d,
        "history_7d": history,
        "avg_7d": avg_7d,
        "regime": regime,
    }

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, indent=2))

    _log(f"Current: {value} ({classification}) | 7d avg: {avg_7d} | Trend: {trend_7d}")
    _log(f"Regime: {regime}")
    _log(f"Saved to signals/market/fear_greed_latest.json")

    return result


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        _log(f"FATAL: {e}")
        sys.exit(1)
