#!/usr/bin/env python3
"""
Strategy DSL & Registry — Sprint 10.2
Formal strategy definitions with activation rules.

Each strategy is a dict with:
- name, description
- entry_conditions (list of checks)
- exit_conditions
- sizing_override (optional)
- active (bool)
- performance tracking
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STRATEGIES_DIR = BASE_DIR / "strategies"
STATE_DIR = BASE_DIR / "state"
sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[STRATEGY] {ts} {msg}", flush=True)


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
# Strategy Definitions (DSL)
# ─────────────────────────────────────────────────────────

STRATEGIES = {
    "meme-momentum": {
        "name": "Meme Momentum",
        "description": "Catch meme coins during rapid volume+social surges",
        "chain": ["solana", "binance"],
        "direction": "LONG",
        "entry_conditions": {
            "min_volume_24h_usd": 100000,
            "min_price_change_pct": 10,
            "max_price_change_pct": 200,
            "min_holder_count": 500,
            "max_top10_holder_pct": 60,
            "min_sanad_score": 70,
            "min_social_score": 40,
            "max_age_hours": 72,
        },
        "exit_conditions": {
            "stop_loss_pct": 3,
            "take_profit_pct": 8,
            "trailing_stop_pct": 2,
            "max_hold_hours": 6,
        },
        "sizing": {
            "base_pct": 7.5,
            "max_pct": 15,
            "kelly_override": None,
        },
        "active": True,
    },
    "paper-mode-any": {
        "name": "Paper Mode - Any Signal",
        "description": "Fallback strategy for paper trading: accept any signal that passes Sanad",
        "chain": [],  # Any chain
        "direction": "",  # Any direction
        "entry_conditions": {
            "min_sanad_score": 50,  # Only requirement: pass Sanad minimum
        },
        "exit_conditions": {
            "stop_loss_pct": 15,
            "take_profit_pct": 30,
            "trailing_stop_pct": 3,
            "max_hold_hours": 24,
        },
        "sizing": {
            "base_pct": 5.0,
            "max_pct": 10,
            "kelly_override": None,
        },
        "active": True,
    },
    "early-launch": {
        "name": "Early Launch Sniper",
        "description": "Enter within first 2 hours of token launch on Pump.fun",
        "chain": ["solana"],
        "direction": "LONG",
        "entry_conditions": {
            "max_age_hours": 2,
            "min_volume_24h_usd": 10000,
            "min_holder_count": 50,
            "max_top10_holder_pct": 80,
            "honeypot_verdict": "SAFE",
            "rugpull_verdict_not": ["RUG", "DANGER"],
            "min_sanad_score": 65,
        },
        "exit_conditions": {
            "stop_loss_pct": 4,
            "take_profit_pct": 10,
            "trailing_stop_pct": 3,
            "max_hold_hours": 4,
        },
        "sizing": {
            "base_pct": 5,
            "max_pct": 10,
            "kelly_override": 0.1,
        },
        "active": True,
    },
    "sentiment-divergence": {
        "name": "Sentiment Divergence",
        "description": "Trade when social sentiment diverges from price action",
        "chain": ["solana", "binance", "ethereum"],
        "direction": "LONG",
        "entry_conditions": {
            "min_social_score": 60,
            "max_price_change_pct": -5,
            "min_volume_24h_usd": 500000,
            "min_sanad_score": 75,
        },
        "exit_conditions": {
            "stop_loss_pct": 3,
            "take_profit_pct": 6,
            "trailing_stop_pct": 2,
            "max_hold_hours": 6,
        },
        "sizing": {
            "base_pct": 7.5,
            "max_pct": 15,
        },
        "active": True,
    },
    "whale-following": {
        "name": "Whale Following",
        "description": "Mirror large wallet accumulation patterns",
        "chain": ["solana"],
        "direction": "LONG",
        "entry_conditions": {
            "min_whale_tx_count": 3,
            "min_whale_volume_usd": 50000,
            "min_sanad_score": 70,
            "honeypot_verdict": "SAFE",
        },
        "exit_conditions": {
            "stop_loss_pct": 3,
            "take_profit_pct": 6,
            "trailing_stop_pct": 2,
            "max_hold_hours": 8,
        },
        "sizing": {
            "base_pct": 7.5,
            "max_pct": 15,
        },
        "active": True,
    },
    "cex-listing-play": {
        "name": "CEX Listing Anticipation",
        "description": "Buy tokens likely to get listed on major CEX",
        "chain": ["solana", "ethereum"],
        "direction": "LONG",
        "entry_conditions": {
            "min_holder_count": 10000,
            "min_volume_24h_usd": 1000000,
            "min_sanad_score": 80,
            "min_social_score": 50,
        },
        "exit_conditions": {
            "stop_loss_pct": 3,
            "take_profit_pct": 8,
            "trailing_stop_pct": 3,
            "max_hold_hours": 8,
        },
        "sizing": {
            "base_pct": 7.5,
            "max_pct": 15,
        },
        "active": True,
    },
    "mean-reversion": {
        "name": "Mean Reversion",
        "description": "BB + RSI oversold reversals on majors",
        "chain": ["binance"],
        "direction": "LONG",
        "entry_conditions": {
            "min_sanad_score": 60,
            "rsi_below": 30,
            "price_below_bb_lower": True,
            "min_volume_24h_usd": 1000000,
        },
        "exit_conditions": {
            "stop_loss_pct": 2,
            "take_profit_pct": 4,
            "trailing_stop_pct": 1.5,
            "max_hold_hours": 24,
        },
        "sizing": {"base_pct": 5, "max_pct": 10},
        "active": True,
        "instruments": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    },
    "trend-following": {
        "name": "Trend Following",
        "description": "EMA crossover + ATR stops on majors",
        "chain": ["binance"],
        "direction": "LONG",
        "entry_conditions": {
            "min_sanad_score": 50,
            "ema20_above_ema50": True,
            "min_volume_24h_usd": 500000,
        },
        "exit_conditions": {
            "stop_loss_pct": 3,
            "take_profit_pct": 6,
            "trailing_stop_pct": 2,
            "max_hold_hours": 48,
        },
        "sizing": {"base_pct": 5, "max_pct": 10},
        "active": True,
        "instruments": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    },
    "scalping": {
        "name": "Scalping",
        "description": "MACD crossover micro-moves on high-volume majors",
        "chain": ["binance"],
        "direction": "LONG",
        "entry_conditions": {
            "min_sanad_score": 40,
            "macd_bullish_cross": True,
            "min_volume_24h_usd": 5000000,
        },
        "exit_conditions": {
            "stop_loss_pct": 0.5,
            "take_profit_pct": 1,
            "trailing_stop_pct": 0.3,
            "max_hold_hours": 4,
        },
        "sizing": {"base_pct": 3, "max_pct": 7},
        "active": True,
        "instruments": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    },
    "whale-distribution-fade": {
        "name": "Whale Distribution Fade",
        "description": "Short tokens when 2+ tracked whales are distributing (selling)",
        "chain": ["solana", "binance"],
        "direction": "SHORT",
        "entry_conditions": {
            "min_distribution_whale_count": 2,
            "min_distribution_alerts": 3,
            "max_price_change_1h_pct": 5,
            "min_volume_24h_usd": 100000,
            "min_sanad_score": 50,
        },
        "exit_conditions": {
            "stop_loss_pct": 5,
            "take_profit_pct": 10,
            "trailing_stop_pct": 3,
            "max_hold_hours": 48,
        },
        "sizing": {
            "base_pct": 5,
            "max_pct": 10,
            "kelly_override": None,
        },
        "active": True,
    },
    "bear-momentum": {
        "name": "Bear Momentum",
        "description": "Short tokens showing sustained weakness in bear regime",
        "chain": ["binance"],
        "direction": "SHORT",
        "entry_conditions": {
            "min_price_drop_24h_pct": -5,
            "max_price_drop_24h_pct": -30,
            "min_volume_24h_usd": 500000,
            "fear_greed_max": 25,
            "min_sanad_score": 60,
        },
        "exit_conditions": {
            "stop_loss_pct": 4,
            "take_profit_pct": 8,
            "trailing_stop_pct": 2,
            "max_hold_hours": 24,
        },
        "sizing": {
            "base_pct": 5,
            "max_pct": 10,
            "kelly_override": None,
        },
        "active": True,
        "instruments": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    },
    "mean-reversion-short": {
        "name": "Mean Reversion Short",
        "description": "Short overbought bounces in bear regime (RSI > 70 + BB upper)",
        "chain": ["binance"],
        "direction": "SHORT",
        "entry_conditions": {
            "min_sanad_score": 60,
            "rsi_above": 70,
            "price_above_bb_upper": True,
            "min_volume_24h_usd": 1000000,
            "fear_greed_max": 40,
        },
        "exit_conditions": {
            "stop_loss_pct": 3,
            "take_profit_pct": 5,
            "trailing_stop_pct": 1.5,
            "max_hold_hours": 24,
        },
        "sizing": {
            "base_pct": 3,
            "max_pct": 7,
        },
        "active": True,
        "instruments": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    },
}


# ─────────────────────────────────────────────────────────
# Registry Functions
# ─────────────────────────────────────────────────────────

def get_all_strategies() -> dict:
    return STRATEGIES


def get_strategy(name: str) -> dict | None:
    return STRATEGIES.get(name)


def get_active_strategies() -> dict:
    return {k: v for k, v in STRATEGIES.items() if v.get("active")}


def match_signal_to_strategies(signal: dict) -> list:
    """Find which strategies match a given signal."""
    matches = []
    chain = signal.get("chain", "").lower()
    direction = signal.get("direction", "").upper()

    for name, strat in get_active_strategies().items():
        # Chain filter
        if chain and strat.get("chain") and chain not in strat["chain"]:
            continue

        # Direction filter
        if direction and strat.get("direction") and direction != strat["direction"]:
            continue

        # Check entry conditions
        conditions = strat.get("entry_conditions", {})
        met = True
        unmet = []

        for key, threshold in conditions.items():
            if key == "min_volume_24h_usd":
                val = signal.get("volume_24h")
                if val is not None and val < threshold:
                    met = False; unmet.append(f"{key}: {val} < {threshold}")
            elif key == "min_price_change_pct":
                val = signal.get("price_change_24h")
                if val is not None and val < threshold:
                    met = False; unmet.append(f"{key}: {val} < {threshold}")
            elif key == "max_price_change_pct":
                val = signal.get("price_change_24h")
                if val is not None and val > threshold:
                    met = False; unmet.append(f"{key}: {val} > {threshold}")
            elif key == "min_sanad_score":
                val = signal.get("score", signal.get("sanad_score", signal.get("signal_score")))
                if val is not None and val < threshold:
                    met = False; unmet.append(f"{key}: {val} < {threshold}")
            elif key == "min_holder_count":
                val = signal.get("holder_count")
                if val is not None and val < threshold:
                    met = False; unmet.append(f"{key}: {val} < {threshold}")
            elif key == "rsi_below":
                indicators = signal.get("indicators", {})
                val = indicators.get("rsi", 50)
                if val >= threshold:
                    met = False; unmet.append(f"{key}: RSI {val} >= {threshold}")
            elif key == "price_below_bb_lower":
                indicators = signal.get("indicators", {})
                price = indicators.get("current_price", 0)
                bb_lower = indicators.get("bb_lower", 0)
                if threshold and price >= bb_lower:
                    met = False; unmet.append(f"{key}: price {price} >= bb_lower {bb_lower}")
            elif key == "ema20_above_ema50":
                indicators = signal.get("indicators", {})
                ema20 = indicators.get("ema20", 0)
                ema50 = indicators.get("ema50", 0)
                if threshold and ema20 <= ema50:
                    met = False; unmet.append(f"{key}: EMA20 {ema20} <= EMA50 {ema50}")
            elif key == "macd_bullish_cross":
                indicators = signal.get("indicators", {})
                macd_hist = indicators.get("macd_hist", 0)
                if threshold and macd_hist <= 0:
                    met = False; unmet.append(f"{key}: MACD histogram {macd_hist} <= 0")

        matches.append({
            "strategy": name,
            "matched": met,
            "unmet_conditions": unmet,
            "exit_rules": strat.get("exit_conditions", {}),
            "sizing": strat.get("sizing", {}),
        })

    matched = [m for m in matches if m["matched"]]
    # Priority order: specific strategies first, paper-mode-any last (fallback)
    priority = {
        "cex-listing-play": 1,
        "whale-following": 2,
        "sentiment-divergence": 3,
        "meme-momentum": 4,
        "early-launch": 5,
        "paper-mode-any": 99,  # Fallback
    }
    matched.sort(key=lambda m: priority.get(m["strategy"], 50))
    return matched


def save_strategy_stats(name: str, trade_result: dict):
    """Update strategy performance after a trade closes."""
    stats = _load_json(STATE_DIR / "strategy_stats.json", {})

    if name not in stats:
        stats[name] = {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl_pct": 0, "active": True}

    s = stats[name]
    s["total_trades"] += 1
    pnl = trade_result.get("pnl_pct", 0)
    s["total_pnl_pct"] += pnl

    if pnl > 0:
        s["wins"] += 1
    else:
        s["losses"] += 1

    s["win_rate"] = round(s["wins"] / max(s["total_trades"], 1), 4)
    s["avg_pnl_pct"] = round(s["total_pnl_pct"] / max(s["total_trades"], 1), 2)
    s["last_trade_at"] = _now().isoformat()

    _save_json(STATE_DIR / "strategy_stats.json", stats)


if __name__ == "__main__":
    _log("=== STRATEGY REGISTRY TEST ===")

    print(f"\n  Total strategies: {len(STRATEGIES)}")
    print(f"  Active: {len(get_active_strategies())}")

    for name, strat in STRATEGIES.items():
        print(f"\n  {name}:")
        print(f"    {strat['description']}")
        print(f"    Chains: {strat['chain']}")
        print(f"    Entry conditions: {len(strat['entry_conditions'])}")
        print(f"    Exit: SL={strat['exit_conditions']['stop_loss_pct']}% TP={strat['exit_conditions']['take_profit_pct']}%")

    # Test signal matching
    test_signal = {
        "token": "BONK",
        "chain": "solana",
        "direction": "LONG",
        "volume_24h": 5000000,
        "price_change_24h": 25,
        "score": 75,
        "holder_count": 50000,
    }

    matches = match_signal_to_strategies(test_signal)
    print(f"\n  Test signal (BONK, vol=5M, +25%, score=75):")
    print(f"    Matched strategies: {[m['strategy'] for m in matches]}")

    _log("=== TEST COMPLETE ===")
