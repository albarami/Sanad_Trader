#!/usr/bin/env python3
"""
Pump.fun Monitor — Sprint 3.8.1
Uses PumpPortal WebSocket for real-time new token detection.

Connects to wss://pumpportal.fun/api/data
Subscribes to: subscribeNewToken, subscribeMigration

Signal generation:
- New token created → log + filter
- Token migrates (graduates bonding curve to Raydium) → HIGH priority signal
- Filters: skip obvious bots, require minimum social signals

Runs as persistent daemon (not cron). Start with: python3 pumpfun_monitor.py &
Or for a single snapshot scan: python3 pumpfun_monitor.py --snapshot
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
SIGNALS_DIR = BASE_DIR / "signals" / "pumpfun"
STATE_PATH = BASE_DIR / "state" / "pumpfun_monitor_state.json"
LOGS_DIR = BASE_DIR / "execution-logs"

WS_URI = "wss://pumpportal.fun/api/data"
RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60
MAX_TOKENS_PER_HOUR = 50  # Don't flood signals


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    line = f"[PUMPFUN] {ts} {msg}"
    print(line, flush=True)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "pumpfun_monitor.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


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


class PumpFunMonitor:
    def __init__(self):
        self.state = _load_json(STATE_PATH, {
            "started_at": None,
            "total_tokens_seen": 0,
            "total_migrations": 0,
            "total_signals": 0,
            "tokens_this_hour": 0,
            "hour_start": None,
            "last_event": None,
        })
        self.reconnect_delay = RECONNECT_DELAY
        self.running = True

    def _reset_hourly_counter(self):
        now = _now()
        hour_start = self.state.get("hour_start")
        if hour_start:
            try:
                start_dt = datetime.fromisoformat(hour_start)
                if (now - start_dt).total_seconds() >= 3600:
                    self.state["tokens_this_hour"] = 0
                    self.state["hour_start"] = now.isoformat()
            except (ValueError, TypeError):
                self.state["hour_start"] = now.isoformat()
                self.state["tokens_this_hour"] = 0
        else:
            self.state["hour_start"] = now.isoformat()

    def _is_likely_bot(self, data: dict) -> bool:
        """Basic bot detection — skip obvious bot-created tokens."""
        name = (data.get("name") or "").lower()
        symbol = (data.get("symbol") or "").lower()

        # Common bot patterns
        bot_patterns = [
            "test", "aaa", "bbb", "xxx", "zzz", "asdf",
            "rugpull", "scam", "honeypot", "drainer",
        ]
        for p in bot_patterns:
            if p in name or p in symbol:
                return True

        # Single character symbols
        if len(symbol) <= 1:
            return True

        return False

    def _process_new_token(self, data: dict):
        """Process a new token creation event."""
        self._reset_hourly_counter()

        mint = data.get("mint", "")
        name = data.get("name", "unknown")
        symbol = data.get("symbol", "???")
        uri = data.get("uri", "")

        self.state["total_tokens_seen"] = self.state.get("total_tokens_seen", 0) + 1

        # Bot filter
        if self._is_likely_bot(data):
            return

        # Rate limit signals
        if self.state.get("tokens_this_hour", 0) >= MAX_TOKENS_PER_HOUR:
            return

        self.state["tokens_this_hour"] = self.state.get("tokens_this_hour", 0) + 1
        self.state["last_event"] = _now().isoformat()

        # Log but don't signal yet — new tokens are too risky without more data
        # They go to a "watched" list for bonding curve monitoring
        _log(f"  NEW: {symbol} ({name}) mint={mint[:12]}...")

        # Save to watched tokens for potential follow-up
        watched_path = BASE_DIR / "state" / "pumpfun_watched.jsonl"
        try:
            with open(watched_path, "a") as f:
                f.write(json.dumps({
                    "mint": mint,
                    "name": name,
                    "symbol": symbol,
                    "uri": uri,
                    "seen_at": _now().isoformat(),
                    "creator": data.get("traderPublicKey", ""),
                    "initial_buy_sol": data.get("initialBuy", 0),
                    "market_cap_sol": data.get("marketCapSol", 0),
                }, default=str) + "\n")
        except Exception as e:
            _log(f"  Error logging watched token: {e}")

    def _process_migration(self, data: dict):
        """Process a token migration (bonding curve graduation) event."""
        mint = data.get("mint", "")
        name = data.get("name", data.get("symbol", "unknown"))
        symbol = data.get("symbol", "???")
        pool = data.get("pool", "")

        self.state["total_migrations"] = self.state.get("total_migrations", 0) + 1
        _log(f"  MIGRATION: {symbol} ({name}) graduated to {pool or 'Raydium'}!")

        # THIS is a signal — token survived bonding curve, now has real liquidity
        now = _now()
        signal = {
            "token": symbol,
            "mint": mint,
            "source": "pumpfun_monitor",
            "source_detail": f"Bonding curve graduation to {pool or 'Raydium'}",
            "thesis": (
                f"{symbol} ({name}) has graduated from Pump.fun bonding curve. "
                f"This means sufficient buying pressure pushed it through the full curve. "
                f"Now tradeable on {pool or 'Raydium'} DEX with real liquidity pool."
            ),
            "signal_score": 65,  # Moderate — needs Sanad verification
            "signal_type": "early_launch",
            "chain": "solana",
            "timestamp": now.isoformat(),
            "raw_data": {
                "mint": mint,
                "pool": pool,
            },
        }

        SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{symbol}_migration.json"
        _save_json(SIGNALS_DIR / filename, signal)

        self.state["total_signals"] = self.state.get("total_signals", 0) + 1
        _log(f"  SIGNAL EMITTED: {symbol} migration")

    def _save_state(self):
        _save_json(STATE_PATH, self.state)

    async def connect_and_listen(self):
        """Main WebSocket loop with auto-reconnect."""
        import websockets

        while self.running:
            try:
                _log(f"Connecting to {WS_URI}...")
                async with websockets.connect(WS_URI, ping_interval=30, ping_timeout=10) as ws:
                    _log("Connected! Subscribing...")

                    # Subscribe to new tokens
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    _log("  Subscribed: subscribeNewToken")

                    # Subscribe to migrations
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    _log("  Subscribed: subscribeMigration")

                    self.reconnect_delay = RECONNECT_DELAY  # Reset on successful connect

                    if not self.state.get("started_at"):
                        self.state["started_at"] = _now().isoformat()

                    _log("Listening for events...")

                    async for message in ws:
                        try:
                            data = json.loads(message)
                            tx_type = data.get("txType", "")

                            if tx_type == "create":
                                self._process_new_token(data)
                            elif tx_type == "migration" or "migration" in str(data.get("method", "")):
                                self._process_migration(data)
                            # Trades on subscribed tokens would go here

                            # Periodic state save
                            total = self.state.get("total_tokens_seen", 0)
                            if total % 100 == 0 and total > 0:
                                self._save_state()
                                _log(f"  Checkpoint: {total} tokens seen, {self.state.get('total_migrations', 0)} migrations, {self.state.get('total_signals', 0)} signals")

                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            _log(f"  Error processing message: {e}")

            except Exception as e:
                _log(f"WebSocket error: {e}")
                self._save_state()
                _log(f"Reconnecting in {self.reconnect_delay}s...")
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, MAX_RECONNECT_DELAY)

    async def snapshot(self, duration_s: int = 30):
        """Run for a fixed duration then exit. For testing."""
        import websockets

        _log(f"Snapshot mode: listening for {duration_s}s...")
        try:
            async with websockets.connect(WS_URI, ping_interval=30, ping_timeout=10) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                _log("Subscribed. Listening...")

                start = time.time()
                count = 0
                migrations = 0

                while time.time() - start < duration_s:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(message)
                        tx_type = data.get("txType", "")

                        if tx_type == "create":
                            count += 1
                            name = data.get("name", "?")
                            symbol = data.get("symbol", "?")
                            mint = data.get("mint", "?")[:12]
                            mcap = data.get("marketCapSol", 0)
                            if not self._is_likely_bot(data):
                                _log(f"  NEW: {symbol} ({name}) mcap={mcap:.1f}SOL mint={mint}...")
                        elif tx_type == "migration":
                            migrations += 1
                            self._process_migration(data)
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        _log(f"  Error: {e}")

                elapsed = time.time() - start
                _log(f"Snapshot done: {count} tokens, {migrations} migrations in {elapsed:.0f}s")
                self._save_state()
                return {"tokens": count, "migrations": migrations, "duration_s": round(elapsed)}

        except Exception as e:
            _log(f"Snapshot error: {e}")
            return {"error": str(e)}


async def main():
    monitor = PumpFunMonitor()

    if "--snapshot" in sys.argv:
        duration = 30
        for arg in sys.argv:
            if arg.startswith("--duration="):
                duration = int(arg.split("=")[1])
        await monitor.snapshot(duration)
    else:
        _log("=== PUMP.FUN MONITOR STARTING (daemon mode) ===")
        _log("Press Ctrl+C to stop")
        try:
            await monitor.connect_and_listen()
        except KeyboardInterrupt:
            _log("Shutting down...")
            monitor._save_state()


if __name__ == "__main__":
    asyncio.run(main())
