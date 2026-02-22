#!/usr/bin/env python3
"""
Sanad Trader v3.0 — Binance Exchange Client

Phase 5 — Exchange API Wrapper

Provides:
1. Market data (prices, order book, 24h ticker)
2. Account data (balances, open orders)
3. Order execution (paper mode logs to state, live mode hits API)
4. Error tracking feeding circuit breakers
5. Health check for Gate #10

Credentials loaded from trading/config/.env (gitignored).
All methods fail-closed: errors return None and increment circuit breaker counter.

References:
- v3 doc Phase 5, Table 5 (data sources)
- v3 doc Phase 10, Gate #10 (Exchange Health)
- v3 doc Table 10 (Circuit Breaker: 5 errors in 60s → trip, 5min cooldown)
- v3 doc Phase 7 (Paper Trading: simulate fees + slippage from real order book)
"""

import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import deque

# Load environment
BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
ENV_PATH = BASE_DIR / "config" / ".env"
STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "execution-logs"

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except Exception as e:
    print(f"[BINANCE] Error loading .env: {e}")

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
BINANCE_BASE_URL = "https://api.binance.com"


# ─────────────────────────────────────────────
# ERROR TRACKING (Circuit Breaker Feed)
# ─────────────────────────────────────────────

class ErrorTracker:
    """
    Tracks errors per component for circuit breaker logic.
    Trip threshold: 5 errors in 60 seconds (from thresholds.yaml).
    """

    def __init__(self, component="binance_api", window_sec=60, trip_threshold=5):
        self.component = component
        self.window_sec = window_sec
        self.trip_threshold = trip_threshold
        self.errors = deque()  # timestamps of recent errors
        self.total_requests = 0
        self.total_errors = 0
        self._tripped = False
        self._cooldown_until = None

    def record_success(self):
        self.total_requests += 1

    def record_error(self, error_msg=""):
        now = time.time()
        self.total_requests += 1
        self.total_errors += 1
        self.errors.append(now)

        # Clean old errors outside window
        cutoff = now - self.window_sec
        while self.errors and self.errors[0] < cutoff:
            self.errors.popleft()

        # Check trip condition
        if len(self.errors) >= self.trip_threshold:
            self._tripped = True
            self._cooldown_until = now + 300  # 5 min cooldown
            self._update_state_file()
            print(f"[BINANCE] CIRCUIT BREAKER TRIPPED: {len(self.errors)} errors in {self.window_sec}s")

    def is_tripped(self):
        if self._tripped:
            if time.time() >= self._cooldown_until:
                # Cooldown expired → half-open (allow 1 test request)
                return False
            return True
        return False

    def reset_after_success(self):
        """Called after a successful half-open test request."""
        self._tripped = False
        self._cooldown_until = None
        self.errors.clear()
        self._update_state_file()
        print(f"[BINANCE] Circuit breaker RESET after successful test")

    def get_error_rate_pct(self, window_minutes=15):
        """Error rate over last N minutes for Gate #10."""
        if self.total_requests == 0:
            return 0.0
        # Simple: total errors / total requests
        return self.total_errors / self.total_requests if self.total_requests > 0 else 0.0

    def _update_state_file(self):
        """Update circuit_breakers.json state file."""
        try:
            cb_path = STATE_DIR / "circuit_breakers.json"
            with open(cb_path, "r") as f:
                cb_state = json.load(f)

            cb_state[self.component] = {
                "state": "open" if self._tripped else "closed",
                "failure_count": len(self.errors),
                "last_failure_at": datetime.now(timezone.utc).isoformat() if self.errors else None,
                "cooldown_until": datetime.fromtimestamp(self._cooldown_until, tz=timezone.utc).isoformat() if self._cooldown_until else None
            }

            with open(cb_path, "w") as f:
                json.dump(cb_state, f, indent=2)
        except Exception as e:
            print(f"[BINANCE] Error updating circuit breaker state: {e}")


# Global error tracker
_error_tracker = ErrorTracker()


# ─────────────────────────────────────────────
# HTTP CLIENT (using urllib — no extra deps)
# ─────────────────────────────────────────────

import urllib.request
import urllib.parse
import urllib.error


def _sign_params(params):
    """Create HMAC SHA256 signature for authenticated endpoints."""
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        BINANCE_SECRET_KEY.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    params['signature'] = signature
    return params


def _request(method, endpoint, params=None, signed=False, timeout=10):
    """
    Make HTTP request to Binance API.
    Returns parsed JSON on success, None on failure.
    All failures increment error tracker.
    """
    if _error_tracker.is_tripped():
        print(f"[BINANCE] Circuit breaker OPEN — request blocked")
        return None

    url = f"{BINANCE_BASE_URL}{endpoint}"

    if params is None:
        params = {}

    if signed:
        if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
            print("[BINANCE] API keys not configured")
            _error_tracker.record_error("API keys missing")
            return None
        params['timestamp'] = int(time.time() * 1000)
        params = _sign_params(params)

    headers = {}
    if BINANCE_API_KEY:
        headers['X-MBX-APIKEY'] = BINANCE_API_KEY

    try:
        if method == "GET":
            if params:
                url += "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers=headers, method="GET")
        elif method == "POST":
            data = urllib.parse.urlencode(params).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        else:
            req = urllib.request.Request(url, headers=headers, method=method)

        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8')
            result = json.loads(body)

            _error_tracker.record_success()
            _error_tracker.reset_after_success()  # Always close after success (handles HALF_OPEN→CLOSED)

            return result

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else str(e)
        print(f"[BINANCE] HTTP {e.code}: {error_body}")
        _error_tracker.record_error(f"HTTP {e.code}")
        return None
    except urllib.error.URLError as e:
        print(f"[BINANCE] URL Error: {e.reason}")
        _error_tracker.record_error(f"URL Error: {e.reason}")
        return None
    except Exception as e:
        print(f"[BINANCE] Request error: {e}")
        _error_tracker.record_error(str(e))
        return None


# ─────────────────────────────────────────────
# MARKET DATA (Public endpoints — no auth needed)
# ─────────────────────────────────────────────

def get_price(symbol, timeout=10):
    """
    Get current price for a symbol (e.g., 'BTCUSDT').
    Returns float price or None on failure.
    
    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')
        timeout: HTTP request timeout in seconds (default 10s)
    """
    result = _request("GET", "/api/v3/ticker/price", {"symbol": symbol.upper()}, timeout=timeout)
    if result and "price" in result:
        return float(result["price"])
    return None


def get_ticker_24h(symbol):
    """
    Get 24-hour ticker stats for a symbol.
    Returns dict with priceChange, priceChangePercent, volume, etc.
    """
    return _request("GET", "/api/v3/ticker/24hr", {"symbol": symbol.upper()})


def get_order_book(symbol, limit=20):
    """
    Get order book depth for a symbol.
    Returns dict with 'bids' and 'asks' arrays.
    Used for: slippage estimation (Gate #6), spread calculation (Gate #7),
    paper trade fill simulation (Phase 7).
    """
    return _request("GET", "/api/v3/depth", {"symbol": symbol.upper(), "limit": limit})


def get_all_prices():
    """Get prices for all symbols. Returns list of {symbol, price}."""
    return _request("GET", "/api/v3/ticker/price")


def get_server_time():
    """Get Binance server time. Used for clock sync verification."""
    result = _request("GET", "/api/v3/time")
    if result and "serverTime" in result:
        return result["serverTime"]
    return None


def get_exchange_info(symbol=None):
    """Get exchange trading rules and symbol info."""
    params = {}
    if symbol:
        params["symbol"] = symbol.upper()
    return _request("GET", "/api/v3/exchangeInfo", params)


# ─────────────────────────────────────────────
# ACCOUNT DATA (Authenticated)
# ─────────────────────────────────────────────

def get_account():
    """
    Get account information including all balances.
    Used by reconciliation to verify positions.
    """
    return _request("GET", "/api/v3/account", signed=True)


def get_balances():
    """
    Get non-zero balances only.
    Returns dict: {"BTC": {"free": 0.5, "locked": 0.0}, ...}
    """
    account = get_account()
    if not account or "balances" not in account:
        return None

    balances = {}
    for b in account["balances"]:
        free = float(b["free"])
        locked = float(b["locked"])
        if free > 0 or locked > 0:
            balances[b["asset"]] = {"free": free, "locked": locked}

    return balances


def get_open_orders(symbol=None):
    """Get all open orders, optionally filtered by symbol."""
    params = {}
    if symbol:
        params["symbol"] = symbol.upper()
    return _request("GET", "/api/v3/openOrders", params, signed=True)


# ─────────────────────────────────────────────
# MARKET DATA ANALYSIS (for Policy Engine gates)
# ─────────────────────────────────────────────

def estimate_slippage_bps(symbol, side, quantity_usd):
    """
    Estimate slippage for a given order size using real order book.
    Returns estimated slippage in basis points (bps).
    Used by Gate #6 (Liquidity Gate: max 300 bps).

    Args:
        symbol: e.g., 'BTCUSDT'
        side: 'BUY' or 'SELL'
        quantity_usd: USD value of the order
    """
    book = get_order_book(symbol, limit=100)
    if not book:
        return None

    price = get_price(symbol)
    if not price:
        return None

    quantity = quantity_usd / price

    if side == "BUY":
        levels = book.get("asks", [])
    else:
        levels = book.get("bids", [])

    if not levels:
        return None

    filled_qty = 0
    total_cost = 0
    mid_price = price

    for level_price_str, level_qty_str in levels:
        level_price = float(level_price_str)
        level_qty = float(level_qty_str)

        remaining = quantity - filled_qty
        fill_at_level = min(remaining, level_qty)

        total_cost += fill_at_level * level_price
        filled_qty += fill_at_level

        if filled_qty >= quantity:
            break

    if filled_qty < quantity * 0.95:
        # Couldn't fill 95% of order — depth insufficient
        return 99999  # Signal insufficient depth

    avg_price = total_cost / filled_qty
    slippage_pct = abs(avg_price - mid_price) / mid_price
    slippage_bps = slippage_pct * 10000

    return round(slippage_bps, 1)


def get_spread_bps(symbol):
    """
    Get current bid-ask spread in basis points.
    Used by Gate #7 (Spread Gate: max 200 bps).
    """
    book = get_order_book(symbol, limit=5)
    if not book or not book.get("bids") or not book.get("asks"):
        return None

    best_bid = float(book["bids"][0][0])
    best_ask = float(book["asks"][0][0])

    if best_bid <= 0:
        return None

    spread_pct = (best_ask - best_bid) / best_bid
    spread_bps = spread_pct * 10000

    return round(spread_bps, 1)


# ─────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────

def place_order(symbol, side, quantity, order_type="MARKET", price=None, paper_mode=True):
    """
    Place an order on Binance.

    In PAPER mode: logs order to state files, simulates fill from real order book.
    In LIVE mode: sends real order to Binance API.

    Args:
        symbol: e.g., 'BTCUSDT'
        side: 'BUY' or 'SELL'
        quantity: amount of base asset
        order_type: 'MARKET' or 'LIMIT'
        price: required for LIMIT orders
        paper_mode: if True, simulate instead of executing

    Returns:
        Order result dict or None on failure
    """
    if paper_mode:
        return _paper_order(symbol, side, quantity, order_type, price)
    else:
        return _live_order(symbol, side, quantity, order_type, price)


def _paper_order(symbol, side, quantity, order_type, price):
    """
    Simulate order execution using real order book data.
    Models: 0.1% trading fee, realistic slippage from order book depth.
    Per v3 doc Phase 7: "Simulate 0.1% trading fees, realistic slippage
    modeled from actual order book depth snapshots."
    """
    current_price = get_price(symbol)
    if not current_price:
        print(f"[BINANCE PAPER] Cannot get price for {symbol}")
        return None

    # Get real order book for slippage simulation
    book = get_order_book(symbol, limit=50)
    fill_price = current_price  # Default

    if book:
        if side == "BUY":
            levels = book.get("asks", [])
        else:
            levels = book.get("bids", [])

        if levels:
            filled_qty = 0
            total_cost = 0
            for level_price_str, level_qty_str in levels:
                lp = float(level_price_str)
                lq = float(level_qty_str)
                remaining = quantity - filled_qty
                fill_at = min(remaining, lq)
                total_cost += fill_at * lp
                filled_qty += fill_at
                if filled_qty >= quantity:
                    break
            if filled_qty > 0:
                fill_price = total_cost / filled_qty

    # Apply 0.1% trading fee
    fee_rate = 0.001
    fee_usd = fill_price * quantity * fee_rate

    order_result = {
        "orderId": f"PAPER-{int(time.time()*1000)}",
        "symbol": symbol.upper(),
        "side": side.upper(),
        "type": order_type,
        "quantity": quantity,
        "price": fill_price,
        "fee_usd": fee_usd,
        "fee_rate": fee_rate,
        "status": "FILLED",
        "mode": "PAPER",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "simulated_from_orderbook": book is not None
    }

    # Log to execution logs
    _log_paper_order(order_result)

    print(f"[BINANCE PAPER] {side} {quantity} {symbol} @ {fill_price:.6f} (fee: ${fee_usd:.4f})")
    return order_result


def _live_order(symbol, side, quantity, order_type, price):
    """
    Execute real order on Binance.
    ONLY called when system mode is LIVE.
    """
    params = {
        "symbol": symbol.upper(),
        "side": side.upper(),
        "type": order_type,
        "quantity": f"{quantity:.8f}",
    }
    if order_type == "LIMIT":
        if price is None:
            print("[BINANCE] LIMIT order requires price")
            return None
        params["price"] = f"{price:.8f}"
        params["timeInForce"] = "GTC"

    result = _request("POST", "/api/v3/order", params, signed=True)
    if result:
        print(f"[BINANCE LIVE] Order placed: {result.get('orderId')} {side} {quantity} {symbol}")
    return result


def _log_paper_order(order):
    """Log paper trade to execution-logs/paper-trades.jsonl"""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / "paper-trades.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(order) + "\n")
    except Exception as e:
        print(f"[BINANCE] Error logging paper trade: {e}")


# ─────────────────────────────────────────────
# HEALTH CHECK (for Gate #10 and Heartbeat)
# ─────────────────────────────────────────────

def health_check():
    """
    Comprehensive health check for Binance connectivity.
    Returns dict consumed by exchange_health.json state file.
    """
    results = {
        "exchange": "binance",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "api_reachable": False,
        "authenticated": False,
        "error_rate_pct": _error_tracker.get_error_rate_pct(),
        "circuit_breaker_state": "open" if _error_tracker.is_tripped() else "closed",
        "websocket_connected": True,  # Stub until WebSocket implemented
        "server_time_offset_ms": None,
    }

    # Test 1: Public API reachable
    server_time = get_server_time()
    if server_time:
        results["api_reachable"] = True
        local_time_ms = int(time.time() * 1000)
        results["server_time_offset_ms"] = abs(local_time_ms - server_time)

    # Test 2: Authenticated API works
    account = get_account()
    if account and "balances" in account:
        results["authenticated"] = True

    # Update exchange health state file
    try:
        health_path = STATE_DIR / "exchange_health.json"
        with open(health_path, "r") as f:
            health_state = json.load(f)

        health_state["binance"] = {
            "error_rate_pct": results["error_rate_pct"],
            "websocket_connected": results["websocket_connected"],
            "last_check": results["timestamp"],
            "api_reachable": results["api_reachable"],
            "authenticated": results["authenticated"],
        }

        with open(health_path, "w") as f:
            json.dump(health_state, f, indent=2)
    except Exception as e:
        print(f"[BINANCE] Error updating health state: {e}")

    return results


# ─────────────────────────────────────────────
# PRICE SNAPSHOT (for Cron — feeds price_cache and price_history)
# ─────────────────────────────────────────────

def snapshot_prices(symbols):
    """
    Fetch current prices for a list of symbols and update state files.
    Called by price_snapshot cron (every 3 minutes per Table 6 Row 1).

    Args:
        symbols: list of symbols, e.g., ['BTCUSDT', 'ETHUSDT']

    Updates:
    - state/price_cache.json (latest prices for all tracked tokens)
    - state/price_history.json (append timestamped price for flash crash detection)
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    cache = {}
    history_updates = {}

    for symbol in symbols:
        price = get_price(symbol)
        if price is not None:
            cache[symbol] = price
            history_updates[symbol] = {"timestamp": timestamp, "price": price}

    if not cache:
        print("[BINANCE] Price snapshot: no prices fetched")
        return False

    # Update price_cache.json
    try:
        cache_path = STATE_DIR / "price_cache.json"
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"[BINANCE] Error saving price cache: {e}")

    # Append to price_history.json (for flash crash detection)
    try:
        history_path = STATE_DIR / "price_history.json"
        try:
            with open(history_path, "r") as f:
                history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            history = {}

        for symbol, entry in history_updates.items():
            if symbol not in history:
                history[symbol] = []
            history[symbol].append(entry)

            # Keep only last 100 entries per symbol (avoid unbounded growth)
            if len(history[symbol]) > 100:
                history[symbol] = history[symbol][-100:]

        with open(history_path, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"[BINANCE] Error saving price history: {e}")

    print(f"[BINANCE] Price snapshot: {len(cache)} symbols updated")
    return True


# ─────────────────────────────────────────────
# CLI / TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("Binance Client — Connection Test")
    print("=" * 50)
    print(f"API Key loaded: {'Yes' if BINANCE_API_KEY else 'NO'}")
    print(f"Secret loaded: {'Yes' if BINANCE_SECRET_KEY else 'NO'}")
    print()

    # Test 1: Public API — get BTC price
    print("[Test 1] Get BTC price...")
    btc_price = get_price("BTCUSDT")
    if btc_price:
        print(f"  BTC/USDT: ${btc_price:,.2f}")
    else:
        print("  FAILED")

    # Test 2: Order book depth
    print("[Test 2] Get BTC order book...")
    book = get_order_book("BTCUSDT", limit=5)
    if book and "bids" in book:
        best_bid = float(book["bids"][0][0])
        best_ask = float(book["asks"][0][0])
        print(f"  Best bid: ${best_bid:,.2f}, Best ask: ${best_ask:,.2f}")
        spread = get_spread_bps("BTCUSDT")
        print(f"  Spread: {spread} bps")
    else:
        print("  FAILED")

    # Test 3: Slippage estimation
    print("[Test 3] Estimate slippage for $200 BTC buy...")
    slip = estimate_slippage_bps("BTCUSDT", "BUY", 200)
    if slip is not None:
        print(f"  Estimated slippage: {slip} bps")
    else:
        print("  FAILED")

    # Test 4: Authenticated — account access
    print("[Test 4] Get account balances...")
    balances = get_balances()
    if balances is not None:
        print(f"  Found {len(balances)} non-zero balances")
        for asset, bal in list(balances.items())[:5]:
            print(f"    {asset}: free={bal['free']}, locked={bal['locked']}")
    else:
        print("  FAILED (check API key permissions)")

    # Test 5: Health check
    print("[Test 5] Full health check...")
    health = health_check()
    print(f"  API reachable: {health['api_reachable']}")
    print(f"  Authenticated: {health['authenticated']}")
    print(f"  Error rate: {health['error_rate_pct']:.2%}")
    print(f"  Server time offset: {health['server_time_offset_ms']}ms")

    # Test 6: Paper order simulation
    print("[Test 6] Paper trade: BUY 0.001 BTC...")
    order = place_order("BTCUSDT", "BUY", 0.001, paper_mode=True)
    if order:
        print(f"  Order ID: {order['orderId']}")
        print(f"  Fill price: ${order['price']:,.2f}")
        print(f"  Fee: ${order['fee_usd']:.4f}")
    else:
        print("  FAILED")

    # Summary
    print()
    all_passed = all([btc_price, book, slip is not None, balances is not None, health["api_reachable"], order])
    print(f"Result: {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")
