#!/usr/bin/env python3
"""
Whale Exit Detection Trigger — Sprint 4.1.10

Monitors on-chain whale movements for open positions.
If whale(s) dump while we're long → emit exit signal.

Deterministic Python. No LLMs.

Reads:
- state/onchain_analytics_state.json (whale alerts from onchain_analytics.py)
- state/positions.json (open positions)

Emits:
- signals/exits/whale_exit_*.json

Runs every 15 min via cron (after onchain_analytics).
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
SIGNALS_DIR = BASE_DIR / "signals" / "exits"
POSITIONS_PATH = STATE_DIR / "positions.json"
ONCHAIN_STATE = STATE_DIR / "onchain_analytics_state.json"
WHALE_EXIT_STATE = STATE_DIR / "whale_exit_state.json"

# Thresholds
WHALE_TX_THRESHOLD_USD = 500_000   # $500K+ = whale movement
WHALE_CLUSTER_COUNT = 2             # 2+ whale sells in window = cluster
CLUSTER_WINDOW_HOURS = 2            # Look back 2 hours
COOLDOWN_HOURS = 4                  # Don't re-trigger same token


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[WHALE-EXIT] {ts} {msg}", flush=True)


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


def _get_open_positions() -> dict:
    data = _load_json(POSITIONS_PATH, {})
    
    # Handle {"positions": [...]} structure
    if isinstance(data, dict) and "positions" in data:
        positions = data["positions"]
    else:
        positions = data
    
    if isinstance(positions, list):
        return {p.get("token", p.get("symbol", "")): p for p in positions if p.get("status", "").upper() == "OPEN"}
    elif isinstance(positions, dict):
        return {k: v for k, v in positions.items() if isinstance(v, dict) and v.get("status", "").upper() == "OPEN"}
    return {}


def _get_whale_signals() -> list:
    """Get recent whale signals from onchain_analytics."""
    state = _load_json(ONCHAIN_STATE, {})
    signals = state.get("signals_emitted", [])

    # Also check for whale transactions in the raw data
    whale_txs = state.get("whale_transactions", [])

    # Filter to recent (within cluster window)
    cutoff = (_now() - timedelta(hours=CLUSTER_WINDOW_HOURS)).isoformat()
    recent = []

    for s in signals:
        ts = s.get("timestamp", s.get("detected_at", ""))
        if ts >= cutoff:
            recent.append(s)

    for tx in whale_txs:
        ts = tx.get("timestamp", tx.get("detected_at", ""))
        if ts >= cutoff:
            recent.append(tx)

    return recent


def check_whale_exits():
    now = _now()
    _log("=== WHALE EXIT CHECK ===")

    positions = _get_open_positions()
    if not positions:
        _log("No open positions — nothing to check")
        return []

    whale_signals = _get_whale_signals()
    exit_state = _load_json(WHALE_EXIT_STATE, {"cooldowns": {}, "alerts": []})
    cooldowns = exit_state.get("cooldowns", {})
    signals_out = []

    for token, pos in positions.items():
        token_clean = token.replace("USDT", "").upper()

        # Check cooldown
        if token_clean in cooldowns:
            try:
                until = datetime.fromisoformat(cooldowns[token_clean])
                if now < until:
                    continue
            except (ValueError, TypeError):
                pass

        # Count whale sells matching this token
        whale_sells = []
        for ws in whale_signals:
            ws_token = ws.get("token", ws.get("symbol", "")).upper().replace("USDT", "")
            ws_type = ws.get("type", ws.get("signal_type", "")).lower()

            # Match by token or general whale activity (BTC/ETH whale dumps affect all)
            is_match = (ws_token == token_clean or
                       ws_token in ("BTC", "ETH") or
                       "whale" in ws_type)
            is_sell = ("sell" in ws_type or "dump" in ws_type or
                      "large_tx" in ws_type or "whale" in ws_type)

            if is_match and is_sell:
                whale_sells.append(ws)

        if not whale_sells:
            continue

        # Determine exit urgency
        exit_signal = None
        count = len(whale_sells)

        if count >= WHALE_CLUSTER_COUNT * 2:
            exit_signal = {
                "urgency": "CRITICAL",
                "reason": f"{count} whale sells detected in {CLUSTER_WINDOW_HOURS}h — mass exit",
                "recommended_action": "IMMEDIATE_EXIT",
            }
        elif count >= WHALE_CLUSTER_COUNT:
            exit_signal = {
                "urgency": "HIGH",
                "reason": f"{count} whale sells in {CLUSTER_WINDOW_HOURS}h — cluster detected",
                "recommended_action": "EXIT_AT_MARKET",
            }
        elif count == 1:
            # Single whale sell — just tighten stop
            exit_signal = {
                "urgency": "NORMAL",
                "reason": f"Single whale sell detected for {token_clean}",
                "recommended_action": "TIGHTEN_STOP",
            }

        if exit_signal:
            signal = {
                "token": token,
                "source": "whale_exit_trigger",
                "type": "EXIT",
                "whale_sell_count": count,
                **exit_signal,
                "position_entry": pos.get("entry_price"),
                "timestamp": now.isoformat(),
            }

            SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
            fname = f"{now.strftime('%Y%m%d_%H%M')}_{token_clean}_whale_exit.json"
            _save_json(SIGNALS_DIR / fname, signal)
            signals_out.append(signal)

            cooldowns[token_clean] = (now + timedelta(hours=COOLDOWN_HOURS)).isoformat()
            _log(f"  EXIT [{exit_signal['urgency']}]: {token} — {exit_signal['reason']}")

    # Save state
    exit_state["cooldowns"] = cooldowns
    exit_state["last_check"] = now.isoformat()
    exit_state["positions_checked"] = len(positions)
    _save_json(WHALE_EXIT_STATE, exit_state)

    _log(f"=== CHECK COMPLETE: {len(signals_out)} exits from {len(positions)} positions ===")
    return signals_out


if __name__ == "__main__":
    check_whale_exits()
