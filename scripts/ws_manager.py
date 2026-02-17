#!/usr/bin/env python3
"""
WebSocket Manager — Sprint 3.8.7 + 3.8.8 + 3.8.9
Manages persistent WebSocket connections for real-time data.

Streams:
- Binance: trade streams for watchlist tokens
- MEXC: trade streams for watchlist tokens
- PumpPortal: managed separately (pumpfun_monitor.py)

Features:
- Auto-reconnect with exponential backoff
- Health monitoring (last message timestamp)
- Graceful degradation (each stream independent)
- State file for supervisor monitoring

Run as daemon: python3 ws_manager.py &
Or single stream test: python3 ws_manager.py --test binance
                       python3 ws_manager.py --test mexc
"""
import asyncio
import json
import os
import sys
import time
import signal as sig
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
CONFIG_DIR = BASE_DIR / "config"
STATE_DIR = BASE_DIR / "state"
WS_STATE_PATH = STATE_DIR / "ws_manager_state.json"
PRICE_CACHE_PATH = STATE_DIR / "price_cache.json"
WATCHLIST_PATH = CONFIG_DIR / "watchlist.json"

BINANCE_WS = "wss://stream.binance.com:9443/ws"
MEXC_WS = "wss://wbs.mexc.com/ws"

RECONNECT_MIN = 3
RECONNECT_MAX = 60
HEALTH_CHECK_INTERVAL = 30
STALE_THRESHOLD_S = 120  # 2 min without message = stale


def _log(tag, msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[WS-{tag}] {ts} {msg}", flush=True)


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


def _load_watchlist_symbols() -> list[str]:
    data = _load_json(WATCHLIST_PATH, {})
    if isinstance(data, dict):
        symbols = data.get("symbols", [])
        return [s.get("symbol", s) if isinstance(s, dict) else s
                for s in symbols
                if (s.get("symbol", s) if isinstance(s, dict) else s).endswith("USDT")]
    return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "PEPEUSDT", "WIFUSDT"]


def _update_price_cache(symbol: str, price: float, source: str):
    """Update price cache with real-time WebSocket price."""
    cache = _load_json(PRICE_CACHE_PATH, {})
    cache[symbol] = {
        "price": price,
        "source": source,
        "timestamp": _now().isoformat(),
    }
    _save_json(PRICE_CACHE_PATH, cache)


class StreamState:
    """Track state for a single WebSocket stream."""
    def __init__(self, name: str):
        self.name = name
        self.connected = False
        self.last_message_at = None
        self.reconnect_delay = RECONNECT_MIN
        self.total_messages = 0
        self.total_reconnects = 0
        self.last_error = None

    def to_dict(self):
        return {
            "name": self.name,
            "connected": self.connected,
            "last_message_at": self.last_message_at,
            "total_messages": self.total_messages,
            "total_reconnects": self.total_reconnects,
            "last_error": self.last_error,
            "stale": self.is_stale(),
        }

    def is_stale(self) -> bool:
        if not self.last_message_at:
            return True
        try:
            last = datetime.fromisoformat(self.last_message_at)
            return (_now() - last).total_seconds() > STALE_THRESHOLD_S
        except (ValueError, TypeError):
            return True

    def record_message(self):
        self.last_message_at = _now().isoformat()
        self.total_messages += 1

    def record_connect(self):
        self.connected = True
        self.reconnect_delay = RECONNECT_MIN
        _log(self.name, "Connected")

    def record_disconnect(self, error=None):
        self.connected = False
        self.total_reconnects += 1
        self.last_error = str(error) if error else None
        _log(self.name, f"Disconnected: {error}")


# ─────────────────────────────────────────────────────
# Binance WebSocket
# ─────────────────────────────────────────────────────

async def binance_stream(state: StreamState, symbols: list[str]):
    """Connect to Binance combined trade streams."""
    import websockets

    # Build combined stream URL
    streams = [f"{s.lower()}@trade" for s in symbols[:10]]  # Max 10
    url = f"{BINANCE_WS}/{'/'.join(streams)}" if len(streams) == 1 else \
        f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    while True:
        try:
            _log("BINANCE", f"Connecting to {len(streams)} streams...")
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                state.record_connect()

                async for message in ws:
                    try:
                        data = json.loads(message)
                        # Combined stream format
                        if "data" in data:
                            data = data["data"]

                        event = data.get("e", "")
                        if event == "trade":
                            symbol = data.get("s", "")
                            price = float(data.get("p", 0))
                            qty = float(data.get("q", 0))
                            if price > 0:
                                _update_price_cache(symbol, price, "binance_ws")
                                state.record_message()

                                # Log every 100th message
                                if state.total_messages % 100 == 0:
                                    _log("BINANCE", f"  {symbol} ${price:.6f} (msg #{state.total_messages})")
                    except (json.JSONDecodeError, KeyError, ValueError):
                        pass

        except Exception as e:
            state.record_disconnect(e)
            delay = min(state.reconnect_delay * 2, RECONNECT_MAX)
            state.reconnect_delay = delay
            _log("BINANCE", f"Reconnecting in {delay}s...")
            await asyncio.sleep(delay)


# ─────────────────────────────────────────────────────
# MEXC WebSocket
# ─────────────────────────────────────────────────────

async def mexc_stream(state: StreamState, symbols: list[str]):
    """Connect to MEXC trade streams."""
    import websockets

    while True:
        try:
            _log("MEXC", f"Connecting for {len(symbols)} symbols...")
            async with websockets.connect(MEXC_WS, ping_interval=20, ping_timeout=10) as ws:
                state.record_connect()

                # Subscribe to trade channels
                for sym in symbols[:10]:
                    sub_msg = {
                        "method": "SUBSCRIPTION",
                        "params": [f"spot@public.deals.v3.api@{sym}"],
                    }
                    await ws.send(json.dumps(sub_msg))

                # MEXC requires periodic ping
                last_ping = time.time()

                async for message in ws:
                    try:
                        # Send ping every 30s
                        if time.time() - last_ping > 30:
                            await ws.send(json.dumps({"method": "PING"}))
                            last_ping = time.time()

                        data = json.loads(message)

                        # Skip pong responses
                        if data.get("msg") == "PONG" or data.get("id") == 0:
                            continue

                        channel = data.get("c", "")
                        if "deals" in channel and "d" in data:
                            deals = data["d"].get("deals", [])
                            for deal in deals:
                                price = float(deal.get("p", 0))
                                symbol = data.get("s", channel.split("@")[0] if "@" in channel else "")
                                if price > 0 and symbol:
                                    _update_price_cache(symbol, price, "mexc_ws")
                                    state.record_message()

                                    if state.total_messages % 100 == 0:
                                        _log("MEXC", f"  {symbol} ${price:.6f} (msg #{state.total_messages})")
                    except (json.JSONDecodeError, KeyError, ValueError):
                        pass

        except Exception as e:
            state.record_disconnect(e)
            delay = min(state.reconnect_delay * 2, RECONNECT_MAX)
            state.reconnect_delay = delay
            _log("MEXC", f"Reconnecting in {delay}s...")
            await asyncio.sleep(delay)


# ─────────────────────────────────────────────────────
# Health Monitor
# ─────────────────────────────────────────────────────

async def health_monitor(states: dict[str, StreamState]):
    """Periodically check stream health and update state file."""
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        status = {
            "updated_at": _now().isoformat(),
            "streams": {name: s.to_dict() for name, s in states.items()},
        }

        # Check for stale streams
        for name, s in states.items():
            if s.connected and s.is_stale():
                _log("HEALTH", f"{name} is STALE (no messages for {STALE_THRESHOLD_S}s)")

        _save_json(WS_STATE_PATH, status)


# ─────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────

async def run_all():
    """Run all WebSocket streams + health monitor."""
    symbols = _load_watchlist_symbols()
    _log("MAIN", f"Starting WebSocket manager for {len(symbols)} symbols")

    states = {
        "binance": StreamState("binance"),
        "mexc": StreamState("mexc"),
    }

    tasks = [
        asyncio.create_task(binance_stream(states["binance"], symbols)),
        asyncio.create_task(mexc_stream(states["mexc"], symbols)),
        asyncio.create_task(health_monitor(states)),
    ]

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    for s in (sig.SIGINT, sig.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda: [t.cancel() for t in tasks])
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        _log("MAIN", "Shutting down...")
        final_state = {
            "updated_at": _now().isoformat(),
            "streams": {name: s.to_dict() for name, s in states.items()},
            "shutdown": True,
        }
        _save_json(WS_STATE_PATH, final_state)


async def test_stream(exchange: str, duration: int = 15):
    """Test a single stream for a fixed duration."""
    import websockets

    symbols = _load_watchlist_symbols()[:3]  # Test with 3 symbols
    state = StreamState(exchange)
    _log("TEST", f"Testing {exchange} with {symbols} for {duration}s...")

    if exchange == "binance":
        streams = [f"{s.lower()}@trade" for s in symbols]
        url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                state.record_connect()
                start = time.time()
                while time.time() - start < duration:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(msg)
                        if "data" in data:
                            data = data["data"]
                        if data.get("e") == "trade":
                            sym = data.get("s", "")
                            price = float(data.get("p", 0))
                            state.record_message()
                            _log("TEST", f"  {sym} ${price:.8f}")
                    except asyncio.TimeoutError:
                        continue
                _log("TEST", f"Done: {state.total_messages} messages in {duration}s")
        except Exception as e:
            _log("TEST", f"Error: {e}")

    elif exchange == "mexc":
        try:
            async with websockets.connect(MEXC_WS, ping_interval=20, ping_timeout=10) as ws:
                state.record_connect()
                for sym in symbols:
                    await ws.send(json.dumps({
                        "method": "SUBSCRIPTION",
                        "params": [f"spot@public.deals.v3.api@{sym}"],
                    }))
                start = time.time()
                while time.time() - start < duration:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(msg)
                        if data.get("msg") == "PONG" or not data.get("d"):
                            continue
                        deals = data.get("d", {}).get("deals", [])
                        for d in deals:
                            price = float(d.get("p", 0))
                            state.record_message()
                            _log("TEST", f"  ${price:.8f}")
                    except asyncio.TimeoutError:
                        continue
                _log("TEST", f"Done: {state.total_messages} messages in {duration}s")
        except Exception as e:
            _log("TEST", f"Error: {e}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        exchange = sys.argv[sys.argv.index("--test") + 1] if len(sys.argv) > sys.argv.index("--test") + 1 else "binance"
        duration = 15
        for arg in sys.argv:
            if arg.startswith("--duration="):
                duration = int(arg.split("=")[1])
        asyncio.run(test_stream(exchange, duration))
    elif "--status" in sys.argv:
        state = _load_json(WS_STATE_PATH, {})
        print(json.dumps(state, indent=2))
    else:
        _log("MAIN", "=== WEBSOCKET MANAGER STARTING ===")
        asyncio.run(run_all())
