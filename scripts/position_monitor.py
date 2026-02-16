#!/usr/bin/env python3
"""
Sanad Trader v3.0 — Position Monitor

Deterministic Python script. No LLM calls. Runs every 3 minutes via cron.
Checks all open positions against exit conditions and closes when triggered.

Exit conditions (checked in order):
  A. Stop-Loss (HARD) — price drops below entry * (1 - stop_loss_pct)
  B. Take-Profit — price rises above entry * (1 + take_profit_pct)
  C. Trailing Stop — activated at 15% profit, closes on 8% drop from high-water
  D. Time-Based Exit — position open > 48 hours
  E. Volume Death — SKIPPED (no entry volume recorded yet)
  F. Flash Crash Override — price dropped >10% in 15min → close ALL meme positions

Fail-safes:
  - If price_cache.json is empty or stale (>10min), do NOT close. Log warning and exit.
  - If positions.json is unreadable, log error and exit.
  - State writes are atomic (write .tmp then rename).
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path("/data/.openclaw/workspace/trading")
STATE_DIR = BASE_DIR / "state"
CONFIG_DIR = BASE_DIR / "config"
LOGS_DIR = BASE_DIR / "execution-logs"
SCRIPTS_DIR = BASE_DIR / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc)

def now_iso():
    return now_utc().isoformat()

def load_json(path):
    """Load a JSON file. Returns None on failure."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError) as e:
        print(f"[POSITION MONITOR] ERROR loading {path}: {e}")
        return None

def save_json_atomic(path, data):
    """Write JSON atomically: write to .tmp then rename."""
    path = Path(path)
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        print(f"[POSITION MONITOR] ERROR saving {path}: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False

def parse_dt(iso_str):
    """Parse ISO datetime string."""
    return datetime.fromisoformat(iso_str)

def log_to_jsonl(filepath, record):
    """Append a JSON record to a .jsonl file."""
    try:
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"[POSITION MONITOR] ERROR writing {filepath}: {e}")


# ─────────────────────────────────────────────
# TRAILING STOP STATE
# ─────────────────────────────────────────────

TRAILING_STOPS_PATH = STATE_DIR / "trailing_stops.json"

def load_trailing_stops():
    data = load_json(TRAILING_STOPS_PATH)
    return data if data else {}

def save_trailing_stops(data):
    save_json_atomic(TRAILING_STOPS_PATH, data)


# ─────────────────────────────────────────────
# EXIT CONDITION CHECKS
# ─────────────────────────────────────────────

# Trailing stop parameters (from meme-momentum.md)
TRAILING_ACTIVATION_PCT = 0.15   # Activate at 15% profit
TRAILING_DROP_PCT = 0.08         # Close on 8% drop from high-water
MAX_HOLD_HOURS = 48              # Time-based exit
FLASH_CRASH_PCT = 0.10           # 10% drop in 15 minutes
FLASH_CRASH_WINDOW_MIN = 15      # 15-minute window


def check_stop_loss(position, current_price):
    """Exit Condition A: Hard stop-loss."""
    entry = position["entry_price"]
    stop_pct = position.get("stop_loss_pct", 0.15)
    stop_price = entry * (1.0 - stop_pct)

    if current_price <= stop_price:
        return True, "STOP_LOSS", f"Price ${current_price:,.4f} <= stop ${stop_price:,.4f} (-{stop_pct*100:.0f}%)"
    return False, None, None


def check_take_profit(position, current_price):
    """Exit Condition B: Take-profit."""
    entry = position["entry_price"]
    tp_pct = position.get("take_profit_pct", 0.30)
    tp_price = entry * (1.0 + tp_pct)

    if current_price >= tp_price:
        return True, "TAKE_PROFIT", f"Price ${current_price:,.4f} >= target ${tp_price:,.4f} (+{tp_pct*100:.0f}%)"
    return False, None, None


def check_trailing_stop(position, current_price, trailing_stops):
    """Exit Condition C: Trailing stop (activate at 15% profit, 8% drop from high-water)."""
    symbol = position["symbol"]
    entry = position["entry_price"]
    unrealized_pct = (current_price - entry) / entry

    ts_data = trailing_stops.get(symbol, {})

    # Check if trailing stop should activate
    if not ts_data.get("activated", False):
        if unrealized_pct >= TRAILING_ACTIVATION_PCT:
            # Activate trailing stop
            ts_data = {
                "high_water_mark": current_price,
                "activated": True,
                "activated_at": now_iso(),
            }
            trailing_stops[symbol] = ts_data
            print(f"    [TRAILING] Activated for {symbol} at +{unrealized_pct*100:.1f}% | HWM: ${current_price:,.4f}")
        return False, None, None

    # Trailing stop is active — update high-water mark
    hwm = ts_data.get("high_water_mark", current_price)
    if current_price > hwm:
        ts_data["high_water_mark"] = current_price
        hwm = current_price
        trailing_stops[symbol] = ts_data

    # Check if price dropped 8% below high-water mark
    drop_from_hwm = (hwm - current_price) / hwm
    if drop_from_hwm >= TRAILING_DROP_PCT:
        return True, "TRAILING_STOP", (
            f"Price ${current_price:,.4f} dropped {drop_from_hwm*100:.1f}% from HWM ${hwm:,.4f} "
            f"(threshold: {TRAILING_DROP_PCT*100:.0f}%)"
        )

    return False, None, None


def check_time_exit(position):
    """Exit Condition D: Time-based exit (max 48 hours)."""
    opened_at = parse_dt(position["opened_at"])
    hold_hours = (now_utc() - opened_at).total_seconds() / 3600

    if hold_hours > MAX_HOLD_HOURS:
        return True, "TIME_EXIT", f"Position open {hold_hours:.1f}h > {MAX_HOLD_HOURS}h max"
    return False, None, None


def check_volume_death(position, current_price):
    """Exit Condition E: Volume death (SKIPPED — no entry volume recorded)."""
    # TODO: Add entry_volume to position records in sanad_pipeline.py
    return False, None, None


def check_flash_crash(price_history):
    """
    Exit Condition F: Flash crash override (portfolio-wide).
    If ANY watched symbol dropped >10% in 15 minutes, close ALL meme positions.
    Returns list of symbols that triggered.
    """
    triggered = []
    now = now_utc()

    for symbol, entries in price_history.items():
        if not entries or len(entries) < 2:
            continue

        # Find price from ~15 minutes ago
        recent_price = None
        old_price = None
        for entry in reversed(entries):
            ts = parse_dt(entry["timestamp"]) if isinstance(entry.get("timestamp"), str) else None
            price = entry.get("price", 0)

            if ts is None:
                continue

            age_min = (now - ts).total_seconds() / 60

            if age_min <= 1 and recent_price is None:
                recent_price = price
            elif FLASH_CRASH_WINDOW_MIN - 2 <= age_min <= FLASH_CRASH_WINDOW_MIN + 5:
                old_price = price
                break

        if recent_price and old_price and old_price > 0:
            change_pct = (recent_price - old_price) / old_price
            if change_pct <= -FLASH_CRASH_PCT:
                triggered.append({
                    "symbol": symbol,
                    "change_pct": change_pct,
                    "recent_price": recent_price,
                    "old_price": old_price,
                })

    return triggered


# ─────────────────────────────────────────────
# CLOSE POSITION
# ─────────────────────────────────────────────

def close_position(position, current_price, reason, detail=""):
    """Close a position and update all state."""
    now = now_utc()
    entry = position["entry_price"]
    qty = position["quantity"]

    pnl_pct = (current_price - entry) / entry
    pnl_usd = (current_price - entry) * qty
    fee_usd = current_price * qty * 0.001  # 0.1% paper fee
    net_pnl_usd = pnl_usd - fee_usd
    hold_hours = (now - parse_dt(position["opened_at"])).total_seconds() / 3600

    # Update position record
    position["status"] = "CLOSED"
    position["current_price"] = current_price
    position["exit_price"] = current_price
    position["exit_reason"] = reason
    position["pnl_pct"] = round(pnl_pct, 6)
    position["pnl_usd"] = round(net_pnl_usd, 4)
    position["fee_usd"] = round(fee_usd, 4)
    position["hold_hours"] = round(hold_hours, 2)
    position["closed_at"] = now.isoformat()

    pnl_sign = "+" if net_pnl_usd >= 0 else ""
    print(f"  [CLOSE] {position['token']} — {reason} @ ${current_price:,.4f} | "
          f"P&L: {pnl_sign}{pnl_pct*100:.1f}% ({pnl_sign}${net_pnl_usd:.2f}) | "
          f"Hold: {hold_hours:.1f}h")
    if detail:
        print(f"    Reason: {detail}")

    # Log paper trade (SELL side)
    paper_trade = {
        "orderId": f"PAPER-CLOSE-{int(now.timestamp()*1000)}",
        "symbol": position["symbol"],
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "price": current_price,
        "fee_usd": fee_usd,
        "fee_rate": 0.001,
        "status": "FILLED",
        "mode": "PAPER",
        "timestamp": now.isoformat(),
        "exit_reason": reason,
        "pnl_usd": net_pnl_usd,
        "pnl_pct": pnl_pct,
    }
    log_to_jsonl(LOGS_DIR / "paper-trades.jsonl", paper_trade)

    # Log decision
    decision = {
        "correlation_id": position.get("id", "unknown"),
        "timestamp": now.isoformat(),
        "signal": {
            "token": position["token"],
            "source": "position_monitor",
            "thesis": f"Exit: {reason}",
        },
        "final_action": "CLOSE",
        "exit_reason": reason,
        "exit_detail": detail,
        "execution": {
            "order_id": paper_trade["orderId"],
            "side": "SELL",
            "fill_price": current_price,
            "quantity": qty,
            "pnl_pct": pnl_pct,
            "pnl_usd": net_pnl_usd,
            "fee_usd": fee_usd,
            "hold_hours": hold_hours,
        },
    }
    log_to_jsonl(LOGS_DIR / "decisions.jsonl", decision)

    # Log to Supabase
    try:
        import supabase_client
        supabase_client.log_event("TRADE_CLOSED", {
            "token": position["token"],
            "symbol": position["symbol"],
            "entry_price": entry,
            "exit_price": current_price,
            "pnl_pct": pnl_pct,
            "pnl_usd": net_pnl_usd,
            "exit_reason": reason,
            "hold_hours": hold_hours,
            "strategy": position["strategy_name"],
            "sanad_score": position["sanad_score"],
        }, correlation_id=position.get("id"))
        print(f"    Supabase: TRADE_CLOSED logged")
    except Exception as e:
        print(f"    WARNING: Supabase log failed: {e}")

    # Add to trade_history.json (for Gate #13 cooldown)
    try:
        th_path = STATE_DIR / "trade_history.json"
        trade_history = load_json(th_path) or {"trades": []}
        trades = trade_history.get("trades", [])
        trades.append({
            "token": position["token"],
            "symbol": position["symbol"],
            "timestamp": now.isoformat(),
            "side": "SELL",
            "reason": reason,
            "entry_price": entry,
            "exit_price": current_price,
            "pnl_pct": pnl_pct,
            "pnl_usd": net_pnl_usd,
        })
        trade_history["trades"] = trades
        save_json_atomic(th_path, trade_history)
    except Exception as e:
        print(f"    WARNING: trade_history update failed: {e}")

    # ── Post-trade analysis (Genius Memory) ──
    try:
        import post_trade_analyzer
        post_trade_analyzer.analyze_trade(position)
        print(f"    Genius Memory: post-trade analysis complete")
    except Exception as e:
        print(f"    WARNING: Post-trade analysis failed: {e}")

    return net_pnl_usd


def update_portfolio(positions_data, closed_pnls):
    """Recalculate portfolio after closes."""
    portfolio = load_json(STATE_DIR / "portfolio.json")
    if not portfolio:
        print("[POSITION MONITOR] ERROR: Cannot load portfolio.json for update")
        return

    # Sum all closed trade P&L
    all_positions = positions_data.get("positions", [])
    total_closed_pnl = sum(
        p.get("pnl_usd", 0) for p in all_positions if p["status"] == "CLOSED"
    )

    open_positions = [p for p in all_positions if p["status"] == "OPEN"]

    starting = portfolio.get("starting_balance_usd", 10000.0)
    current = starting + total_closed_pnl
    peak = max(portfolio.get("peak_balance_usd", starting), current)

    # Recalculate exposure
    meme_usd = sum(
        p.get("position_usd", 0) for p in open_positions
        if p.get("strategy_name", "") in ("meme-momentum", "early-launch")
    )
    total_exposure_usd = sum(p.get("position_usd", 0) for p in open_positions)

    token_exposure = {}
    for p in open_positions:
        tok = p.get("token", "")
        token_exposure[tok] = token_exposure.get(tok, 0) + p.get("position_usd", 0) / current if current > 0 else 0

    portfolio["current_balance_usd"] = round(current, 2)
    portfolio["peak_balance_usd"] = round(peak, 2)
    portfolio["open_position_count"] = len(open_positions)
    portfolio["daily_pnl_pct"] = round(sum(closed_pnls) / starting, 6) if starting > 0 else 0
    portfolio["current_drawdown_pct"] = round((peak - current) / peak, 6) if peak > 0 else 0
    portfolio["meme_allocation_pct"] = round(meme_usd / current, 4) if current > 0 else 0
    portfolio["total_exposure_pct"] = round(total_exposure_usd / current, 4) if current > 0 else 0
    portfolio["token_exposure_pct"] = {k: round(v, 4) for k, v in token_exposure.items()}
    portfolio["updated_at"] = now_iso()

    save_json_atomic(STATE_DIR / "portfolio.json", portfolio)
    print(f"  [PORTFOLIO] Balance: ${current:,.2f} | Open: {len(open_positions)} | "
          f"Drawdown: {portfolio['current_drawdown_pct']*100:.2f}%")


# ─────────────────────────────────────────────
# MAIN MONITOR
# ─────────────────────────────────────────────

def run_monitor():
    """Main position monitor loop."""
    print(f"\n[POSITION MONITOR] {now_iso()}")
    print(f"{'='*60}")

    # ── Load state ──
    positions_data = load_json(STATE_DIR / "positions.json")
    if positions_data is None:
        print("[POSITION MONITOR] FATAL: Cannot read positions.json — aborting")
        return

    price_cache = load_json(STATE_DIR / "price_cache.json")
    if not price_cache:
        print("[POSITION MONITOR] FATAL: Cannot read price_cache.json — aborting")
        return

    # Check price cache freshness
    cache_path = STATE_DIR / "price_cache.json"
    cache_mtime = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=timezone.utc)
    cache_age_min = (now_utc() - cache_mtime).total_seconds() / 60
    if cache_age_min > 10:
        print(f"[POSITION MONITOR] WARNING: price_cache.json is {cache_age_min:.0f}min old (>10min) — "
              f"skipping all exits for safety")
        return

    price_history = load_json(STATE_DIR / "price_history.json") or {}
    trailing_stops = load_trailing_stops()

    open_positions = [p for p in positions_data.get("positions", []) if p["status"] == "OPEN"]
    print(f"[POSITION MONITOR] Checking {len(open_positions)} open position(s)...")

    if not open_positions:
        print("[POSITION MONITOR] No open positions. Nothing to do.")
        return

    # ── Flash crash check (portfolio-wide) ──
    flash_triggers = check_flash_crash(price_history)
    flash_close_all_meme = len(flash_triggers) > 0

    if flash_close_all_meme:
        for ft in flash_triggers:
            print(f"  [FLASH CRASH] {ft['symbol']}: {ft['change_pct']*100:.1f}% in {FLASH_CRASH_WINDOW_MIN}min "
                  f"(${ft['old_price']:,.4f} → ${ft['recent_price']:,.4f})")

    # ── Check each position ──
    closed_pnls = []

    for position in open_positions:
        symbol = position["symbol"]
        token = position["token"]
        entry = position["entry_price"]
        current_price = price_cache.get(symbol)

        if current_price is None:
            print(f"  [{token}] WARNING: No price in cache for {symbol} — skipping")
            continue

        # Update current price in position
        position["current_price"] = current_price

        pnl_pct = (current_price - entry) / entry
        hold_hours = (now_utc() - parse_dt(position["opened_at"])).total_seconds() / 3600
        stop_price = entry * (1.0 - position.get("stop_loss_pct", 0.15))
        tp_price = entry * (1.0 + position.get("take_profit_pct", 0.30))

        pnl_sign = "+" if pnl_pct >= 0 else ""
        print(f"  [{token}] Price: ${entry:,.4f} → ${current_price:,.4f} | "
              f"P&L: {pnl_sign}{pnl_pct*100:.2f}% | Stop: ${stop_price:,.2f} | "
              f"TP: ${tp_price:,.2f} | Hold: {hold_hours:.1f}h")

        # ── Exit Condition F: Flash Crash Override ──
        if flash_close_all_meme and position.get("strategy_name", "") in ("meme-momentum", "early-launch"):
            pnl = close_position(position, current_price, "FLASH_CRASH",
                                 f"Flash crash detected — closing all meme positions")
            closed_pnls.append(pnl)
            continue

        # ── Exit Condition A: Stop-Loss ──
        triggered, reason, detail = check_stop_loss(position, current_price)
        if triggered:
            pnl = close_position(position, current_price, reason, detail)
            closed_pnls.append(pnl)
            continue

        # ── Exit Condition B: Take-Profit ──
        triggered, reason, detail = check_take_profit(position, current_price)
        if triggered:
            pnl = close_position(position, current_price, reason, detail)
            closed_pnls.append(pnl)
            continue

        # ── Exit Condition C: Trailing Stop ──
        triggered, reason, detail = check_trailing_stop(position, current_price, trailing_stops)
        if triggered:
            pnl = close_position(position, current_price, reason, detail)
            closed_pnls.append(pnl)
            continue

        # ── Exit Condition D: Time-Based Exit ──
        triggered, reason, detail = check_time_exit(position)
        if triggered:
            pnl = close_position(position, current_price, reason, detail)
            closed_pnls.append(pnl)
            continue

        # ── Exit Condition E: Volume Death (SKIPPED) ──
        # TODO: Add entry_volume to position records in sanad_pipeline.py

    # ── Save state ──
    save_json_atomic(STATE_DIR / "positions.json", positions_data)
    save_trailing_stops(trailing_stops)

    if closed_pnls:
        update_portfolio(positions_data, closed_pnls)
    else:
        # Still update current prices in positions
        print(f"\n[POSITION MONITOR] All positions OK. Next check in 3min.")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    run_monitor()
