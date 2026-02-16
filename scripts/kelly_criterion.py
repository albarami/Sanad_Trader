#!/usr/bin/env python3
"""
Fractional Kelly Criterion — Sprint 5.6.1 through 5.6.4

Deterministic Python. No LLMs.

Calculates optimal position size based on:
K% = W - [(1-W) / R]
Position = K% × kelly_fraction (default 0.50 = half-Kelly)

5.6.1 — Kelly calculator
5.6.2 — Win rate + payoff ratio from strategy-evolution data
5.6.3 — Half-Kelly (configurable fraction)
5.6.4 — 30-trade minimum before activation

Reads:
- genius-memory/strategy-evolution/*.json (win rate, avg win/loss)
- config/thresholds.yaml (kelly_fraction, max_position_pct, cold_start_pct)
- state/trade_history.json (trade count per strategy)

Returns: position size as % of portfolio
"""

import json
import yaml
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STRATEGY_DIR = BASE_DIR / "genius-memory" / "strategy-evolution"
THRESHOLDS_PATH = BASE_DIR / "config" / "thresholds.yaml"
TRADE_HISTORY_PATH = BASE_DIR / "state" / "trade_history.json"

# Defaults if thresholds.yaml missing
DEFAULT_KELLY_FRACTION = 0.50       # Half-Kelly
DEFAULT_MAX_POSITION_PCT = 10.0     # Hard cap from Policy Engine
DEFAULT_COLD_START_PCT = 2.0        # Before 30 trades
MIN_TRADES_FOR_KELLY = 30           # Statistical confidence threshold
MIN_WIN_RATE = 0.10                 # Below 10% win rate → don't trade
MAX_KELLY_RAW = 0.25                # Cap raw Kelly at 25%


def _log(msg):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[KELLY] {ts} {msg}", flush=True)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _load_thresholds() -> dict:
    try:
        with open(THRESHOLDS_PATH) as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}


def _get_strategy_stats(strategy: str) -> dict | None:
    """Load strategy stats from genius-memory/strategy-evolution/."""
    # Try exact filename
    for suffix in [".json", ""]:
        path = STRATEGY_DIR / f"{strategy}{suffix}"
        if path.exists():
            return _load_json(path)

    # Try scanning directory
    if STRATEGY_DIR.exists():
        for f in STRATEGY_DIR.iterdir():
            if f.suffix == ".json":
                data = _load_json(f)
                if data and data.get("strategy") == strategy:
                    return data
    return None


def _get_trade_count(strategy: str) -> int:
    """Count completed trades for a strategy."""
    history = _load_json(TRADE_HISTORY_PATH, [])
    if isinstance(history, list):
        return sum(1 for t in history if t.get("strategy") == strategy)
    elif isinstance(history, dict):
        trades = history.get("trades", [])
        return sum(1 for t in trades if t.get("strategy") == strategy)
    return 0


# ─────────────────────────────────────────────────────────
# 5.6.1 — Kelly Calculator
# ─────────────────────────────────────────────────────────

def kelly_raw(win_rate: float, payoff_ratio: float) -> float:
    """Calculate raw Kelly percentage.

    K% = W - [(1-W) / R]

    win_rate: probability of winning (0-1)
    payoff_ratio: avg_win / avg_loss (>0)

    Returns: Kelly % (can be negative = don't bet)
    """
    if payoff_ratio <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0

    k = win_rate - ((1 - win_rate) / payoff_ratio)
    return k


def kelly_fractional(win_rate: float, payoff_ratio: float, fraction: float = 0.5) -> float:
    """Calculate fractional Kelly.

    Default: half-Kelly (fraction=0.50) for safety.
    """
    raw = kelly_raw(win_rate, payoff_ratio)
    if raw <= 0:
        return 0.0

    # Cap raw Kelly
    capped = min(raw, MAX_KELLY_RAW)
    return capped * fraction


# ─────────────────────────────────────────────────────────
# 5.6.2 + 5.6.3 + 5.6.4 — Full Position Sizing
# ─────────────────────────────────────────────────────────

def calculate_position_size(
    strategy: str,
    risk_reward_ratio: float = None,
    regime_modifier: float = 1.0,
) -> dict:
    """Calculate position size for a strategy.

    Returns dict with:
    - position_pct: recommended position size as % of portfolio
    - method: "kelly" or "cold_start"
    - details: breakdown of calculation
    """
    thresholds = _load_thresholds()
    kelly_fraction = thresholds.get("kelly_fraction", DEFAULT_KELLY_FRACTION)
    max_position_pct = thresholds.get("max_position_pct", DEFAULT_MAX_POSITION_PCT)
    cold_start_pct = thresholds.get("cold_start_default_pct", DEFAULT_COLD_START_PCT)

    # Get trade count (5.6.4)
    trade_count = _get_trade_count(strategy)

    # Cold start check
    if trade_count < MIN_TRADES_FOR_KELLY:
        position_pct = cold_start_pct * regime_modifier
        position_pct = min(position_pct, max_position_pct)
        return {
            "position_pct": round(position_pct, 2),
            "method": "cold_start",
            "trade_count": trade_count,
            "min_trades_needed": MIN_TRADES_FOR_KELLY,
            "trades_remaining": MIN_TRADES_FOR_KELLY - trade_count,
            "details": f"Cold start: {cold_start_pct}% default × {regime_modifier} regime modifier",
        }

    # Load strategy stats (5.6.2)
    stats = _get_strategy_stats(strategy)
    if not stats:
        _log(f"No stats for {strategy} — using cold start")
        return {
            "position_pct": round(min(cold_start_pct, max_position_pct), 2),
            "method": "cold_start",
            "trade_count": trade_count,
            "details": "No strategy stats found — using cold start default",
        }

    # Extract win rate and payoff ratio
    win_rate = stats.get("win_rate", stats.get("overall_win_rate", 0))
    if isinstance(win_rate, str):
        win_rate = float(win_rate.replace("%", "")) / 100
    elif win_rate > 1:
        win_rate = win_rate / 100  # Convert from percentage

    avg_win = abs(stats.get("avg_win_pct", stats.get("avg_win", 0)))
    avg_loss = abs(stats.get("avg_loss_pct", stats.get("avg_loss", 1)))

    # Use trade-specific R:R if provided, else from stats
    if risk_reward_ratio and risk_reward_ratio > 0:
        payoff_ratio = risk_reward_ratio
    elif avg_loss > 0:
        payoff_ratio = avg_win / avg_loss
    else:
        payoff_ratio = 2.0  # Default 2:1

    # Below minimum win rate → don't trade
    if win_rate < MIN_WIN_RATE:
        return {
            "position_pct": 0.0,
            "method": "kelly_reject",
            "win_rate": win_rate,
            "payoff_ratio": payoff_ratio,
            "kelly_raw": kelly_raw(win_rate, payoff_ratio),
            "details": f"Win rate {win_rate:.1%} below minimum {MIN_WIN_RATE:.1%}",
        }

    # Calculate Kelly (5.6.1 + 5.6.3)
    raw_k = kelly_raw(win_rate, payoff_ratio)
    fractional_k = kelly_fractional(win_rate, payoff_ratio, kelly_fraction)

    # Apply regime modifier
    position_pct = fractional_k * 100 * regime_modifier

    # Hard cap from Policy Engine
    position_pct = min(position_pct, max_position_pct)
    position_pct = max(position_pct, 0)

    return {
        "position_pct": round(position_pct, 2),
        "method": "kelly",
        "win_rate": round(win_rate, 4),
        "payoff_ratio": round(payoff_ratio, 2),
        "kelly_raw_pct": round(raw_k * 100, 2),
        "kelly_fraction": kelly_fraction,
        "kelly_adjusted_pct": round(fractional_k * 100, 2),
        "regime_modifier": regime_modifier,
        "max_cap_pct": max_position_pct,
        "trade_count": trade_count,
        "details": f"Kelly: {win_rate:.1%} WR, {payoff_ratio:.1f}:1 R:R → raw {raw_k*100:.1f}% × {kelly_fraction} = {fractional_k*100:.1f}% × {regime_modifier} regime",
    }


# ─────────────────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    _log("=== KELLY CRITERION TEST ===")

    # Test 1: Raw Kelly calculations
    print("\n  === Raw Kelly Tests ===")
    tests = [
        (0.55, 2.0, "55% WR, 2:1 R:R (good edge)"),
        (0.45, 2.5, "45% WR, 2.5:1 R:R (moderate)"),
        (0.40, 1.5, "40% WR, 1.5:1 R:R (marginal)"),
        (0.30, 2.0, "30% WR, 2:1 R:R (poor)"),
        (0.60, 3.0, "60% WR, 3:1 R:R (strong)"),
    ]

    for wr, rr, desc in tests:
        raw = kelly_raw(wr, rr) * 100
        half = kelly_fractional(wr, rr, 0.5) * 100
        print(f"    {desc}: Raw={raw:.1f}%, Half-Kelly={half:.1f}%")

    # Test 2: Full position sizing (will use cold start since no trades yet)
    print("\n  === Position Sizing Tests ===")
    strategies = ["meme-momentum", "early-launch", "sentiment-divergence"]

    for strat in strategies:
        result = calculate_position_size(strat, risk_reward_ratio=2.5, regime_modifier=0.8)
        print(f"    {strat}: {result['position_pct']}% ({result['method']})")
        print(f"      {result['details']}")

    _log("=== TEST COMPLETE ===")
