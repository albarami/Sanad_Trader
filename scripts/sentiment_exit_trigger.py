#!/usr/bin/env python3
"""
Sentiment Reversal Exit Trigger — Sprint 4.1.11
Monitors sentiment shifts for open positions.
If sentiment drops sharply while we're long → emit exit signal.

Deterministic Python. No LLMs.

Reads:
- state/sentiment_state.json (from sentiment_scanner.py)
- positions.json (open positions)

Emits:
- signals/exits/sentiment_reversal_*.json

Runs every 15 min via cron (after sentiment_scanner).
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
SIGNALS_DIR = BASE_DIR / "signals" / "exits"
POSITIONS_PATH = STATE_DIR / "positions.json"
SENTIMENT_STATE = STATE_DIR / "sentiment_state.json"
EXIT_STATE = STATE_DIR / "sentiment_exit_state.json"

# Thresholds
BEARISH_SCORE = 30       # Below this = bearish
REVERSAL_DROP = 20       # 20+ point drop from previous = reversal
CRITICAL_DROP = 35       # 35+ point drop = urgent exit
COOLDOWN_HOURS = 4       # Don't re-trigger for same token within 4h


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[SENT-EXIT] {ts} {msg}", flush=True)


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


def _get_open_positions() -> dict:
    """Get open positions as {token: position_data}."""
    positions = _load_json(POSITIONS_PATH, {})
    if isinstance(positions, list):
        return {p.get("token", p.get("symbol", "")): p for p in positions if p.get("status", "").upper() == "OPEN"}
    elif isinstance(positions, dict):
        return {k: v for k, v in positions.items() if isinstance(v, dict) and v.get("status", "").upper() == "OPEN"}
    return {}


def _get_sentiment_data() -> dict:
    """Get latest sentiment scores per token."""
    state = _load_json(SENTIMENT_STATE, {})
    return state.get("scores", state)


def check_reversals():
    now = _now()
    _log("=== SENTIMENT EXIT CHECK ===")

    positions = _get_open_positions()
    if not positions:
        _log("No open positions — nothing to check")
        return []

    sentiment = _get_sentiment_data()
    exit_state = _load_json(EXIT_STATE, {"cooldowns": {}, "previous_scores": {}})
    cooldowns = exit_state.get("cooldowns", {})
    prev_scores = exit_state.get("previous_scores", {})
    signals = []

    for token, pos in positions.items():
        # Normalize token name for sentiment lookup
        token_clean = token.replace("USDT", "").upper()
        current_score = None

        # Find sentiment score (try multiple key formats)
        for key in [token_clean, token, token_clean.lower(), f"{token_clean}USDT"]:
            if key in sentiment:
                data = sentiment[key]
                current_score = data.get("sentiment_score", data) if isinstance(data, dict) else data
                break

        if current_score is None:
            continue

        prev_score = prev_scores.get(token_clean)
        drop = (prev_score - current_score) if prev_score is not None else 0

        # Check cooldown
        if token_clean in cooldowns:
            try:
                cooldown_until = datetime.fromisoformat(cooldowns[token_clean])
                if now < cooldown_until:
                    continue
            except (ValueError, TypeError):
                pass

        # Determine exit urgency
        exit_signal = None

        if drop >= CRITICAL_DROP:
            exit_signal = {
                "urgency": "CRITICAL",
                "reason": f"Sentiment crashed {drop:.0f} points ({prev_score:.0f}→{current_score:.0f})",
                "recommended_action": "IMMEDIATE_EXIT",
            }
        elif drop >= REVERSAL_DROP and current_score < BEARISH_SCORE:
            exit_signal = {
                "urgency": "HIGH",
                "reason": f"Sentiment reversed {drop:.0f} points to bearish ({current_score:.0f}/100)",
                "recommended_action": "EXIT_AT_MARKET",
            }
        elif current_score < BEARISH_SCORE and (prev_score is None or prev_score >= BEARISH_SCORE):
            exit_signal = {
                "urgency": "NORMAL",
                "reason": f"Sentiment turned bearish ({current_score:.0f}/100)",
                "recommended_action": "TIGHTEN_STOP",
            }

        if exit_signal:
            signal = {
                "token": token,
                "source": "sentiment_exit_trigger",
                "type": "EXIT",
                "sentiment_score": current_score,
                "previous_score": prev_score,
                "drop": drop,
                **exit_signal,
                "position_entry": pos.get("entry_price"),
                "timestamp": now.isoformat(),
            }
            SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
            fname = f"{now.strftime('%Y%m%d_%H%M')}_{token_clean}_sentiment_exit.json"
            _save_json(SIGNALS_DIR / fname, signal)
            signals.append(signal)
            cooldowns[token_clean] = (now + timedelta(hours=COOLDOWN_HOURS)).isoformat()
            _log(f"  EXIT SIGNAL [{exit_signal['urgency']}]: {token} — {exit_signal['reason']}")

        # Update previous scores
        prev_scores[token_clean] = current_score

    # Save state
    exit_state["cooldowns"] = cooldowns
    exit_state["previous_scores"] = prev_scores
    exit_state["last_check"] = now.isoformat()
    exit_state["open_positions_checked"] = len(positions)
    _save_json(EXIT_STATE, exit_state)

    _log(f"=== CHECK COMPLETE: {len(signals)} exit signals from {len(positions)} positions ===")
    return signals


if __name__ == "__main__":
    check_reversals()
