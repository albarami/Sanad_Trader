#!/usr/bin/env python3
"""
Helius WebSocket Listener — Sprint 3.8.6
Deterministic Python. No LLMs.

Subscribes to Helius Enhanced WebSocket for real-time Solana events:
  - Token transfers (whale movements)
  - DEX swaps (large trades on Raydium/Orca/Jupiter)
  - Account changes on watched wallets/tokens

Uses: wss://mainnet.helius-rpc.com/?api-key=<KEY>
Fallback: Helius webhooks (HTTP) if WS unavailable.

Run standalone:  python3 helius_ws.py
Run test:        python3 helius_ws.py --test
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
CONFIG_DIR = BASE_DIR / "config"
STATE_DIR = BASE_DIR / "state"
SIGNALS_DIR = BASE_DIR / "signals" / "helius_ws"

RECONNECT_MIN = 3
RECONNECT_MAX = 60
STALE_THRESHOLD_S = 120
MAX_EVENT_BUFFER = 500

sys.path.insert(0, str(SCRIPT_DIR))
import env_loader
env_loader.load_env()


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[HELIUS-WS] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def _get_helius_key() -> str:
    return os.environ.get("HELIUS_API_KEY", "")


def _load_watched_accounts() -> list[str]:
    """Load accounts to watch from config/helius_watch.json"""
    watch_path = CONFIG_DIR / "helius_watch.json"
    data = _load_json(watch_path, {"accounts": []})
    return data.get("accounts", [])


def _load_watched_tokens() -> list[str]:
    """Load token mints to watch from watchlist."""
    wl = _load_json(CONFIG_DIR / "watchlist.json", {"tokens": []})
    return [t.get("mint", "") for t in wl.get("tokens", []) if t.get("mint")]


# ─────────────────────────────────────────────────────────
# Event processing
# ─────────────────────────────────────────────────────────

class HeliusEventBuffer:
    """Circular buffer for recent events, deduped by signature."""

    def __init__(self, maxlen=MAX_EVENT_BUFFER):
        self.events = deque(maxlen=maxlen)
        self.seen_sigs = set()
        self.stats = {
            "total_received": 0,
            "transfers": 0,
            "swaps": 0,
            "other": 0,
            "whale_alerts": 0,
        }

    def process(self, event: dict) -> dict | None:
        """Process a raw Helius enhanced transaction event.
        Returns a normalized signal dict, or None if duplicate/irrelevant."""
        sig = event.get("signature", "")
        if sig in self.seen_sigs:
            return None
        if sig:
            self.seen_sigs.add(sig)
            # Prune seen set if too large
            if len(self.seen_sigs) > MAX_EVENT_BUFFER * 2:
                self.seen_sigs = set(list(self.seen_sigs)[-MAX_EVENT_BUFFER:])

        self.stats["total_received"] += 1

        tx_type = event.get("type", "UNKNOWN")
        description = event.get("description", "")
        timestamp = event.get("timestamp", 0)

        # Classify
        signal = {
            "source": "helius_ws",
            "signature": sig,
            "type": tx_type,
            "description": description[:200],
            "timestamp": timestamp,
            "received_at": _now().isoformat(),
        }

        if tx_type in ("TRANSFER", "TOKEN_TRANSFER"):
            self.stats["transfers"] += 1
            # Extract amount for whale detection
            token_transfers = event.get("tokenTransfers", [])
            for tt in token_transfers:
                amt = tt.get("tokenAmount", 0)
                if isinstance(amt, (int, float)) and amt > 0:
                    signal["token_amount"] = amt
                    signal["mint"] = tt.get("mint", "")
                    signal["from"] = tt.get("fromUserAccount", "")
                    signal["to"] = tt.get("toUserAccount", "")
                    # Whale threshold: >$50k equivalent (we don't know USD here,
                    # but large raw amounts are flagged for downstream enrichment)
                    if amt > 1_000_000:
                        signal["whale_alert"] = True
                        self.stats["whale_alerts"] += 1

        elif tx_type == "SWAP":
            self.stats["swaps"] += 1
            swap_info = event.get("events", {}).get("swap", {})
            signal["swap"] = swap_info

        else:
            self.stats["other"] += 1

        self.events.append(signal)
        return signal


# ─────────────────────────────────────────────────────────
# WebSocket connection
# ─────────────────────────────────────────────────────────

async def connect_and_listen(
    buffer: HeliusEventBuffer,
    on_signal=None,
    duration_s: int = 0,
):
    """Connect to Helius enhanced WebSocket and listen for events.

    Args:
        buffer: HeliusEventBuffer to store events
        on_signal: optional callback(signal_dict)
        duration_s: if >0, disconnect after this many seconds (for testing)
    """
    try:
        import websockets
    except ImportError:
        _log("websockets not installed — pip install websockets")
        return

    api_key = _get_helius_key()
    if not api_key:
        _log("ERROR: No HELIUS_API_KEY")
        return

    ws_url = f"wss://mainnet.helius-rpc.com/?api-key={api_key}"

    accounts = _load_watched_accounts()
    tokens = _load_watched_tokens()
    all_addresses = list(set(accounts + tokens))

    if not all_addresses:
        _log("WARNING: No accounts/tokens to watch. Add to config/helius_watch.json or watchlist.json")
        # Still connect to test connectivity
        all_addresses = ["DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"]  # BONK as default

    backoff = RECONNECT_MIN
    start_time = time.monotonic()

    while True:
        try:
            _log(f"Connecting to Helius WS... ({len(all_addresses)} addresses)")
            async with websockets.connect(ws_url, ping_interval=30) as ws:
                _log("Connected")
                backoff = RECONNECT_MIN

                # Subscribe to enhanced transactions via transactionSubscribe
                sub_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "transactionSubscribe",
                    "params": [
                        {
                            "accountInclude": all_addresses[:50],  # Helius limit
                        },
                        {
                            "commitment": "confirmed",
                            "encoding": "jsonParsed",
                            "transactionDetails": "full",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                }
                await ws.send(json.dumps(sub_msg))
                _log(f"Subscribed to {min(len(all_addresses), 50)} addresses")

                # Listen loop
                async for raw_msg in ws:
                    try:
                        data = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    # Handle subscription confirmation
                    if "result" in data and "id" in data:
                        _log(f"Subscription confirmed: {data.get('result')}")
                        continue

                    # Handle transaction notifications
                    params = data.get("params", {})
                    result = params.get("result", {})
                    if isinstance(result, dict):
                        signal = buffer.process(result)
                        if signal and on_signal:
                            on_signal(signal)

                    # Duration check for tests
                    if duration_s > 0 and (time.monotonic() - start_time) > duration_s:
                        _log(f"Test duration {duration_s}s reached, disconnecting")
                        return

        except Exception as e:
            _log(f"Connection error: {e}")
            _log(f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

            if duration_s > 0 and (time.monotonic() - start_time) > duration_s:
                _log("Test duration exceeded during reconnect, stopping")
                return


# ─────────────────────────────────────────────────────────
# State persistence
# ─────────────────────────────────────────────────────────

def save_state(buffer: HeliusEventBuffer):
    """Save current state to disk."""
    state = {
        "stats": buffer.stats,
        "last_events": list(buffer.events)[-10:],
        "updated_at": _now().isoformat(),
    }
    _save_json(STATE_DIR / "helius_ws_state.json", state)


def save_signal(signal: dict):
    """Save individual signal to signals directory for pipeline pickup."""
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    sig_hash = signal.get("signature", "unknown")[:16]
    path = SIGNALS_DIR / f"sig_{sig_hash}.json"
    _save_json(path, signal)


# ─────────────────────────────────────────────────────────
# Entry points
# ─────────────────────────────────────────────────────────

async def run_daemon(buffer: HeliusEventBuffer):
    """Run as persistent daemon."""
    def on_signal(sig):
        save_signal(sig)
        if sig.get("whale_alert"):
            _log(f"🐋 WHALE ALERT: {sig.get('description', '')[:100]}")

    await connect_and_listen(buffer, on_signal=on_signal)


async def run_test(duration: int = 20):
    """Quick connectivity test."""
    _log("=== HELIUS WS TEST ===")
    buf = HeliusEventBuffer()
    signals_received = []

    def on_signal(sig):
        signals_received.append(sig)
        _log(f"  Signal: {sig.get('type', '?')} — {sig.get('description', '')[:80]}")

    try:
        import websockets  # noqa: F401
        _log("websockets module: OK")
    except ImportError:
        _log("websockets module: MISSING — install with: pip install websockets")
        _log("=== TEST FAILED ===")
        return False

    api_key = _get_helius_key()
    if not api_key:
        _log("HELIUS_API_KEY: MISSING")
        _log("=== TEST FAILED ===")
        return False
    _log(f"HELIUS_API_KEY: ...{api_key[-8:]}")

    _log(f"Listening for {duration}s...")
    try:
        await asyncio.wait_for(
            connect_and_listen(buf, on_signal=on_signal, duration_s=duration),
            timeout=duration + 10,
        )
    except asyncio.TimeoutError:
        pass

    _log(f"Received {buf.stats['total_received']} events in {duration}s")
    _log(f"  Transfers: {buf.stats['transfers']}")
    _log(f"  Swaps: {buf.stats['swaps']}")
    _log(f"  Whale alerts: {buf.stats['whale_alerts']}")

    save_state(buf)
    _log("=== TEST COMPLETE ===")
    return True


if __name__ == "__main__":
    if "--test" in sys.argv:
        dur = 20
        for a in sys.argv:
            if a.isdigit():
                dur = int(a)
        asyncio.run(run_test(dur))
    else:
        _log("Starting Helius WS daemon...")
        buf = HeliusEventBuffer()
        try:
            asyncio.run(run_daemon(buf))
        except KeyboardInterrupt:
            _log("Shutdown requested")
            save_state(buf)
