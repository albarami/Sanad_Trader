#!/usr/bin/env python3
"""
Thompson Sampler — Sprint 6.6
Deterministic Python. No LLMs.

Implements Thompson Sampling for strategy selection in the Sanad pipeline.
When a signal arrives, the system must decide which strategy to evaluate it against.
Thompson Sampling balances exploration (trying underused strategies) with
exploitation (favoring proven strategies).

Algorithm:
For each eligible strategy:
  1. Maintain Beta(alpha, beta) distribution where:
     - alpha = 1 + wins
     - beta = 1 + losses
  2. Sample a random value from each strategy's Beta distribution
  3. Select the strategy with the highest sampled value
  4. After trade closes, update alpha (win) or beta (loss)

This naturally balances exploration/exploitation:
- New strategies (alpha=1, beta=1) get explored due to high variance
- Proven strategies (many wins) get selected more often
- Failing strategies (many losses) get selected less often

After 30 days in LIVE mode, switches to pure exploitation (highest expected value).

Used by:
- sanad_pipeline.py: strategy selection stage
- Can also rank strategies for a given signal

State stored in: state/thompson_state.json
"""

import json
import os
import sys
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"
STRATEGIES_DIR = BASE_DIR / "strategies"
THOMPSON_STATE = STATE_DIR / "thompson_state.json"
CONFIG_DIR = BASE_DIR / "config"

# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────
EXPLORATION_DAYS = 30         # Days before switching to exploitation
MIN_TRADES_FOR_EXPLOITATION = 50  # Minimum total trades across all strategies
REGIME_PENALTY = 0.3          # Penalty multiplier for regime-mismatched strategies


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[THOMPSON] {ts} {msg}", flush=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
def _load_json(path: Path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json_atomic(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        _log(f"ERROR saving {path}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────
# Strategy Registry
# ─────────────────────────────────────────────────────────
# Known strategies and their regime affinities
STRATEGY_REGISTRY = {
    "meme-momentum": {
        "preferred_regimes": ["BULL_NORMAL_VOL", "BULL_LOW_VOL", "BULL_HIGH_VOL"],
        "neutral_regimes": ["SIDEWAYS_NORMAL_VOL", "SIDEWAYS_LOW_VOL"],
        "avoid_regimes": ["BEAR_HIGH_VOL", "BEAR_NORMAL_VOL", "BEAR_LOW_VOL"],
        "signal_types": ["trending", "volume_spike", "social_momentum", "meme_radar"],
        "max_signal_age_min": 30,
    },
    "early-launch": {
        "preferred_regimes": ["BULL_NORMAL_VOL", "BULL_LOW_VOL", "SIDEWAYS_NORMAL_VOL"],
        "neutral_regimes": ["BULL_HIGH_VOL", "SIDEWAYS_LOW_VOL"],
        "avoid_regimes": ["BEAR_HIGH_VOL", "BEAR_NORMAL_VOL", "BEAR_LOW_VOL", "SIDEWAYS_HIGH_VOL"],
        "signal_types": ["new_launch", "pumpfun", "dex_new_pool"],
        "max_signal_age_min": 10,
    },
    "whale-following": {
        "preferred_regimes": ["BULL_NORMAL_VOL", "BULL_LOW_VOL", "SIDEWAYS_NORMAL_VOL"],
        "neutral_regimes": ["BULL_HIGH_VOL", "SIDEWAYS_LOW_VOL", "SIDEWAYS_HIGH_VOL"],
        "avoid_regimes": ["BEAR_HIGH_VOL"],
        "signal_types": ["whale_accumulation", "large_transfer", "exchange_flow"],
        "max_signal_age_min": 60,
    },
    "sentiment-divergence": {
        "preferred_regimes": ["BEAR_LOW_VOL", "SIDEWAYS_NORMAL_VOL", "SIDEWAYS_LOW_VOL"],
        "neutral_regimes": ["BEAR_NORMAL_VOL", "BULL_LOW_VOL"],
        "avoid_regimes": ["BULL_HIGH_VOL"],  # Divergence doesn't apply in euphoria
        "signal_types": ["sentiment_divergence", "fear_greed_extreme", "contrarian"],
        "max_signal_age_min": 60,
    },
    "cex-listing-play": {
        "preferred_regimes": ["BULL_NORMAL_VOL", "BULL_LOW_VOL", "SIDEWAYS_NORMAL_VOL"],
        "neutral_regimes": ["BULL_HIGH_VOL", "SIDEWAYS_LOW_VOL", "SIDEWAYS_HIGH_VOL",
                            "BEAR_LOW_VOL", "BEAR_NORMAL_VOL"],
        "avoid_regimes": [],  # Listings work in any regime (event-driven)
        "signal_types": ["cex_listing", "listing_announcement", "listing_rumor"],
        "max_signal_age_min": 120,
    },
}


# ─────────────────────────────────────────────────────────
# Thompson State Management
# ─────────────────────────────────────────────────────────
def _load_state() -> dict:
    """Load Thompson Sampling state."""
    state = _load_json(THOMPSON_STATE, None)
    if state:
        return state

    # Initialize fresh state
    state = {
        "mode": "thompson",  # "thompson" or "exploitation"
        "first_trade_at": None,
        "total_trades": 0,
        "strategies": {},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    # Initialize each strategy with Beta(1, 1) — uniform prior
    for name in STRATEGY_REGISTRY:
        state["strategies"][name] = {
            "alpha": 1,       # 1 + wins
            "beta": 1,        # 1 + losses
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_pct": 0.0,
            "last_selected_at": None,
            "last_trade_at": None,
            "status": "PAPER",
        }

    _save_json_atomic(THOMPSON_STATE, state)
    return state


def _check_mode_transition(state: dict) -> str:
    """Check if we should switch from thompson to exploitation."""
    if state["mode"] == "exploitation":
        return "exploitation"

    first_trade = state.get("first_trade_at")
    if not first_trade:
        return "thompson"

    try:
        first_dt = datetime.fromisoformat(first_trade)
        days_active = (_now() - first_dt).total_seconds() / 86400
        total_trades = state.get("total_trades", 0)

        if days_active >= EXPLORATION_DAYS and total_trades >= MIN_TRADES_FOR_EXPLOITATION:
            _log(f"Mode transition: thompson → exploitation "
                 f"(days={days_active:.0f}, trades={total_trades})")
            state["mode"] = "exploitation"
            return "exploitation"
    except (ValueError, TypeError):
        pass

    return "thompson"


# ─────────────────────────────────────────────────────────
# Selection Algorithm
# ─────────────────────────────────────────────────────────
def select_strategy(
    signal: dict = None,
    current_regime: str = "UNKNOWN",
    seed: int = None,
) -> dict:
    """Select the best strategy for a given signal using Thompson Sampling.

    Args:
        signal: Signal dict (optional, used for type matching)
        current_regime: Current market regime tag (e.g. "BEAR_HIGH_VOL")
        seed: Random seed for reproducibility in testing

    Returns:
        {
            "selected": "strategy-name",
            "scores": {"strategy-name": score, ...},
            "mode": "thompson" | "exploitation",
            "eligible": ["strategy-name", ...],
            "excluded": {"strategy-name": "reason", ...},
        }
    """
    if seed is not None:
        random.seed(seed)

    state = _load_state()
    mode = _check_mode_transition(state)

    signal_type = signal.get("source", "") if signal else ""
    signal_age_min = 0
    if signal and signal.get("timestamp"):
        try:
            sig_dt = datetime.fromisoformat(signal["timestamp"])
            signal_age_min = (_now() - sig_dt).total_seconds() / 60
        except (ValueError, TypeError):
            pass

    eligible = {}
    excluded = {}

    for name, registry in STRATEGY_REGISTRY.items():
        strat_state = state["strategies"].get(name, {"alpha": 1, "beta": 1})

        # Check strategy status
        if strat_state.get("status") == "RETIRED":
            excluded[name] = "RETIRED"
            continue

        # Check signal age
        if signal_age_min > registry["max_signal_age_min"]:
            excluded[name] = f"signal_too_old ({signal_age_min:.0f}min > {registry['max_signal_age_min']}min)"
            continue

        # Check regime avoidance
        if current_regime in registry.get("avoid_regimes", []):
            excluded[name] = f"regime_avoided ({current_regime})"
            continue

        # Strategy is eligible — calculate score
        alpha = strat_state.get("alpha", 1)
        beta_param = strat_state.get("beta", 1)

        if mode == "thompson":
            # Sample from Beta distribution
            sample = random.betavariate(alpha, beta_param)
        else:
            # Exploitation: use expected value (mean of Beta)
            sample = alpha / (alpha + beta_param)

        # Regime affinity bonus/penalty
        if current_regime in registry.get("preferred_regimes", []):
            sample *= 1.15  # 15% bonus
        elif current_regime in registry.get("neutral_regimes", []):
            pass  # No modifier
        else:
            sample *= (1 - REGIME_PENALTY)  # 30% penalty for unknown regimes

        # Signal type match bonus
        if signal_type and signal_type in registry.get("signal_types", []):
            sample *= 1.20  # 20% bonus for matching signal type

        eligible[name] = round(sample, 6)

    if not eligible:
        _log("WARNING: No eligible strategies — all excluded")
        return {
            "selected": None,
            "scores": {},
            "mode": mode,
            "eligible": [],
            "excluded": excluded,
        }

    # Select highest scoring strategy
    selected = max(eligible, key=eligible.get)

    # Update state
    state["strategies"].setdefault(selected, {"alpha": 1, "beta": 1})
    state["strategies"][selected]["last_selected_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    _save_json_atomic(THOMPSON_STATE, state)

    _log(f"Selected: {selected} (score={eligible[selected]:.4f}, mode={mode})")
    for name, score in sorted(eligible.items(), key=lambda x: x[1], reverse=True):
        _log(f"  {name}: {score:.4f}")
    if excluded:
        for name, reason in excluded.items():
            _log(f"  EXCLUDED {name}: {reason}")

    return {
        "selected": selected,
        "scores": eligible,
        "mode": mode,
        "eligible": list(eligible.keys()),
        "excluded": excluded,
    }


def rank_strategies(
    current_regime: str = "UNKNOWN",
    signal: dict = None,
) -> list[dict]:
    """Rank all strategies by expected value (no randomness).
    Useful for displaying in console/reports.
    """
    state = _load_state()
    rankings = []

    for name, registry in STRATEGY_REGISTRY.items():
        strat_state = state["strategies"].get(name, {"alpha": 1, "beta": 1})
        alpha = strat_state.get("alpha", 1)
        beta_param = strat_state.get("beta", 1)
        expected = alpha / (alpha + beta_param)
        trades = strat_state.get("trades", 0)
        wins = strat_state.get("wins", 0)
        win_rate = wins / trades if trades > 0 else 0

        regime_fit = "preferred" if current_regime in registry.get("preferred_regimes", []) \
            else "neutral" if current_regime in registry.get("neutral_regimes", []) \
            else "avoid" if current_regime in registry.get("avoid_regimes", []) \
            else "unknown"

        rankings.append({
            "strategy": name,
            "expected_value": round(expected, 4),
            "alpha": alpha,
            "beta": beta_param,
            "trades": trades,
            "wins": wins,
            "win_rate": round(win_rate, 4),
            "regime_fit": regime_fit,
            "status": strat_state.get("status", "PAPER"),
        })

    rankings.sort(key=lambda r: r["expected_value"], reverse=True)
    return rankings


# ─────────────────────────────────────────────────────────
# Outcome Recording
# ─────────────────────────────────────────────────────────
def record_outcome(strategy_name: str, is_win: bool, pnl_pct: float = 0.0):
    """Record a trade outcome and update Beta parameters.
    Called by post_trade_analyzer.py after trade close.
    """
    state = _load_state()

    if strategy_name not in state["strategies"]:
        state["strategies"][strategy_name] = {
            "alpha": 1,
            "beta": 1,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_pct": 0.0,
            "last_selected_at": None,
            "last_trade_at": None,
            "status": "PAPER",
        }

    strat = state["strategies"][strategy_name]

    if is_win:
        strat["alpha"] += 1
        strat["wins"] += 1
    else:
        strat["beta"] += 1
        strat["losses"] += 1

    strat["trades"] += 1
    strat["total_pnl_pct"] = round(strat["total_pnl_pct"] + pnl_pct, 6)
    strat["last_trade_at"] = _now_iso()

    # Update totals
    state["total_trades"] = sum(
        s.get("trades", 0) for s in state["strategies"].values()
    )
    if not state["first_trade_at"]:
        state["first_trade_at"] = _now_iso()

    state["updated_at"] = _now_iso()
    _save_json_atomic(THOMPSON_STATE, state)

    expected = strat["alpha"] / (strat["alpha"] + strat["beta"])
    _log(
        f"Outcome: {strategy_name} {'WIN' if is_win else 'LOSS'} "
        f"({pnl_pct:+.2%}) | "
        f"Alpha={strat['alpha']} Beta={strat['beta']} "
        f"E[v]={expected:.3f} "
        f"({strat['wins']}/{strat['trades']})"
    )


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Try to get current regime
    try:
        from regime_classifier import get_regime_tag
        regime = get_regime_tag()
    except ImportError:
        regime = "UNKNOWN"

    if len(sys.argv) > 1 and sys.argv[1] == "select":
        _log(f"=== STRATEGY SELECTION (regime={regime}) ===")
        result = select_strategy(current_regime=regime)
        print(f"\n  Selected: {result['selected']}")
        print(f"Mode: {result['mode']}")
        print(f"\n  Scores:")
        for name, score in sorted(result["scores"].items(), key=lambda x: x[1], reverse=True):
            print(f"    {name}: {score:.4f}")
        if result["excluded"]:
            print(f"\n  Excluded:")
            for name, reason in result["excluded"].items():
                print(f"    {name}: {reason}")

    elif len(sys.argv) > 1 and sys.argv[1] == "rank":
        rankings = rank_strategies(current_regime=regime)
        print(f"\n{'Strategy':<25} {'E[v]':>6} {'Trades':>7} {'WR':>6} {'Regime':>10} {'Status':>8}")
        print("-" * 70)
        for r in rankings:
            wr = f"{r['win_rate']:.0%}" if r["trades"] > 0 else "N/A"
            print(f"{r['strategy']:<25} {r['expected_value']:>6.3f} "
                  f"{r['trades']:>7} {wr:>6} {r['regime_fit']:>10} {r['status']:>8}")

    else:
        _log(f"=== THOMPSON SAMPLER STATUS (regime={regime}) ===")
        state = _load_state()
        print(f"\n  Mode: {state['mode']}")
        print(f"Total trades: {state['total_trades']}")
        print(f"\n{'Strategy':<25} {'Alpha':>6} {'Beta':>6} {'E[v]':>6} {'W/L':>8}")
        print("-" * 55)
        for name, s in state["strategies"].items():
            ev = s["alpha"] / (s["alpha"] + s["beta"])
            wl = f"{s.get('wins', 0)}/{s.get('losses', 0)}"
            print(f"{name:<25} {s['alpha']:>6} {s['beta']:>6} {ev:>6.3f} {wl:>8}")
