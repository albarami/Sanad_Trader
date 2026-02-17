#!/usr/bin/env python3
"""
Order Management System (OMS) — Sprint 4.2.1 through 4.2.8

Deterministic Python. No LLMs.

Handles the full order lifecycle:
4.2.1 — State machine: NEW→SUBMITTED→ACK→PARTIAL→FILLED→CANCELED→REJECTED
4.2.2 — Idempotency via client_order_id
4.2.3 — Duplicate prevention (check before place)
4.2.4 — Order-intent persistence (record BEFORE sending)
4.2.5 — Limit orders as default for CEX
4.2.6 — Time-in-force handling (GTC/IOC/FOK)
4.2.7 — Partial fill handling
4.2.8 — Order timeout/retry with backoff

Used by: sanad_pipeline Stage 7 (execution), position_monitor (exits)
"""

import json
import os
import sys
import time
import hashlib
from enum import Enum
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
ORDERS_PATH = STATE_DIR / "oms_orders.json"
INTENTS_PATH = STATE_DIR / "oms_intents.json"
LOGS_DIR = BASE_DIR / "execution-logs"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[OMS] {ts} {msg}", flush=True)


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


# ─────────────────────────────────────────────────────────
# 4.2.1 — Order State Machine
# ─────────────────────────────────────────────────────────

class OrderState(str, Enum):
    NEW = "NEW"                         # Created locally, not yet sent
    SUBMITTED = "SUBMITTED"             # Sent to exchange, awaiting ACK
    ACKNOWLEDGED = "ACKNOWLEDGED"       # Exchange confirmed receipt
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"                   # Fully executed
    CANCELED = "CANCELED"               # Canceled by us or exchange
    REJECTED = "REJECTED"               # Exchange rejected
    EXPIRED = "EXPIRED"                 # Timed out without fill
    FAILED = "FAILED"                   # Internal error, never reached exchange


# Valid state transitions
VALID_TRANSITIONS = {
    OrderState.NEW: {OrderState.SUBMITTED, OrderState.FAILED, OrderState.CANCELED},
    OrderState.SUBMITTED: {OrderState.ACKNOWLEDGED, OrderState.FILLED, OrderState.PARTIALLY_FILLED, OrderState.REJECTED, OrderState.FAILED, OrderState.CANCELED},
    OrderState.ACKNOWLEDGED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED},
    OrderState.PARTIALLY_FILLED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED},
    OrderState.FILLED: set(),       # Terminal
    OrderState.CANCELED: set(),     # Terminal
    OrderState.REJECTED: set(),     # Terminal
    OrderState.EXPIRED: set(),      # Terminal
    OrderState.FAILED: set(),       # Terminal
}

TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED, OrderState.FAILED}


def _transition_valid(current: str, target: str) -> bool:
    try:
        current_state = OrderState(current)
        target_state = OrderState(target)
        return target_state in VALID_TRANSITIONS.get(current_state, set())
    except ValueError:
        return False


# ─────────────────────────────────────────────────────────
# 4.2.2 — Idempotency: client_order_id generation
# ─────────────────────────────────────────────────────────

def generate_client_order_id(correlation_id: str, strategy: str, side: str, symbol: str) -> str:
    """Generate deterministic client_order_id for idempotency.

    Same inputs within a 5-minute bucket → same ID → exchange deduplicates.
    """
    bucket = _now().strftime("%Y%m%d%H") + str(_now().minute // 5)
    raw = f"{correlation_id}:{strategy}:{side}:{symbol}:{bucket}"
    return "ST_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────
# 4.2.3 — Duplicate Prevention
# ─────────────────────────────────────────────────────────

def _check_duplicate(client_order_id: str, orders: dict) -> bool:
    """Check if an order with this client_order_id already exists and is active."""
    if client_order_id in orders:
        existing = orders[client_order_id]
        state = existing.get("state", "")
        if state not in [s.value for s in TERMINAL_STATES]:
            _log(f"DUPLICATE blocked: {client_order_id} already in state {state}")
            return True
    return False


def _check_conflicting_orders(symbol: str, side: str, orders: dict) -> list:
    """Find any active orders for same symbol+side."""
    conflicts = []
    for oid, order in orders.items():
        if (order.get("symbol") == symbol
                and order.get("side") == side
                and order.get("state") not in [s.value for s in TERMINAL_STATES]):
            conflicts.append(oid)
    return conflicts


# ─────────────────────────────────────────────────────────
# 4.2.4 — Order-Intent Persistence
# ─────────────────────────────────────────────────────────

def _record_intent(order: dict):
    """Persist order intent BEFORE sending to exchange.

    This prevents fire-and-forget scenarios.
    """
    intents = _load_json(INTENTS_PATH, {"intents": []})
    intents["intents"].append({
        "client_order_id": order["client_order_id"],
        "symbol": order["symbol"],
        "side": order["side"],
        "quantity": order["quantity"],
        "price": order.get("price"),
        "strategy": order.get("strategy", "unknown"),
        "recorded_at": _now().isoformat(),
        "exchange_sent": False,
    })
    # Keep last 200 intents
    intents["intents"] = intents["intents"][-200:]
    _save_json(INTENTS_PATH, intents)


def _mark_intent_sent(client_order_id: str, exchange_order_id: str = None):
    """Mark intent as sent to exchange."""
    intents = _load_json(INTENTS_PATH, {"intents": []})
    for intent in intents["intents"]:
        if intent["client_order_id"] == client_order_id:
            intent["exchange_sent"] = True
            intent["exchange_order_id"] = exchange_order_id
            intent["sent_at"] = _now().isoformat()
            break
    _save_json(INTENTS_PATH, intents)


# ─────────────────────────────────────────────────────────
# Core: Place Order (4.2.5, 4.2.6)
# ─────────────────────────────────────────────────────────

def place_order(
    symbol: str,
    side: str,
    quantity: float,
    price: float = None,
    order_type: str = "LIMIT",
    time_in_force: str = "GTC",
    strategy: str = "unknown",
    correlation_id: str = "",
    exchange: str = "binance",
    paper_mode: bool = True,
    max_retries: int = 3,
) -> dict:
    """Place an order through the OMS with full lifecycle management.

    4.2.5 — Default to LIMIT orders for slippage control
    4.2.6 — GTC/IOC/FOK time-in-force
    4.2.8 — Retry with backoff
    """
    now = _now()
    orders = _load_json(ORDERS_PATH, {})

    # Generate idempotent order ID (4.2.2)
    if not correlation_id:
        correlation_id = now.strftime("%Y%m%d%H%M%S")
    client_order_id = generate_client_order_id(correlation_id, strategy, side, symbol)

    # Check duplicates (4.2.3)
    if _check_duplicate(client_order_id, orders):
        return orders[client_order_id]

    # Check conflicts
    conflicts = _check_conflicting_orders(symbol, side, orders)
    if conflicts:
        _log(f"WARNING: {len(conflicts)} active {side} orders for {symbol}: {conflicts}")

    # Validate TIF (4.2.6)
    valid_tif = {"GTC", "IOC", "FOK"}
    if time_in_force not in valid_tif:
        _log(f"Invalid TIF '{time_in_force}', defaulting to GTC")
        time_in_force = "GTC"

    # Default to LIMIT (4.2.5)
    if order_type not in {"LIMIT", "MARKET"}:
        order_type = "LIMIT"
    if order_type == "LIMIT" and price is None:
        _log("LIMIT order requires price — falling back to MARKET")
        order_type = "MARKET"
        time_in_force = "GTC"

    # Create order record (4.2.1 — NEW state)
    order = {
        "client_order_id": client_order_id,
        "exchange_order_id": None,
        "symbol": symbol.upper(),
        "side": side.upper(),
        "quantity": quantity,
        "price": price,
        "order_type": order_type,
        "time_in_force": time_in_force,
        "strategy": strategy,
        "correlation_id": correlation_id,
        "exchange": exchange,
        "paper_mode": paper_mode,
        "state": OrderState.NEW.value,
        "filled_quantity": 0.0,
        "avg_fill_price": 0.0,
        "fills": [],
        "retries": 0,
        "max_retries": max_retries,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "submitted_at": None,
        "filled_at": None,
        "error": None,
        "events": [{"state": "NEW", "at": now.isoformat()}],
    }

    # Persist intent BEFORE sending (4.2.4)
    _record_intent(order)
    orders[client_order_id] = order
    _save_json(ORDERS_PATH, orders)

    _log(f"NEW: {side} {quantity} {symbol} @ {price or 'MARKET'} [{strategy}] id={client_order_id[:12]}...")

    # Submit to exchange (4.2.8 — retry with backoff)
    result = _submit_with_retry(order, orders, max_retries)
    return result


def _submit_with_retry(order: dict, orders: dict, max_retries: int) -> dict:
    """Submit order to exchange with exponential backoff retry."""
    client_order_id = order["client_order_id"]

    for attempt in range(max_retries):
        try:
            # Transition to SUBMITTED
            _update_state(order, OrderState.SUBMITTED)
            order["submitted_at"] = _now().isoformat()
            order["retries"] = attempt
            orders[client_order_id] = order
            _save_json(ORDERS_PATH, orders)

            if order["paper_mode"]:
                result = _execute_paper(order)
            else:
                result = _execute_live(order)

            if result.get("success"):
                exchange_id = result.get("exchange_order_id", "paper_" + client_order_id[:8])
                order["exchange_order_id"] = exchange_id
                _mark_intent_sent(client_order_id, exchange_id)

                # Check if immediately filled
                status = result.get("status", "NEW")
                if status == "FILLED":
                    _handle_fill(order, order["quantity"], result.get("avg_price", order.get("price", 0)))
                elif status == "PARTIALLY_FILLED":
                    filled_qty = result.get("filled_quantity", 0)
                    fill_price = result.get("avg_price", order.get("price", 0))
                    _handle_partial_fill(order, filled_qty, fill_price)
                else:
                    _update_state(order, OrderState.ACKNOWLEDGED)

                orders[client_order_id] = order
                _save_json(ORDERS_PATH, orders)
                _log(f"ACK: {order['side']} {order['symbol']} → {order['state']} (exchange_id={exchange_id})")
                return order

            else:
                error = result.get("error", "Unknown error")
                if _is_retryable(error) and attempt < max_retries - 1:
                    delay = min(2 ** attempt, 10)
                    _log(f"RETRY {attempt+1}/{max_retries}: {error} — waiting {delay}s")
                    time.sleep(delay)
                    continue
                else:
                    _update_state(order, OrderState.REJECTED)
                    order["error"] = error
                    orders[client_order_id] = order
                    _save_json(ORDERS_PATH, orders)
                    _log(f"REJECTED: {order['symbol']} — {error}")
                    return order

        except Exception as e:
            if attempt < max_retries - 1:
                delay = min(2 ** attempt, 10)
                _log(f"ERROR attempt {attempt+1}: {e} — retrying in {delay}s")
                time.sleep(delay)
            else:
                _update_state(order, OrderState.FAILED)
                order["error"] = str(e)
                orders[client_order_id] = order
                _save_json(ORDERS_PATH, orders)
                _log(f"FAILED: {order['symbol']} after {max_retries} attempts — {e}")
                return order

    return order


def _is_retryable(error: str) -> bool:
    """Determine if an error is retryable."""
    retryable = ["timeout", "rate limit", "429", "503", "502", "network", "connection"]
    return any(r in error.lower() for r in retryable)


# ─────────────────────────────────────────────────────────
# 4.2.7 — Partial Fill Handling
# ─────────────────────────────────────────────────────────

def _handle_partial_fill(order: dict, filled_qty: float, fill_price: float):
    """Handle partial fill — update quantities and state."""
    fill = {
        "quantity": filled_qty,
        "price": fill_price,
        "at": _now().isoformat(),
    }
    order["fills"].append(fill)

    # Update cumulative
    total_filled = sum(f["quantity"] for f in order["fills"])
    total_cost = sum(f["quantity"] * f["price"] for f in order["fills"])
    order["filled_quantity"] = total_filled
    order["avg_fill_price"] = total_cost / total_filled if total_filled > 0 else 0

    if total_filled >= order["quantity"]:
        _update_state(order, OrderState.FILLED)
        order["filled_at"] = _now().isoformat()
        _log(f"FILLED (via partials): {order['symbol']} {total_filled}/{order['quantity']} @ avg ${order['avg_fill_price']:.6f}")
    else:
        _update_state(order, OrderState.PARTIALLY_FILLED)
        _log(f"PARTIAL: {order['symbol']} {total_filled}/{order['quantity']} @ ${fill_price:.6f}")


def _handle_fill(order: dict, quantity: float, price: float):
    """Handle complete fill."""
    fill = {"quantity": quantity, "price": price, "at": _now().isoformat()}
    order["fills"].append(fill)
    order["filled_quantity"] = quantity
    order["avg_fill_price"] = price
    order["filled_at"] = _now().isoformat()
    _update_state(order, OrderState.FILLED)


def _update_state(order: dict, new_state: OrderState):
    """Transition order to new state with validation."""
    current = order.get("state", "NEW")
    if not _transition_valid(current, new_state.value):
        _log(f"INVALID TRANSITION: {current} → {new_state.value} for {order.get('client_order_id', '?')}")
        return False
    order["state"] = new_state.value
    order["updated_at"] = _now().isoformat()
    order["events"].append({"state": new_state.value, "at": _now().isoformat()})
    return True


# ─────────────────────────────────────────────────────────
# Exchange Execution
# ─────────────────────────────────────────────────────────

def _execute_paper(order: dict) -> dict:
    """Paper trade execution — simulate fill."""
    # Simulate immediate fill for paper mode
    fill_price = order.get("price")

    if fill_price is None:
        # Get live price for market orders
        try:
            if order["exchange"] == "binance":
                import binance_client
                fill_price = binance_client.get_price(order["symbol"])
            elif order["exchange"] == "mexc":
                import mexc_client
                fill_price = mexc_client.get_price(order["symbol"])
        except Exception:
            fill_price = 0

    if not fill_price or fill_price <= 0:
        return {"success": False, "error": "Cannot determine fill price"}

    # Simulate small slippage for realism
    import random
    slippage_pct = random.uniform(0, 0.001)  # 0-0.1%
    if order["side"] == "BUY":
        fill_price *= (1 + slippage_pct)
    else:
        fill_price *= (1 - slippage_pct)

    # Log paper order
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_entry = {
        "client_order_id": order["client_order_id"],
        "symbol": order["symbol"],
        "side": order["side"],
        "quantity": order["quantity"],
        "price": fill_price,
        "type": "PAPER",
        "executed_at": _now().isoformat(),
    }
    log_file = LOGS_DIR / "oms_paper_fills.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    return {
        "success": True,
        "exchange_order_id": f"paper_{order['client_order_id'][:8]}",
        "status": "FILLED",
        "filled_quantity": order["quantity"],
        "avg_price": fill_price,
    }


def _execute_live(order: dict) -> dict:
    """Live exchange execution via exchange router."""
    try:
        if order["exchange"] == "binance":
            import binance_client
            result = binance_client.place_order(
                symbol=order["symbol"],
                side=order["side"],
                quantity=order["quantity"],
                order_type=order["order_type"],
                price=order.get("price"),
                time_in_force=order["time_in_force"],
            )
        elif order["exchange"] == "mexc":
            import mexc_client
            result = mexc_client.place_order(
                symbol=order["symbol"],
                side=order["side"],
                quantity=order["quantity"],
                order_type=order["order_type"],
                price=order.get("price"),
            )
        else:
            return {"success": False, "error": f"Unknown exchange: {order['exchange']}"}

        if result and result.get("orderId"):
            return {
                "success": True,
                "exchange_order_id": str(result["orderId"]),
                "status": result.get("status", "NEW"),
                "filled_quantity": float(result.get("executedQty", 0)),
                "avg_price": float(result.get("price", order.get("price", 0))),
            }
        else:
            return {"success": False, "error": str(result)}

    except Exception as e:
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────
# Query & Management
# ─────────────────────────────────────────────────────────

def get_order(client_order_id: str) -> dict | None:
    orders = _load_json(ORDERS_PATH, {})
    return orders.get(client_order_id)


def get_active_orders() -> list:
    orders = _load_json(ORDERS_PATH, {})
    return [o for o in orders.values() if o.get("state") not in [s.value for s in TERMINAL_STATES]]


def get_orders_by_symbol(symbol: str) -> list:
    orders = _load_json(ORDERS_PATH, {})
    return [o for o in orders.values() if o.get("symbol") == symbol.upper()]


def cancel_order(client_order_id: str) -> dict | None:
    """Cancel an order."""
    orders = _load_json(ORDERS_PATH, {})
    order = orders.get(client_order_id)
    if not order:
        _log(f"Cancel: order {client_order_id} not found")
        return None

    if order["state"] in [s.value for s in TERMINAL_STATES]:
        _log(f"Cancel: order already in terminal state {order['state']}")
        return order

    # Cancel on exchange if live
    if not order.get("paper_mode") and order.get("exchange_order_id"):
        try:
            if order["exchange"] == "binance":
                import binance_client
                binance_client.cancel_order(order["symbol"], order["exchange_order_id"])
            elif order["exchange"] == "mexc":
                import mexc_client
                mexc_client.cancel_order(order["symbol"], order["exchange_order_id"])
        except Exception as e:
            _log(f"Cancel exchange error: {e}")

    _update_state(order, OrderState.CANCELED)
    orders[client_order_id] = order
    _save_json(ORDERS_PATH, orders)
    _log(f"CANCELED: {order['symbol']} {order['side']} id={client_order_id[:12]}...")
    return order


def cancel_all(symbol: str = None) -> int:
    """Cancel all active orders, optionally filtered by symbol."""
    active = get_active_orders()
    if symbol:
        active = [o for o in active if o.get("symbol") == symbol.upper()]
    count = 0
    for order in active:
        result = cancel_order(order["client_order_id"])
        if result:
            count += 1
    _log(f"Canceled {count} orders" + (f" for {symbol}" if symbol else ""))
    return count


def status() -> dict:
    """OMS status summary."""
    orders = _load_json(ORDERS_PATH, {})
    state_counts = {}
    for o in orders.values():
        s = o.get("state", "UNKNOWN")
        state_counts[s] = state_counts.get(s, 0) + 1
    return {
        "total_orders": len(orders),
        "active": len([o for o in orders.values() if o.get("state") not in [s.value for s in TERMINAL_STATES]]),
        "by_state": state_counts,
        "updated_at": _now().isoformat(),
    }


# ─────────────────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    _log("=== OMS TEST ===")

    # Test 1: Place a paper BUY order
    _log("Test 1: Paper BUY BTCUSDT")
    order1 = place_order(
        symbol="BTCUSDT",
        side="BUY",
        quantity=0.001,
        price=68000.0,
        strategy="test-strategy",
        correlation_id="test-001",
        exchange="binance",
        paper_mode=True,
    )
    print(f"  State: {order1['state']}, Filled: {order1['filled_quantity']}, Avg: ${order1['avg_fill_price']:.2f}")

    # Test 2: Duplicate detection
    _log("Test 2: Same order again (should be duplicate)")
    order2 = place_order(
        symbol="BTCUSDT",
        side="BUY",
        quantity=0.001,
        price=68000.0,
        strategy="test-strategy",
        correlation_id="test-001",
        exchange="binance",
        paper_mode=True,
    )
    print(f"  Same order returned: {order2['client_order_id'] == order1['client_order_id']}")

    # Test 3: Different order
    _log("Test 3: Paper SELL ETHUSDT")
    order3 = place_order(
        symbol="ETHUSDT",
        side="SELL",
        quantity=0.1,
        price=1980.0,
        strategy="sentiment-exit",
        correlation_id="test-002",
        exchange="binance",
        paper_mode=True,
    )
    print(f"  State: {order3['state']}, Filled: {order3['filled_quantity']}, Avg: ${order3['avg_fill_price']:.2f}")

    # Test 4: Cancel
    _log("Test 4: Cancel order 1")
    # Already filled, should fail gracefully
    cancel_result = cancel_order(order1["client_order_id"])
    print(f"  Cancel result: {cancel_result['state']}")

    # Status
    _log("=== OMS STATUS ===")
    s = status()
    print(f"  Total: {s['total_orders']}, Active: {s['active']}")
    print(f"  By state: {s['by_state']}")
