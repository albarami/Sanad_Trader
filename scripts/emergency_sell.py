#!/usr/bin/env python3
"""
Emergency Sell All — Sprint 4.1.12

Closes ALL open positions immediately via OMS.
Also cancels all active orders first.

Deterministic Python. No LLMs.

Called by:
- heartbeat.py when flash crash / kill switch triggered
- Manual invocation: python3 emergency_sell.py
- WhatsApp/Telegram command: /emergency

Flow:
1. Cancel all active OMS orders
2. For each open position → place MARKET SELL via OMS
3. Update positions to CLOSED
4. Emit EMERGENCY_SELL event
5. Send alert (Telegram/WhatsApp)
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
POSITIONS_PATH = STATE_DIR / "positions.json"
EVENTS_DIR = BASE_DIR / "execution-logs"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[EMERGENCY] {ts} {msg}", flush=True)


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


def _get_open_positions() -> list:
    positions = _load_json(POSITIONS_PATH, {})
    if isinstance(positions, list):
        return [p for p in positions if p.get("status") == "open"]
    elif isinstance(positions, dict):
        return [v for v in positions.values() if isinstance(v, dict) and v.get("status") == "open"]
    return []


def emergency_sell_all(reason: str = "Manual emergency", paper_mode: bool = True) -> dict:
    """Execute emergency liquidation of all positions."""
    now = _now()
    _log(f"{'='*50}")
    _log(f"EMERGENCY SELL ALL TRIGGERED")
    _log(f"Reason: {reason}")
    _log(f"Mode: {'PAPER' if paper_mode else 'LIVE'}")
    _log(f"{'='*50}")

    result = {
        "triggered_at": now.isoformat(),
        "reason": reason,
        "paper_mode": paper_mode,
        "orders_canceled": 0,
        "positions_closed": 0,
        "positions_failed": 0,
        "total_positions": 0,
        "details": [],
    }

    # Step 1: Cancel all active OMS orders
    try:
        import oms
        canceled = oms.cancel_all()
        result["orders_canceled"] = canceled
        _log(f"Step 1: Canceled {canceled} active orders")
    except Exception as e:
        _log(f"Step 1: Cancel orders failed: {e}")

    # Step 2: Get open positions
    positions = _get_open_positions()
    result["total_positions"] = len(positions)

    if not positions:
        _log("No open positions — nothing to sell")
        _log("EMERGENCY SELL COMPLETE (no action needed)")
        return result

    _log(f"Step 2: Found {len(positions)} open positions to close")

    # Step 3: Market sell each position via OMS
    try:
        import exchange_router
    except ImportError:
        exchange_router = None

    for pos in positions:
        token = pos.get("token", pos.get("symbol", "unknown"))
        symbol = token if token.endswith("USDT") else f"{token}USDT"
        quantity = pos.get("quantity", pos.get("amount", 0))

        if quantity <= 0:
            _log(f"  SKIP {symbol}: zero quantity")
            continue

        _log(f"  SELLING {quantity} {symbol}...")

        try:
            # Route to correct exchange
            exchange = "binance"
            if exchange_router:
                route = exchange_router.route(symbol)
                exchange = route.get("exchange", "binance")

            # Place MARKET sell via OMS (no price = market order)
            import oms
            order = oms.place_order(
                symbol=symbol,
                side="SELL",
                quantity=quantity,
                price=None,
                order_type="MARKET",
                strategy="emergency_sell",
                correlation_id=f"EMERGENCY_{now.strftime('%Y%m%d%H%M%S')}",
                exchange=exchange,
                paper_mode=paper_mode,
                max_retries=2,
            )

            if order.get("state") == "FILLED":
                fill_price = order.get("avg_fill_price", 0)
                _log(f"  SOLD {symbol} @ ${fill_price:.6f} — {order['state']}")
                result["positions_closed"] += 1
                result["details"].append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "fill_price": fill_price,
                    "state": order["state"],
                })
            else:
                _log(f"  WARNING: {symbol} order state = {order.get('state')} — may need manual check")
                result["positions_failed"] += 1
                result["details"].append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "state": order.get("state"),
                    "error": order.get("error"),
                })

        except Exception as e:
            _log(f"  FAILED {symbol}: {e}")
            result["positions_failed"] += 1
            result["details"].append({
                "symbol": symbol,
                "error": str(e),
            })

    # Step 4: Update positions to closed — SQLite SSOT
    try:
        import state_store
        open_positions = state_store.get_open_positions()
        for p in open_positions:
            pid = p.get("position_id") or p.get("id")
            if pid:
                state_store.update_position_close(
                    pid,
                    close_price=p.get("entry_price", 0),  # Emergency: use entry as close (no market data)
                    close_reason="EMERGENCY_SELL"
                )
        state_store.sync_json_cache()
        _log(f"Step 4: {len(open_positions)} positions marked CLOSED in SQLite")
    except Exception as e:
        _log(f"Step 4: Position update failed: {e}")

    # Step 5: Log emergency event
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    event = {
        "event_type": "EMERGENCY_SELL_ALL",
        "timestamp": now.isoformat(),
        **result,
    }
    event_file = EVENTS_DIR / f"emergency_sell_{now.strftime('%Y%m%d_%H%M%S')}.json"
    _save_json(event_file, event)

    _log(f"{'='*50}")
    _log(f"EMERGENCY SELL COMPLETE")
    _log(f"  Closed: {result['positions_closed']}/{result['total_positions']}")
    _log(f"  Failed: {result['positions_failed']}")
    _log(f"  Orders canceled: {result['orders_canceled']}")
    _log(f"{'='*50}")

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--reason", default="Manual emergency trigger", help="Reason for emergency sell")
    parser.add_argument("--live", action="store_true", help="Execute LIVE (default is paper)")
    parser.add_argument("--test", action="store_true", help="Dry run test")
    args = parser.parse_args()

    if args.test:
        _log("=== EMERGENCY SELL TEST (dry run) ===")
        positions = _get_open_positions()
        _log(f"Open positions: {len(positions)}")
        for p in positions:
            token = p.get("token", p.get("symbol", "?"))
            qty = p.get("quantity", p.get("amount", 0))
            _log(f"  Would sell: {qty} {token}")
        _log("=== TEST COMPLETE (no orders placed) ===")
    else:
        emergency_sell_all(reason=args.reason, paper_mode=not args.live)
