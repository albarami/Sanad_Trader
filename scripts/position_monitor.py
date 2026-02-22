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

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
STATE_DIR = BASE_DIR / "state"
CONFIG_DIR = BASE_DIR / "config"
LOGS_DIR = BASE_DIR / "execution-logs"
SCRIPTS_DIR = BASE_DIR / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

# Import state_store for unified state management (Ticket 12)
try:
    import state_store
    state_store.install_ssot_guard()
    HAS_STATE_STORE = True
except ImportError:
    HAS_STATE_STORE = False
    print("[POSITION MONITOR] WARNING: state_store not available, using JSON fallback")

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

# Trailing stop parameters — Al-Muhasbi audit: let winners run, cut losers fast
TRAILING_ACTIVATION_PCT = 0.04   # Paper mode: activate at +4% (tighter for faster turnover)
TRAILING_DROP_PCT = 0.03         # Paper mode: 3% trail from HWM (lock profits faster)
_default_max_hold = 24
_strategy_configs = {}
try:
    import yaml as _yaml
    _thresholds = _yaml.safe_load(open(BASE_DIR / "config" / "thresholds.yaml"))
    _mode = _thresholds.get("mode", "paper")
    if _mode == "paper" and "paper_max_hold_hours" in _thresholds.get("risk", {}):
        _default_max_hold = _thresholds["risk"]["paper_max_hold_hours"]
    elif "max_hold_hours" in _thresholds.get("risk", {}):
        _default_max_hold = _thresholds["risk"]["max_hold_hours"]
    
    # Load strategy-specific configs
    _strategy_configs = _thresholds.get("strategies", {})
except Exception:
    pass
MAX_HOLD_HOURS = _default_max_hold  # Paper: 12h, Live: 24h (Al-Muhasbi approved)
FLASH_CRASH_PCT = 0.10           # 10% drop in 15 minutes
FLASH_CRASH_WINDOW_MIN = 15      # 15-minute window


def _get_strategy_config(position):
    """Get strategy-specific exit parameters, fallback to defaults."""
    strategy_name = position.get("strategy_name", "")
    if strategy_name and strategy_name in _strategy_configs:
        return _strategy_configs[strategy_name]
    return None


def check_stop_loss(position, current_price):
    """Exit Condition A: Hard stop-loss."""
    entry = position["entry_price"]
    stop_pct = position.get("stop_loss_pct", 0.15)
    side = position.get("side", "LONG").upper()
    
    if side == "SHORT":
        # SHORT: stop loss triggers when price goes UP
        stop_price = entry * (1.0 + stop_pct)
        if current_price >= stop_price:
            return True, "STOP_LOSS", f"Price ${current_price:,.4f} >= stop ${stop_price:,.4f} (+{stop_pct*100:.0f}%)"
    else:
        # LONG: stop loss triggers when price goes DOWN
        stop_price = entry * (1.0 - stop_pct)
        if current_price <= stop_price:
            return True, "STOP_LOSS", f"Price ${current_price:,.4f} <= stop ${stop_price:,.4f} (-{stop_pct*100:.0f}%)"
    return False, None, None


def check_take_profit(position, current_price):
    """Exit Condition B: Take-profit (strategy-aware)."""
    entry = position["entry_price"]
    side = position.get("side", "LONG").upper()
    
    # Check for strategy-specific target, fallback to position default, then global default
    strategy_cfg = _get_strategy_config(position)
    if strategy_cfg and "take_profit_pct" in strategy_cfg:
        tp_pct = strategy_cfg["take_profit_pct"]
    else:
        tp_pct = position.get("take_profit_pct", 0.30)
    
    if side == "SHORT":
        # SHORT: take profit when price goes DOWN
        tp_price = entry * (1.0 - tp_pct)
        if current_price <= tp_price:
            return True, "TAKE_PROFIT", f"Price ${current_price:,.4f} <= target ${tp_price:,.4f} (-{tp_pct*100:.0f}%)"
    else:
        # LONG: take profit when price goes UP
        tp_price = entry * (1.0 + tp_pct)
        if current_price >= tp_price:
            return True, "TAKE_PROFIT", f"Price ${current_price:,.4f} >= target ${tp_price:,.4f} (+{tp_pct*100:.0f}%)"
    return False, None, None




BREAKEVEN_ACTIVATION_PCT = 0.05  # Al-Muhasbi approved: move SL to entry at +5%


def check_breakeven_stop(position, current_price):
    """Exit Condition B2: Breakeven stop.
    Once position reaches +5%, move stop loss to entry price.
    This creates a zero-risk position — can only win or break even from here.
    Does NOT close the position — modifies the stop loss in place.
    Returns True if stop loss was updated, False otherwise.
    """
    entry = position["entry_price"]
    unrealized_pct = (current_price - entry) / entry

    # Only activate if position is up 5%+ and SL hasn't been moved to breakeven yet
    current_sl = position.get("stop_loss_pct", 0.15)
    breakeven_sl = 0.001  # 0.1% below entry — effectively breakeven with tiny buffer

    if unrealized_pct >= BREAKEVEN_ACTIVATION_PCT and current_sl > breakeven_sl:
        position["stop_loss_pct"] = breakeven_sl
        position["breakeven_activated"] = True
        token = position.get("token", "?")
        print(f"    [BREAKEVEN] {token}: +{unrealized_pct*100:.1f}% — SL moved to entry (breakeven)")
        return True
    return False


def check_trailing_stop(position, current_price, trailing_stops):
    """Exit Condition C: Trailing stop (strategy-aware activation and drop)."""
    symbol = position["symbol"]
    entry = position["entry_price"]
    side = position.get("side", "LONG").upper()
    
    # Calculate unrealized P&L based on side
    if side == "SHORT":
        unrealized_pct = (entry - current_price) / entry  # Profit when price drops
    else:
        unrealized_pct = (current_price - entry) / entry  # Profit when price rises

    # Get strategy-specific trailing parameters
    strategy_cfg = _get_strategy_config(position)
    activation_pct = TRAILING_ACTIVATION_PCT
    drop_pct = TRAILING_DROP_PCT
    
    if strategy_cfg:
        # Use strategy trailing_stop_pct as drop percentage
        # Activation stays at default unless strategy specifies it
        if "trailing_stop_pct" in strategy_cfg:
            drop_pct = strategy_cfg["trailing_stop_pct"]

    ts_data = trailing_stops.get(symbol, {})

    # Check if trailing stop should activate
    if not ts_data.get("activated", False):
        if unrealized_pct >= activation_pct:
            # Activate trailing stop
            if side == "SHORT":
                # For SHORT, track low-water mark (price LOW)
                ts_data = {
                    "low_water_mark": current_price,
                    "activated": True,
                    "activated_at": now_iso(),
                }
            else:
                # For LONG, track high-water mark (price HIGH)
                ts_data = {
                    "high_water_mark": current_price,
                    "activated": True,
                    "activated_at": now_iso(),
                }
            trailing_stops[symbol] = ts_data
            print(f"    [TRAILING] Activated for {symbol} at +{unrealized_pct*100:.1f}% | Mark: ${current_price:,.4f}")
        return False, None, None

    # Trailing stop is active — update water mark
    if side == "SHORT":
        # SHORT: Track lowest price reached, exit if price rises from LWM
        lwm = ts_data.get("low_water_mark", current_price)
        if current_price < lwm:
            ts_data["low_water_mark"] = current_price
            lwm = current_price
            trailing_stops[symbol] = ts_data
        
        # Check if price rose above threshold from low-water mark
        rise_from_lwm = (current_price - lwm) / lwm
        if rise_from_lwm >= drop_pct:
            return True, "TRAILING_STOP", (
                f"Price ${current_price:,.4f} rose {rise_from_lwm*100:.1f}% from LWM ${lwm:,.4f} "
                f"(threshold: {drop_pct*100:.0f}%)"
            )
    else:
        # LONG: Track highest price reached, exit if price drops from HWM
        hwm = ts_data.get("high_water_mark", current_price)
        if current_price > hwm:
            ts_data["high_water_mark"] = current_price
            hwm = current_price
            trailing_stops[symbol] = ts_data

        # Check if price dropped below threshold from high-water mark
        drop_from_hwm = (hwm - current_price) / hwm
        if drop_from_hwm >= drop_pct:
            return True, "TRAILING_STOP", (
                f"Price ${current_price:,.4f} dropped {drop_from_hwm*100:.1f}% from HWM ${hwm:,.4f} "
                f"(threshold: {drop_pct*100:.0f}%)"
            )

    return False, None, None


def check_time_exit(position):
    """Exit Condition D: Time-based exit using strategy > Bull's timeframe > tier defaults."""
    opened_at = parse_dt(position["opened_at"])
    hold_hours = (now_utc() - opened_at).total_seconds() / 3600
    bull_timeframe = position.get("bull_timeframe", "")
    
    # Priority 1: Strategy-specific max_hold_hours
    strategy_cfg = _get_strategy_config(position)
    if strategy_cfg and "max_hold_hours" in strategy_cfg:
        max_hold = strategy_cfg["max_hold_hours"]
    else:
        # Priority 2: Bull's timeframe if available
        asset_tier = position.get("asset_tier", "TIER_3_MICRO")
        
        try:
            from exit_time_parser import extract_max_hold_hours
            max_hold = extract_max_hold_hours(bull_timeframe, asset_tier)
        except Exception:
            # Priority 3: Fallback to tier-based defaults
            tier_defaults = {
                "TIER_1_MACRO": 168,        # 7 days
                "TIER_2_ALT_LARGE": 120,    # 5 days
                "TIER_3_MEME_CEX": 72,      # 3 days
                "TIER_3_MICRO": 24,         # 1 day
            }
            max_hold = tier_defaults.get(asset_tier, MAX_HOLD_HOURS)
    
    if hold_hours > max_hold:
        return True, "TIME_EXIT", f"Position open {hold_hours:.1f}h > {max_hold}h (from strategy)"
    return False, None, None


def check_volume_death(position, current_price):
    """Exit Condition E: Volume death (SKIPPED — no entry volume recorded)."""
    # TODO: Add entry_volume to position records in sanad_pipeline.py
    return False, None, None




def check_momentum_decay(position, current_price):
    """Exit Condition E2: Momentum decay.
    Al-Muhasbi approved: exit if BOTH conditions met:
    1. 2-hour rolling return goes negative (price below 2h ago)
    2. Current volume dropped >30% from entry volume
    Both conditions required to avoid whipsaw on normal pullbacks.
    """
    import json
    from pathlib import Path

    token = position.get("token", "?")
    entry = position["entry_price"]
    symbol = position.get("symbol", "")

    # Condition 1: Check 2-hour price trend
    # Use price_history.json for historical comparison
    history_path = Path("/data/.openclaw/workspace/trading/state/price_history.json")
    try:
        history = json.loads(history_path.read_text()) if history_path.exists() else {}
        prices = history.get(symbol, [])
        if len(prices) < 40:  # Need ~2h of 3-min snapshots (40 data points)
            return False, None, None

        price_2h_ago = prices[-40] if isinstance(prices[-40], (int, float)) else prices[-40].get("price", 0)
        two_hour_return = (current_price - price_2h_ago) / price_2h_ago if price_2h_ago > 0 else 0

        if two_hour_return >= 0:
            return False, None, None  # Still positive — no decay

        # Condition 2: Volume drop >30% from entry
        # Use entry volume vs current volume from price cache
        entry_vol = position.get("entry_volume_24h", 0)
        if entry_vol <= 0:
            return False, None, None  # No entry volume recorded — skip

        cache_path = Path("/data/.openclaw/workspace/trading/state/price_cache.json")
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        current_vol = cache.get(symbol, {}).get("volume_24h", 0)
        if current_vol <= 0:
            return False, None, None

        vol_change = (current_vol - entry_vol) / entry_vol
        if vol_change < -0.30:  # Volume dropped >30%
            return True, "MOMENTUM_DECAY", (
                f"2h return: {two_hour_return*100:.1f}% (negative) AND "
                f"volume dropped {abs(vol_change)*100:.0f}% from entry. "
                f"Both momentum decay conditions met."
            )
    except Exception as e:
        print(f"    [MOMENTUM] Error checking {token}: {e}")

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
    side = position.get("side", "LONG").upper()

    # Calculate P&L based on position side
    if side == "SHORT":
        # SHORT: profit when price drops
        pnl_pct = (entry - current_price) / entry
        pnl_usd = (entry - current_price) * qty
    else:
        # LONG: profit when price rises
        pnl_pct = (current_price - entry) / entry
        pnl_usd = (current_price - entry) * qty
    
    # V4: Load execution costs from thresholds.yaml
    try:
        import yaml as _yaml
        _th_path = Path(os.environ.get("SANAD_HOME", str(Path(__file__).resolve().parent.parent))) / "config" / "thresholds.yaml"
        _th_cfg = _yaml.safe_load(_th_path.read_text()) if _th_path.exists() else {}
        _exec_costs = _th_cfg.get("execution_costs", {})
    except Exception:
        _exec_costs = {}
    _paper_fee_bps = float(_exec_costs.get("paper_fee_bps", 10))
    _paper_slip_bps = float(_exec_costs.get("paper_slippage_bps", 5))
    
    # Paper SELL exec price = mid * (1 - slippage/10000) — adverse
    mid_exit_price = current_price
    exec_exit_price = mid_exit_price * (1 - _paper_slip_bps / 10000.0)
    
    # Recompute with exec price
    if side == "SHORT":
        pnl_pct = (entry - exec_exit_price) / entry
        pnl_usd = (entry - exec_exit_price) * qty
    else:
        pnl_pct = (exec_exit_price - entry) / entry
        pnl_usd = (exec_exit_price - entry) * qty
    
    fee_usd = exec_exit_price * qty * (_paper_fee_bps / 10000.0)
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

    # Record execution quality
    try:
        from execution_quality import record_execution
        record_execution(paper_trade)
    except Exception as e:
        print(f"    Execution quality recording error: {e}")

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

    # ── TELEGRAM NOTIFICATION ──
    try:
        import notifier
        emoji = "🟢" if net_pnl_usd >= 0 else "🔴"
        pnl_sign = "+" if net_pnl_usd >= 0 else ""
        notifier.send(
            f"{emoji} SELL {position['token']}/USDT\n\n"
            f"Reason: {reason}\n"
            f"Entry: {entry:,.4f}\n"
            f"Exit: {current_price:,.4f}\n"
            f"PnL: {pnl_sign}{pnl_pct*100:.1f}% ({pnl_sign}{net_pnl_usd:.2f})\n"
            f"Hold: {hold_hours:.1f}h\n\n"
            f"Strategy: {position.get('strategy_name', '?')}\n"
            f"Sanad Score: {position.get('sanad_score', '?')}",
            level="L2",
            title=f"SELL {position['token']}"
        )
    except Exception as e:
        print(f"    WARNING: Telegram notification failed: {e}")

    # Add to trade_history.json (for Gate #13 cooldown)
    try:
        # Extract attribution (fail-safe)
        source_raw = position.get("signal_source_canonical", position.get("signal_source", "unknown"))
        attribution = {"source_primary": "unknown:general", "sources_used": [], "enrichers_used": [], "source_raw": source_raw}
        try:
            from signal_normalizer import parse_attribution, canonical_source
            # Construct minimal signal dict for attribution
            signal_stub = {"source": source_raw}
            attribution = parse_attribution(signal_stub)
        except Exception:
            # Fail-safe: use canonical_source
            try:
                from signal_normalizer import canonical_source
                attribution["source_primary"] = canonical_source(source_raw).get("source_key", "unknown:general")
                attribution["sources_used"] = [attribution["source_primary"]]
            except Exception:
                pass  # Use defaults
        
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
            "source": source_raw,  # Keep for backward compatibility
            "source_raw": attribution["source_raw"],
            "source_primary": attribution["source_primary"],
            "sources_used": attribution["sources_used"],
            "enrichers_used": attribution["enrichers_used"],
            "strategy": position.get("strategy_name", "unknown"),
            "regime_at_entry": position.get("regime_tag", "UNKNOWN"),
        })
        trade_history["trades"] = trades
        save_json_atomic(th_path, trade_history)
    except Exception as e:
        print(f"    WARNING: trade_history update failed: {e}")

    # ── Post-trade analysis (Genius Memory) ──
    try:
        import post_trade_analyzer
        # Convert position to trade format for analyzer
        trade_record = {
            "trade_id": position["id"],
            "token": position["token"],
            "entry_price": entry,
            "exit_price": current_price,
            "pnl_pct": pnl_pct,
            "source": position.get("signal_source_canonical", position.get("signal_source", "unknown")),
            "strategy": position.get("strategy_name", "unknown"),
            "exit_reason": reason,
            "entry_time": position.get("opened_at"),
            "exit_time": now.isoformat(),
            "hold_duration_hours": (now - parse_dt(position["opened_at"])).total_seconds() / 3600,
            "trust_score": position.get("sanad_score", 0),
            "cross_source_count": 1,  # TODO: track corroboration in positions
        }
        post_trade_analyzer.analyze_trade(trade_record)
        print(f"    Genius Memory: post-trade analysis complete")
    except Exception as e:
        print(f"    WARNING: Post-trade analysis failed: {e}")
        import traceback
        traceback.print_exc()

    # ── v3.1 SQLite Close + Learning Loop ──
    # 1. Ensure position exists in SQLite (v3.0→v3.1 bridge) and close it
    # 2. Trigger learning loop for Thompson/UCB1 stats update
    # Non-blocking: if anything fails, learning_status stays PENDING and cron picks it up.
    try:
        import state_store
        state_store.init_db()
        position_id_str = position.get("position_id") or position.get("id", "")
        
        # Try V4 close_position first (handles fills, gross/net, reward)
        try:
            state_store.close_position(
                position_id=position_id_str,
                close_reason=reason,
                close_price=exec_exit_price,
                exit_expected_price=mid_exit_price,
                exit_slippage_bps=_paper_slip_bps,
                exit_fee_bps=_paper_fee_bps,
                venue="paper",
            )
            position_id = position_id_str
        except (ValueError, Exception) as v4_err:
            # Fallback: ensure_and_close (v3.0→v3.1 bridge for legacy positions)
            position_id = state_store.ensure_and_close_position(position, {
                "close_price": exec_exit_price,
                "close_reason": reason,
                "exit_price": exec_exit_price,
                "exit_reason": reason,
                "pnl_usd": net_pnl_usd,
                "pnl_pct": pnl_pct,
            })
        print(f"    SQLite: position {position_id[:8]}... CLOSED (learning_status=PENDING)")
        # Immediately attempt learning
        import learning_loop
        learning_loop.process_closed_position(position_id)
        print(f"    Learning Loop: stats updated (DONE)")
    except Exception as e:
        # Non-blocking: cron fallback will process PENDING positions
        print(f"    SQLite/Learning: deferred to cron ({e})")

    return net_pnl_usd


def update_portfolio(positions_data, closed_pnls):
    """Recalculate portfolio after closes. Balance always derived from trade_history.json."""
    # Load portfolio from SQLite (single source of truth)
    if HAS_STATE_STORE:
        try:
            portfolio = state_store.get_portfolio()
        except Exception as e:
            print(f"[POSITION MONITOR] WARNING: state_store.get_portfolio failed ({e}), using JSON fallback")
            portfolio = load_json(STATE_DIR / "portfolio.json")
    else:
        portfolio = load_json(STATE_DIR / "portfolio.json")
    
    if not portfolio:
        print("[POSITION MONITOR] ERROR: Cannot load portfolio for update")
        return

    all_positions = positions_data.get("positions", [])
    open_positions = [p for p in all_positions if p["status"] == "OPEN"]

    # ALWAYS derive balance from trade_history.json (source of truth)
    starting = portfolio.get("starting_balance_usd") or 10000.0
    trade_history = load_json(STATE_DIR / "trade_history.json") or {}
    trades = trade_history.get("trades", trade_history) if isinstance(trade_history, dict) else trade_history
    total_realized_pnl = sum(
        float(t.get("pnl_usd", t.get("net_pnl_usd", 0)) or 0)
        for t in trades if isinstance(t, dict)
    )
    current = starting + total_realized_pnl
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

    # Calculate unrealized P&L from open positions
    unrealized_pnl = 0.0
    for p in open_positions:
        entry = p.get("entry_price", 0)
        cur_price = p.get("current_price", entry)
        size = p.get("position_usd", 0)
        if entry and entry > 0 and cur_price:
            unrealized_pnl += (cur_price - entry) / entry * size

    # Total equity = cash balance + unrealized P&L
    total_equity = current + unrealized_pnl
    peak = max(portfolio.get("peak_balance_usd", starting), total_equity)

    portfolio["cash_balance_usd"] = round(current, 2)
    portfolio["unrealized_pnl_usd"] = round(unrealized_pnl, 2)
    portfolio["current_balance_usd"] = round(total_equity, 2)
    portfolio["peak_balance_usd"] = round(peak, 2)
    portfolio["open_position_count"] = len(open_positions)
    # Derive daily PnL from trade_history since last daily reset
    from datetime import datetime, timezone
    daily_reset_at = portfolio.get("daily_reset_at")
    if not daily_reset_at:
        daily_reset_at = datetime.now(timezone.utc).isoformat()
        portfolio["daily_reset_at"] = daily_reset_at
        print("  [PORTFOLIO] WARNING: daily_reset_at missing — set to now")
    else:
        try:
            reset_dt = datetime.fromisoformat(daily_reset_at)
            age_hours = (datetime.now(timezone.utc) - reset_dt).total_seconds() / 3600
            if age_hours > 36:
                print(f"  [PORTFOLIO] ALERT: daily_reset_at is {age_hours:.0f}h old — daily_pnl_reset cron may have failed")
        except Exception:
            pass
    daily_pnl_usd = sum(
        float(t.get("pnl_usd", t.get("net_pnl_usd", 0)) or 0)
        for t in trades if isinstance(t, dict)
        and (t.get("closed_at", t.get("timestamp", "")) >= daily_reset_at)
    )
    # Build updates dict for state_store
    updates = {
        "current_balance_usd": round(total_equity, 2),
        "open_position_count": len(open_positions),
        "daily_pnl_usd": round(daily_pnl_usd, 2),
        "max_drawdown_pct": round((peak - total_equity) / peak, 6) if peak > 0 else 0,
    }
    
    # Update via SQLite (auto-syncs to JSON cache)
    if HAS_STATE_STORE:
        try:
            state_store.update_portfolio(updates)
        except Exception as e:
            print(f"[POSITION MONITOR] WARNING: state_store.update_portfolio failed ({e}), using JSON fallback")
            # Fallback to JSON write with all fields
            portfolio["cash_balance_usd"] = round(current, 2)
            portfolio["unrealized_pnl_usd"] = round(unrealized_pnl, 2)
            portfolio["current_balance_usd"] = round(total_equity, 2)
            portfolio["peak_balance_usd"] = round(peak, 2)
            portfolio["open_position_count"] = len(open_positions)
            portfolio["daily_pnl_usd"] = round(daily_pnl_usd, 2)
            portfolio["daily_pnl_pct"] = round(daily_pnl_usd / starting, 6) if starting > 0 else 0
            portfolio["current_drawdown_pct"] = round((peak - total_equity) / peak, 6) if peak > 0 else 0
            portfolio["meme_allocation_pct"] = round(meme_usd / total_equity, 4) if total_equity > 0 else 0
            portfolio["total_exposure_pct"] = round(total_exposure_usd / total_equity, 4) if total_equity > 0 else 0
            portfolio["token_exposure_pct"] = {k: round(v, 4) for k, v in token_exposure.items()}
            portfolio["updated_at"] = now_iso()
            save_json_atomic(STATE_DIR / "portfolio.json", portfolio)  # sync_json_cache equivalent
    else:
        # No state_store, use JSON — fallback
        portfolio["cash_balance_usd"] = round(current, 2)
        portfolio["unrealized_pnl_usd"] = round(unrealized_pnl, 2)
        portfolio["current_balance_usd"] = round(total_equity, 2)
        portfolio["peak_balance_usd"] = round(peak, 2)
        portfolio["open_position_count"] = len(open_positions)
        portfolio["daily_pnl_usd"] = round(daily_pnl_usd, 2)
        portfolio["daily_pnl_pct"] = round(daily_pnl_usd / starting, 6) if starting > 0 else 0
        portfolio["current_drawdown_pct"] = round((peak - total_equity) / peak, 6) if peak > 0 else 0
        portfolio["meme_allocation_pct"] = round(meme_usd / total_equity, 4) if total_equity > 0 else 0
        portfolio["total_exposure_pct"] = round(total_exposure_usd / total_equity, 4) if total_equity > 0 else 0
        portfolio["token_exposure_pct"] = {k: round(v, 4) for k, v in token_exposure.items()}
        portfolio["updated_at"] = now_iso()
        save_json_atomic(STATE_DIR / "portfolio.json", portfolio)  # fallback if no state_store
    
    unr_sign = "+" if unrealized_pnl >= 0 else ""
    print(f"  [PORTFOLIO] Equity: ${total_equity:,.2f} (cash ${current:,.2f} {unr_sign}${unrealized_pnl:.2f} unrealized) | "
          f"Open: {len(open_positions)} | Drawdown: {updates['max_drawdown_pct']*100:.2f}%")


# ─────────────────────────────────────────────
# MAIN MONITOR
# ─────────────────────────────────────────────

def run_monitor():
    """Main position monitor loop."""
    print(f"\n[POSITION MONITOR] {now_iso()}")
    print(f"{'='*60}")

    # ── Load state from SQLite (single source of truth) ──
    if HAS_STATE_STORE:
        try:
            all_positions = state_store.get_all_positions()
            positions_data = {"positions": all_positions}
        except Exception as e:
            print(f"[POSITION MONITOR] WARNING: state_store failed ({e}), using JSON fallback")
            positions_data = load_json(STATE_DIR / "positions.json")
    else:
        positions_data = load_json(STATE_DIR / "positions.json")
    
    if positions_data is None:
        print("[POSITION MONITOR] FATAL: Cannot read positions — aborting")
        return

    # ── Normalize v3.1 schema → v3.0 compat aliases ──
    for p in positions_data.get("positions", []):
        if "symbol" not in p and "token_address" in p:
            p["symbol"] = p["token_address"]
        if "token" not in p:
            # Extract short token name from features_json if available
            features = p.get("features_json")
            if isinstance(features, str):
                try:
                    import json as _json
                    features = _json.loads(features)
                except Exception:
                    features = {}
            if isinstance(features, dict):
                entry_sig = features.get("entry_signal", {})
                p["token"] = entry_sig.get("token", p.get("symbol", "UNKNOWN"))
            else:
                p["token"] = p.get("symbol", "UNKNOWN")
        if "exchange" not in p:
            p["exchange"] = "raydium" if p.get("chain") == "solana" else "binance"
        if "position_usd" not in p:
            p["position_usd"] = p.get("size_usd", 0)
        if "quantity" not in p and "size_token" in p:
            p["quantity"] = p["size_token"]
        if "stop_loss_pct" not in p:
            p["stop_loss_pct"] = 0.15
        if "take_profit_pct" not in p:
            p["take_profit_pct"] = 0.30
        if "opened_at" not in p and "created_at" in p:
            p["opened_at"] = p["created_at"]
        if "side" not in p:
            p["side"] = "LONG"

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

    # Update DEX prices for non-Binance positions (P0-2 fix)
    dex_positions = [p for p in open_positions if p.get("exchange") not in ("binance", "mexc")]
    if dex_positions:
        print(f"[POSITION MONITOR] Fetching {len(dex_positions)} DEX prices...")
        try:
            import sys
            sys.path.insert(0, str(STATE_DIR.parent / "scripts"))
            from birdeye_client import get_token_overview
            
            for pos in dex_positions:
                token = pos.get("token")
                token_address = pos.get("token_address")
                
                if not token_address:
                    print(f"  [DEX] {token}: SKIPPED (no token_address in position)")
                    continue
                
                try:
                    overview = get_token_overview(token_address)
                    if overview and overview.get("price"):
                        dex_price = float(overview["price"])
                        symbol = pos.get("symbol", token)
                        price_cache[symbol] = dex_price
                        print(f"  [DEX] {token}: ${dex_price}")
                except Exception as e:
                    print(f"  [DEX] Price fetch failed for {token}: {e}")
        except ImportError as e:
            print(f"[POSITION MONITOR] Birdeye client not available: {e}")
    
    for position in open_positions:
        # Handle both legacy (symbol/token) and v3.1 (token_address) formats
        symbol = position.get("symbol") or position.get("token_address", "UNKNOWN")
        token = position.get("token") or symbol
        entry = position["entry_price"]

        # Price lookup: try symbol directly, then SYMBOL+USDT for CEX positions
        current_price = price_cache.get(symbol)
        if current_price is None and position.get("exchange") in ("binance", "mexc"):
            current_price = price_cache.get(symbol + "USDT")
        if current_price is None:
            # Also try token name + USDT (e.g. BTC → BTCUSDT)
            current_price = price_cache.get(token + "USDT") if token != symbol else None

        if current_price is None:
            print(f"  [{token}] WARNING: No price in cache for {symbol} — skipping")
            continue

        # Update current price in position
        position["current_price"] = current_price

        pnl_pct = (current_price - entry) / entry
        opened_at = position.get("opened_at") or position.get("created_at")
        hold_hours = (now_utc() - parse_dt(opened_at)).total_seconds() / 3600
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

        # ── Exit Condition B2: Breakeven Stop ──
        check_breakeven_stop(position, current_price)
        # (does not close — modifies SL in place, then continues to other checks)

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

        # ── Exit Condition E2: Momentum Decay ──
        triggered, reason, detail = check_momentum_decay(position, current_price)
        if triggered:
            pnl = close_position(position, current_price, reason, detail)
            closed_pnls.append(pnl)
            continue

        should_close = False
        close_reason = ""
        close_detail = ""

        # ── Exit Condition G: Whale Exit Signal ──
        try:
            from whale_exit_trigger import check_whale_exits
            whale_exits = check_whale_exits()
            if whale_exits:
                for we in whale_exits:
                    if we.get("token") == position["token"] and we.get("urgency", 0) >= 2:
                        should_close = True
                        close_reason = "WHALE_EXIT"
                        close_detail = f"Whale exit signal: urgency {we['urgency']}, {we.get('description', '')}"
                        break
        except Exception as e:
            print(f"    Whale exit check error: {e}")

        # ── Exit Condition H: Sentiment Reversal ──
        if not should_close:
            try:
                from sentiment_exit_trigger import check_reversals
                sent_exits = check_reversals()
                if sent_exits:
                    for se in sent_exits:
                        if se.get("token") == position["token"] and se.get("urgency", 0) >= 2:
                            should_close = True
                            close_reason = "SENTIMENT_REVERSAL"
                            close_detail = f"Sentiment reversal: urgency {se['urgency']}, {se.get('description', '')}"
                            break
            except Exception as e:
                print(f"    Sentiment exit check error: {e}")

        if should_close:
            pnl = close_position(position, current_price, close_reason, close_detail)
            closed_pnls.append(pnl)
            continue

    # ── Save state ──
    # NOTE: Positions are auto-synced to JSON by state_store.sync_json_cache()
    # Only save if state_store not available (backward compat)
    if not HAS_STATE_STORE:
        save_json_atomic(STATE_DIR / "positions.json", positions_data)  # fallback if no state_store
    save_trailing_stops(trailing_stops)

    # Always update portfolio with mark-to-market (not just on closes)
    update_portfolio(positions_data, closed_pnls)

    if not closed_pnls:
        print(f"\n[POSITION MONITOR] All positions OK. Next check in 1min.")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    run_monitor()
