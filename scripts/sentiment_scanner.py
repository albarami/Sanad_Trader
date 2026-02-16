#!/usr/bin/env python3
"""
Perplexity Sentiment Scanner — Sprint 3.8.4
Uses Perplexity Sonar to scan crypto social sentiment.

Runs every 15 minutes via cron.
Scans watchlist + open positions for social sentiment shifts.
Outputs to signals/sentiment/ and state/sentiment_state.json.

No Twitter API needed — Perplexity aggregates Twitter/Reddit/Telegram.
"""
import json
import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
CONFIG_ENV = BASE_DIR / "config" / ".env"
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.json"
SIGNALS_DIR = BASE_DIR / "signals" / "sentiment"
STATE_PATH = BASE_DIR / "state" / "sentiment_state.json"

PERPLEXITY_BASE = "https://api.perplexity.ai"
MAX_TOKENS_PER_RUN = 5
SCAN_COOLDOWN_MIN = 30  # Don't re-scan same token within 30 min
SIGNAL_THRESHOLD = 75   # Score >= 75 = emit signal


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[SENTIMENT] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


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


def _load_perplexity_key():
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("PERPLEXITY_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return os.environ.get("PERPLEXITY_API_KEY", "")


def _load_watchlist():
    data = _load_json(WATCHLIST_PATH, {})
    if isinstance(data, dict):
        symbols = data.get("symbols", [])
        if isinstance(symbols, list):
            return [s.get("base", s) if isinstance(s, dict) else s.replace("USDT", "") for s in symbols]
    return ["BTC", "ETH", "SOL", "PEPE", "WIF"]


def _get_open_positions():
    pos = _load_json(BASE_DIR / "state" / "positions.json", {})
    if isinstance(pos, dict):
        return list(pos.keys())
    if isinstance(pos, list):
        return [p.get("symbol", "").replace("USDT", "") for p in pos if isinstance(p, dict)]
    return []


def scan_token_sentiment(token: str, api_key: str) -> dict | None:
    """Query Perplexity for social sentiment on a token."""
    try:
        resp = requests.post(
            f"{PERPLEXITY_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "sonar",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a crypto social sentiment analyst. Respond ONLY with valid JSON, no other text. "
                            "Format: {\"sentiment_score\": <0-100 where 0=extreme_fear 50=neutral 100=extreme_greed>, "
                            "\"trend\": \"rising\"|\"falling\"|\"stable\", "
                            "\"volume\": \"high\"|\"normal\"|\"low\", "
                            "\"key_narratives\": [\"...\"], "
                            "\"risk_signals\": [\"...\"], "
                            "\"notable_mentions\": <count of notable influencer mentions in last 4h>}"
                        )
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Analyze the current social media sentiment for cryptocurrency ${token} "
                            f"across Twitter/X, Reddit, and Telegram in the last 4 hours. "
                            f"Is the mention velocity increasing or decreasing? "
                            f"Any notable influencer mentions? Any FUD or hype narratives?"
                        )
                    }
                ],
                "max_tokens": 400,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Clean markdown fences
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        result = json.loads(content)
        result["token"] = token
        result["scanned_at"] = _now().isoformat()
        return result

    except json.JSONDecodeError:
        _log(f"  {token}: Perplexity returned non-JSON")
        return None
    except requests.exceptions.HTTPError as e:
        _log(f"  {token}: Perplexity HTTP error — {e}")
        return None
    except Exception as e:
        _log(f"  {token}: Error — {e}")
        return None


def run():
    """Main sentiment scan execution."""
    now = _now()
    _log("=== SENTIMENT SCAN ===")

    api_key = _load_perplexity_key()
    if not api_key:
        _log("ERROR: PERPLEXITY_API_KEY not configured")
        return []

    # Build token list: watchlist + open positions (deduplicated)
    watchlist = _load_watchlist()
    positions = _get_open_positions()
    all_tokens = list(dict.fromkeys(positions + watchlist))  # Positions first, deduplicated
    _log(f"Scanning {min(len(all_tokens), MAX_TOKENS_PER_RUN)} tokens (positions: {len(positions)}, watchlist: {len(watchlist)})")

    # Load state
    state = _load_json(STATE_PATH, {
        "last_run": None,
        "scans": {},
        "total_signals": 0,
    })
    scans = state.get("scans", {})

    signals = []
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    for token in all_tokens[:MAX_TOKENS_PER_RUN]:
        if not token:
            continue

        # Cooldown check
        last_scan = scans.get(token, {}).get("scanned_at")
        if last_scan:
            try:
                last_dt = datetime.fromisoformat(last_scan)
                if (now - last_dt).total_seconds() < SCAN_COOLDOWN_MIN * 60:
                    _log(f"  {token}: cooldown (scanned {int((now - last_dt).total_seconds() / 60)}min ago)")
                    continue
            except (ValueError, TypeError):
                pass

        # Scan
        result = scan_token_sentiment(token, api_key)
        if not result:
            continue

        score = result.get("sentiment_score", 50)
        trend = result.get("trend", "stable")
        volume = result.get("volume", "normal")
        _log(f"  {token}: score={score}/100, trend={trend}, volume={volume}")

        # Store scan result
        scans[token] = result

        # Detect sentiment shift from previous scan
        prev_score = state.get("scans", {}).get(token, {}).get("sentiment_score", 50)
        shift = score - prev_score

        # Generate signal if criteria met
        emit_signal = False
        signal_reason = ""

        if score >= SIGNAL_THRESHOLD and trend == "rising":
            emit_signal = True
            signal_reason = f"High sentiment ({score}/100) with rising trend"
        elif abs(shift) >= 25:
            emit_signal = True
            direction = "positive" if shift > 0 else "negative"
            signal_reason = f"Sentiment shift {direction}: {prev_score} → {score} ({shift:+d})"
        elif score <= 25 and trend == "falling":
            emit_signal = True
            signal_reason = f"Extreme fear ({score}/100) — potential contrarian opportunity"

        if emit_signal:
            signal = {
                "token": token,
                "symbol": f"{token}USDT",
                "source": "sentiment_scanner",
                "source_detail": "Perplexity social sentiment aggregation",
                "thesis": signal_reason,
                "signal_score": score,
                "sentiment_data": result,
                "shift_from_previous": shift,
                "timestamp": now.isoformat(),
            }
            filename = f"{now.strftime('%Y%m%d_%H%M')}_{token}.json"
            _save_json(SIGNALS_DIR / filename, signal)
            signals.append(signal)
            _log(f"  SIGNAL: {token} — {signal_reason}")

        time.sleep(1.5)  # Perplexity rate limit

    # Update state
    state["last_run"] = now.isoformat()
    state["scans"] = scans
    state["total_signals"] = state.get("total_signals", 0) + len(signals)
    _save_json(STATE_PATH, state)

    _log(f"=== SCAN COMPLETE: {len(signals)} signals ===")
    return signals


if __name__ == "__main__":
    try:
        signals = run()
    except Exception as e:
        _log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
