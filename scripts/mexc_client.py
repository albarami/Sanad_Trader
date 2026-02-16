#!/usr/bin/env python3
"""
Sanad Trader v3.0 — MEXC Exchange Client

Sprint 4.2 — Exchange API Wrapper

Provides:
1. Market data (prices, order book, klines)
2. Account data (balances, open orders, order status)
3. Order execution (paper mode simulates fills, live mode hits API)
4. Error tracking feeding circuit breakers

Credentials loaded from trading/config/.env (gitignored).
All methods fail-closed: errors return None and increment circuit breaker counter.

References:
- MEXC API v3: https://mexcdevelop.github.io/apidocs/spot_v3_en/
- HMAC-SHA256 signing for authenticated endpoints
- Paper mode: simulate fees + slippage from real order book
"""

import os
import sys
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
CONFIG_ENV = BASE_DIR / "config" / ".env"
STATE_DIR = BASE_DIR / "state"

MEXC_BASE_URL = "https://api.mexc.com"
MEXC_API_KEY = ""
MEXC_API_SECRET = ""

# Load credentials
if CONFIG_ENV.exists():
    for line in CONFIG_ENV.read_text().splitlines():
        if line.startswith("MEXC_API_KEY="):
            MEXC_API_KEY = line.split("=", 1)[1].strip()
        elif line.startswith("MEXC_API_SECRET="):
            MEXC_API_SECRET = line.split("=", 1)[1].strip()

# Lazy import to avoid top-level failure in test envs
import requests


def _log(msg: str):
    print(f"[MEXC] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Error Tracker (Circuit Breaker)
# ---------------------------------------------------------------------------
class ErrorTracker:
    """
    Tracks errors for circuit breaker logic.
    Trip: 3 consecutive failures → 5 min cooldown.
    """

    def __init__(self, component="mexc_api", window_sec=60, trip_threshold=3):
        self.component = component
        self.window_sec = window_sec
        self.trip_threshold = trip_threshold
        self.errors = deque()
        self.total_requests = 0
        self.total_errors = 0
        self._tripped = False
        self._cooldown_until = None
        self._consecutive_failures = 0

    def record_success(self):
        self.total_requests += 1
        self._consecutive_failures = 0

    def record_error(self, error_msg=""):
        now = time.time()
        self.total_requests += 1
        self.total_errors += 1
        self._consecutive_failures += 1
        self.errors.append(now)

        # Clean old errors outside window
        cutoff = now - self.window_sec
        while self.errors and self.errors[0] < cutoff:
            self.errors.popleft()

        if self._consecutive_failures >= self.trip_threshold:
            self._tripped = True
            self._cooldown_until = now + 300  # 5 minutes
            _log(f"⚠️ Circuit breaker TRIPPED ({self.component}): "
                 f"{self._consecutive_failures} consecutive failures → 5min cooldown")

    def is_tripped(self):
        if not self._tripped:
            return False
        if time.time() >= self._cooldown_until:
            self._tripped = False
            self._consecutive_failures = 0
            _log(f"Circuit breaker RESET ({self.component})")
            return False
        return True

    def time_remaining(self):
        if not self._tripped or not self._cooldown_until:
            return 0
        return max(0, int(self._cooldown_until - time.time()))


_tracker = ErrorTracker()

# ---------------------------------------------------------------------------
# Rate limiter: max 20 req/s
# ---------------------------------------------------------------------------
MAX_CALLS_PER_SECOND = 20
_call_timestamps: list[float] = []


def _rate_limit():
    global _call_timestamps
    now = time.time()
    _call_timestamps = [t for t in _call_timestamps if now - t < 1.0]
    if len(_call_timestamps) >= MAX_CALLS_PER_SECOND:
        sleep_for = 1.0 - (now - _call_timestamps[0]) + 0.05
        if sleep_for > 0:
            time.sleep(sleep_for)
    _call_timestamps.append(time.time())


# ---------------------------------------------------------------------------
# HMAC-SHA256 Signing
# ---------------------------------------------------------------------------
def _sign_params(params: dict) -> dict:
    """Add timestamp and HMAC-SHA256 signature to params."""
    params["timestamp"] = str(int(time.time() * 1000))
    query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature = hmac.new(
        MEXC_API_SECRET.encode(),
        query_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    params["signature"] = signature
    return params


# ---------------------------------------------------------------------------
# Core request handler
# ---------------------------------------------------------------------------
def _request(method: str, endpoint: str, params=None, signed=False, timeout=10):
    """
    Send request to MEXC API with error handling, rate limiting, circuit breaker.
    Returns parsed JSON or None on failure.
    """
    if _tracker.is_tripped():
        _log(f"Circuit breaker OPEN — {_tracker.time_remaining()}s remaining. Skipping {endpoint}")
        return None

    _rate_limit()

    if params is None:
        params = {}

    headers = {}
    if signed:
        headers["X-MEXC-APIKEY"] = MEXC_API_KEY
        params = _sign_params(params)

    url = f"{MEXC_BASE_URL}{endpoint}"

    try:
        if method == "GET":
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        elif method == "POST":
            resp = requests.post(url, params=params, headers=headers, timeout=timeout)
        elif method == "DELETE":
            resp = requests.delete(url, params=params, headers=headers, timeout=timeout)
        else:
            _log(f"Unsupported method: {method}")
            return None

        if resp.status_code == 429:
            _log(f"Rate limited (429) on {endpoint}. Backing off 2s.")
            _tracker.record_error("rate_limited")
            time.sleep(2)
            return None

        if resp.status_code != 200:
            _log(f"HTTP {resp.status_code} on {endpoint}: {resp.text[:200]}")
            _tracker.record_error(f"http_{resp.status_code}")
            return None

        data = resp.json()

        # MEXC returns errors as {"code": ..., "msg": ...}
        if isinstance(data, dict) and data.get("code") and data["code"] != 200 and data["code"] != 0:
            _log(f"API error on {endpoint}: code={data.get('code')}, msg={data.get('msg', '')}")
            _tracker.record_error(f"api_{data.get('code')}")
            return None

        _tracker.record_success()
        return data

    except requests.exceptions.Timeout:
        _log(f"Timeout on {endpoint}")
        _tracker.record_error("timeout")
        return None
    except requests.exceptions.ConnectionError as e:
        _log(f"Connection error on {endpoint}: {e}")
        _tracker.record_error("connection")
        return None
    except requests.exceptions.RequestException as e:
        _log(f"Request error on {endpoint}: {e}")
        _tracker.record_error(str(e))
        return None
    except json.JSONDecodeError:
        _log(f"Invalid JSON from {endpoint}")
        _tracker.record_error("json_decode")
        return None


# ---------------------------------------------------------------------------
# 1. get_price
# ---------------------------------------------------------------------------
def get_price(symbol: str) -> float | None:
    """Get current price for a symbol (e.g., BTCUSDT)."""
    data = _request("GET", "/api/v3/ticker/price", {"symbol": symbol.upper()})
    if data and "price" in data:
        return float(data["price"])
    return None


# ---------------------------------------------------------------------------
# 2. get_orderbook
# ---------------------------------------------------------------------------
def get_orderbook(symbol: str, depth: int = 20) -> dict | None:
    """
    Get order book for a symbol.
    Returns: {bids: [[price, qty], ...], asks: [[price, qty], ...], spread_bps: float}
    """
    data = _request("GET", "/api/v3/depth", {"symbol": symbol.upper(), "limit": depth})
    if not data:
        return None

    bids = data.get("bids", [])
    asks = data.get("asks", [])

    spread_bps = None
    if bids and asks:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if best_bid > 0:
            spread_bps = round((best_ask - best_bid) / best_bid * 10000, 1)

    return {
        "bids": bids,
        "asks": asks,
        "bid_depth_usd": sum(float(b[0]) * float(b[1]) for b in bids),
        "ask_depth_usd": sum(float(a[0]) * float(a[1]) for a in asks),
        "spread_bps": spread_bps,
    }


# ---------------------------------------------------------------------------
# 3. get_klines
# ---------------------------------------------------------------------------
def get_klines(symbol: str, interval: str = "1h", limit: int = 100) -> list | None:
    """
    Get candlestick/kline data.
    Intervals: 1m, 5m, 15m, 30m, 60m, 4h, 1d, 1W, 1M
    Returns list of {open_time, open, high, low, close, volume}.
    """
    data = _request("GET", "/api/v3/klines", {
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": limit,
    })
    if not data or not isinstance(data, list):
        return None

    klines = []
    for k in data:
        klines.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": k[6],
        })
    return klines


# ---------------------------------------------------------------------------
# 4. get_account_balance
# ---------------------------------------------------------------------------
def get_account_balance() -> dict | None:
    """
    Get account balances (authenticated).
    Returns: {asset: {free: float, locked: float}, ...} for non-zero balances.
    """
    data = _request("GET", "/api/v3/account", signed=True)
    if not data:
        return None

    balances = {}
    for b in data.get("balances", []):
        free = float(b.get("free", 0))
        locked = float(b.get("locked", 0))
        if free > 0 or locked > 0:
            balances[b["asset"]] = {"free": free, "locked": locked}

    return balances


# ---------------------------------------------------------------------------
# 5. place_order
# ---------------------------------------------------------------------------
def place_order(symbol: str, side: str, quantity: float,
                order_type: str = "MARKET", price: float | None = None,
                paper_mode: bool = True) -> dict | None:
    """
    Place an order on MEXC.

    In PAPER mode: simulates fill from real order book with 0.1% fee + slippage.
    In LIVE mode: sends real order to MEXC API.

    Args:
        symbol: e.g., 'BTCUSDT'
        side: 'BUY' or 'SELL'
        quantity: amount of base asset
        order_type: 'MARKET' or 'LIMIT'
        price: required for LIMIT orders
        paper_mode: if True, simulate instead of executing

    Returns:
        Order result dict or None on failure.
    """
    if paper_mode:
        return _paper_order(symbol, side, quantity, order_type, price)
    else:
        return _live_order(symbol, side, quantity, order_type, price)


def _paper_order(symbol: str, side: str, quantity: float,
                 order_type: str, price: float | None) -> dict | None:
    """
    Simulate order using real order book data.
    Models 0.1% trading fee and realistic slippage.
    """
    current_price = get_price(symbol)
    if not current_price:
        _log(f"PAPER: Cannot get price for {symbol}")
        return None

    # Get real order book for slippage simulation
    book = get_orderbook(symbol, depth=50)
    fill_price = current_price

    if book:
        levels = book["asks"] if side.upper() == "BUY" else book["bids"]
        if levels:
            filled_qty = 0.0
            total_cost = 0.0
            for level_price_str, level_qty_str in levels:
                level_price = float(level_price_str)
                level_qty = float(level_qty_str)
                remaining = quantity - filled_qty
                fill_at_level = min(remaining, level_qty)
                total_cost += fill_at_level * level_price
                filled_qty += fill_at_level
                if filled_qty >= quantity:
                    break
            if filled_qty > 0:
                fill_price = total_cost / filled_qty

    # Apply 0.1% maker/taker fee
    fee_rate = 0.001
    fee_usd = fill_price * quantity * fee_rate

    order_id = f"PAPER-MEXC-{int(time.time() * 1000)}"
    now_iso = datetime.now(timezone.utc).isoformat()

    order = {
        "orderId": order_id,
        "symbol": symbol.upper(),
        "side": side.upper(),
        "type": order_type,
        "quantity": quantity,
        "price": fill_price,
        "fee_usd": round(fee_usd, 4),
        "fee_rate": fee_rate,
        "status": "FILLED",
        "exchange": "mexc",
        "paper_mode": True,
        "timestamp": now_iso,
        "total_usd": round(fill_price * quantity, 4),
    }

    _log(f"PAPER ORDER: {side} {quantity} {symbol} @ ${fill_price:,.4f} "
         f"(fee: ${fee_usd:.4f}) total: ${fill_price * quantity:,.2f}")

    # Log to execution logs
    _log_paper_order(order)

    return order


def _live_order(symbol: str, side: str, quantity: float,
                order_type: str, price: float | None) -> dict | None:
    """Send a real order to MEXC API."""
    params = {
        "symbol": symbol.upper(),
        "side": side.upper(),
        "type": order_type.upper(),
        "quantity": str(quantity),
    }
    if order_type.upper() == "LIMIT" and price is not None:
        params["price"] = str(price)
        params["timeInForce"] = "GTC"

    data = _request("POST", "/api/v3/order", params, signed=True)
    if data:
        _log(f"LIVE ORDER: {side} {quantity} {symbol} → orderId={data.get('orderId')}")
    return data


def _log_paper_order(order: dict):
    """Log paper order to execution logs directory."""
    logs_dir = BASE_DIR / "execution-logs"
    logs_dir.mkdir(exist_ok=True)

    log_file = logs_dir / "mexc_paper_orders.json"
    orders = []
    if log_file.exists():
        try:
            orders = json.loads(log_file.read_text())
        except (json.JSONDecodeError, IOError):
            orders = []

    orders.append(order)

    try:
        log_file.write_text(json.dumps(orders, indent=2))
    except IOError as e:
        _log(f"Error writing paper order log: {e}")


# ---------------------------------------------------------------------------
# 6. cancel_order
# ---------------------------------------------------------------------------
def cancel_order(symbol: str, order_id: str) -> dict | None:
    """Cancel an open order by orderId."""
    data = _request("DELETE", "/api/v3/order", {
        "symbol": symbol.upper(),
        "orderId": order_id,
    }, signed=True)
    if data:
        _log(f"Cancelled order {order_id} on {symbol}")
    return data


# ---------------------------------------------------------------------------
# 7. get_open_orders
# ---------------------------------------------------------------------------
def get_open_orders(symbol: str = None) -> list | None:
    """Get all open orders, optionally filtered by symbol."""
    params = {}
    if symbol:
        params["symbol"] = symbol.upper()
    return _request("GET", "/api/v3/openOrders", params, signed=True)


# ---------------------------------------------------------------------------
# 8. get_order_status
# ---------------------------------------------------------------------------
def get_order_status(symbol: str, order_id: str) -> dict | None:
    """Check status of an order by orderId."""
    return _request("GET", "/api/v3/order", {
        "symbol": symbol.upper(),
        "orderId": order_id,
    }, signed=True)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
def health_check() -> dict:
    """Quick health check for MEXC connectivity and account access."""
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "exchange": "mexc",
        "circuit_breaker_tripped": _tracker.is_tripped(),
        "circuit_breaker_remaining": _tracker.time_remaining(),
        "total_requests": _tracker.total_requests,
        "total_errors": _tracker.total_errors,
    }

    # Test public endpoint
    price = get_price("BTCUSDT")
    results["public_api"] = "OK" if price else "FAIL"
    results["btc_price"] = price

    # Test authenticated endpoint
    balance = get_account_balance()
    results["authenticated_api"] = "OK" if balance is not None else "FAIL"

    return results


# ---------------------------------------------------------------------------
# Standalone report
# ---------------------------------------------------------------------------
def run_report(symbol: str):
    now = datetime.now(timezone.utc)
    _log(now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    _log(f"Report for: {symbol}")
    _log("")

    # Price
    _log("── Price ──")
    price = get_price(symbol)
    if price:
        _log(f"  {symbol}: ${price:,.4f}")
    else:
        _log(f"  ERROR: Could not fetch price for {symbol}")
    _log("")

    # Order book
    _log("── Order Book ──")
    book = get_orderbook(symbol, depth=10)
    if book:
        _log(f"  Spread: {book['spread_bps']} bps")
        _log(f"  Bid depth: ${book['bid_depth_usd']:,.0f}")
        _log(f"  Ask depth: ${book['ask_depth_usd']:,.0f}")
        _log(f"  Top 5 bids:")
        for b in book["bids"][:5]:
            _log(f"    ${float(b[0]):>12,.4f}  qty: {float(b[1]):>12,.6f}")
        _log(f"  Top 5 asks:")
        for a in book["asks"][:5]:
            _log(f"    ${float(a[0]):>12,.4f}  qty: {float(a[1]):>12,.6f}")
    else:
        _log("  ERROR: Could not fetch order book")
    _log("")

    # Klines (last 5 candles, 1h)
    _log("── Recent Candles (1h) ──")
    klines = get_klines(symbol, "60m", 5)
    if klines:
        for k in klines:
            ts = datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc).strftime("%H:%M")
            _log(f"  {ts}  O:{k['open']:>10,.2f}  H:{k['high']:>10,.2f}  "
                 f"L:{k['low']:>10,.2f}  C:{k['close']:>10,.2f}  V:{k['volume']:>12,.2f}")
    else:
        _log("  ERROR: Could not fetch klines")
    _log("")

    # Account balance
    _log("── Account Balance ──")
    balances = get_account_balance()
    if balances is not None:
        if not balances:
            _log("  (no non-zero balances)")
        for asset, amounts in sorted(balances.items()):
            _log(f"  {asset}: free={amounts['free']:,.8f}  locked={amounts['locked']:,.8f}")
    else:
        _log("  ERROR: Could not fetch account balance")
    _log("")

    # Health
    _log("── Health Check ──")
    health = health_check()
    _log(f"  Public API: {health['public_api']}")
    _log(f"  Auth API: {health['authenticated_api']}")
    _log(f"  Circuit breaker: {'TRIPPED' if health['circuit_breaker_tripped'] else 'OK'}")
    _log(f"  Requests/Errors: {health['total_requests']}/{health['total_errors']}")

    _log("")
    _log("Report complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    try:
        run_report(symbol.upper())
    except Exception as e:
        _log(f"FATAL: {e}")
        sys.exit(1)
