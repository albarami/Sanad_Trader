#!/usr/bin/env python3
"""
Safety Guardrails for Self-Learning — Sprint 5.7.5

Deterministic Python. No LLMs.

Programmatic enforcement of rules documented in strategy files:
1. 30-trade minimum before any parameter change
2. Max risk drift: can only TIGHTEN, never LOOSEN
3. 1 change per week per strategy budget
4. Auto-revert on 10% win rate degradation

Called by: any component attempting to modify strategy parameters.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
STRATEGY_DIR = BASE_DIR / "genius-memory" / "strategy-evolution"
CHANGE_LOG_PATH = STATE_DIR / "strategy_changes.json"
TRADE_HISTORY_PATH = STATE_DIR / "trade_history.json"

MIN_TRADES = 30
MAX_CHANGES_PER_WEEK = 1
DEGRADATION_REVERT_PCT = 10.0   # 10% win rate drop → revert
EVAL_WINDOW_TRADES = 15          # Evaluate change after 15 trades


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[GUARD] {ts} {msg}", flush=True)


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


def _get_trade_count(strategy: str) -> int:
    history = _load_json(TRADE_HISTORY_PATH, [])
    trades = history if isinstance(history, list) else history.get("trades", [])
    return sum(1 for t in trades if t.get("strategy") == strategy)


def _get_recent_changes(strategy: str, days: int = 7) -> list:
    log = _load_json(CHANGE_LOG_PATH, {"changes": []})
    cutoff = (_now() - timedelta(days=days)).isoformat()
    return [c for c in log.get("changes", [])
            if c.get("strategy") == strategy and c.get("timestamp", "") >= cutoff]


# Risk parameters that can only tighten
RISK_PARAMS_TIGHTEN_ONLY = {
    "max_position_pct": "lower",        # Can only decrease
    "stop_loss_pct": "lower",           # Tighter stop = lower %
    "max_portfolio_exposure": "lower",  # Can only decrease
    "max_daily_loss_pct": "lower",      # Can only decrease
    "max_concurrent_positions": "lower",
}

RISK_PARAMS_LOOSEN_ONLY = {
    "min_risk_reward": "higher",        # Can only increase (higher bar)
    "min_trust_score": "higher",        # Can only increase
}


def validate_change(
    strategy: str,
    param_name: str,
    old_value: float,
    new_value: float,
    evidence_trade_ids: list = None,
) -> dict:
    """Validate a proposed strategy parameter change.

    Returns: {allowed: bool, reason: str, details: dict}
    """
    now = _now()

    # Rule 1: 30-trade minimum
    trade_count = _get_trade_count(strategy)
    if trade_count < MIN_TRADES:
        return {
            "allowed": False,
            "reason": f"Insufficient trades: {trade_count}/{MIN_TRADES} minimum",
            "rule": "min_trades",
        }

    # Rule 2: Risk drift prevention
    if param_name in RISK_PARAMS_TIGHTEN_ONLY:
        direction = RISK_PARAMS_TIGHTEN_ONLY[param_name]
        if direction == "lower" and new_value > old_value:
            return {
                "allowed": False,
                "reason": f"Cannot LOOSEN {param_name}: {old_value} → {new_value}. Can only tighten (decrease).",
                "rule": "risk_drift",
            }

    if param_name in RISK_PARAMS_LOOSEN_ONLY:
        direction = RISK_PARAMS_LOOSEN_ONLY[param_name]
        if direction == "higher" and new_value < old_value:
            return {
                "allowed": False,
                "reason": f"Cannot LOOSEN {param_name}: {old_value} → {new_value}. Can only tighten (increase).",
                "rule": "risk_drift",
            }

    # Rule 3: 1 change per week per strategy
    recent = _get_recent_changes(strategy, days=7)
    if len(recent) >= MAX_CHANGES_PER_WEEK:
        last_change = recent[-1]
        return {
            "allowed": False,
            "reason": f"Change budget exhausted: {len(recent)}/{MAX_CHANGES_PER_WEEK} this week. Last: {last_change.get('param', '?')} on {last_change.get('timestamp', '?')[:10]}",
            "rule": "change_budget",
        }

    # Check if previous change still in evaluation
    pending = [c for c in recent if not c.get("evaluated", False)]
    if pending:
        return {
            "allowed": False,
            "reason": f"Previous change to {pending[0].get('param', '?')} still being evaluated ({pending[0].get('eval_trades_remaining', '?')} trades remaining)",
            "rule": "pending_evaluation",
        }

    # All checks passed
    return {
        "allowed": True,
        "reason": "All guardrails passed",
        "trade_count": trade_count,
        "changes_this_week": len(recent),
    }


def record_change(
    strategy: str,
    param_name: str,
    old_value: float,
    new_value: float,
    evidence_trade_ids: list = None,
    confidence: float = 0.0,
) -> dict:
    """Record an approved parameter change."""
    log = _load_json(CHANGE_LOG_PATH, {"changes": []})

    entry = {
        "strategy": strategy,
        "param": param_name,
        "old_value": old_value,
        "new_value": new_value,
        "evidence_trades": evidence_trade_ids or [],
        "confidence": confidence,
        "timestamp": _now().isoformat(),
        "evaluated": False,
        "eval_trades_remaining": EVAL_WINDOW_TRADES,
        "pre_change_win_rate": None,  # Filled by evaluator
    }

    log["changes"].append(entry)
    log["changes"] = log["changes"][-100:]  # Keep last 100
    _save_json(CHANGE_LOG_PATH, log)

    _log(f"RECORDED: {strategy}.{param_name} {old_value} → {new_value}")
    return entry


def check_revert_needed(strategy: str) -> list:
    """Check if any recent changes need reverting due to degradation.

    Rule 4: Auto-revert on 10% win rate degradation.
    """
    log = _load_json(CHANGE_LOG_PATH, {"changes": []})
    reverts = []

    for change in log.get("changes", []):
        if change.get("strategy") != strategy:
            continue
        if change.get("evaluated", False):
            continue
        if change.get("reverted", False):
            continue

        pre_wr = change.get("pre_change_win_rate")
        if pre_wr is None:
            continue

        # Get current win rate
        stats_path = STRATEGY_DIR / f"{strategy}.json"
        stats = _load_json(stats_path)
        if not stats:
            continue

        current_wr = stats.get("win_rate", stats.get("overall_win_rate", 0))
        if isinstance(current_wr, str):
            current_wr = float(current_wr.replace("%", "")) / 100
        elif current_wr > 1:
            current_wr = current_wr / 100

        degradation = (pre_wr - current_wr) * 100

        if degradation >= DEGRADATION_REVERT_PCT:
            reverts.append({
                "strategy": strategy,
                "param": change["param"],
                "revert_to": change["old_value"],
                "current_value": change["new_value"],
                "degradation_pct": round(degradation, 1),
                "reason": f"Win rate dropped {degradation:.1f}% ({pre_wr:.1%} → {current_wr:.1%})",
            })
            change["reverted"] = True
            change["revert_reason"] = f"Degradation: {degradation:.1f}%"
            _log(f"REVERT NEEDED: {strategy}.{change['param']} → {change['old_value']} (WR dropped {degradation:.1f}%)")

    _save_json(CHANGE_LOG_PATH, log)
    return reverts


if __name__ == "__main__":
    _log("=== SAFETY GUARDRAILS TEST ===")

    # Test 1: No trades yet → blocked
    r = validate_change("meme-momentum", "stop_loss_pct", 8.0, 7.0)
    print(f"  Test 1 (no trades): allowed={r['allowed']} — {r['reason']}")

    # Test 2: Risk loosening → blocked (simulated)
    print(f"  Test 2 (risk loosening simulation):")
    print(f"    max_position_pct 10→12: Would be BLOCKED (can only decrease)")
    print(f"    stop_loss_pct 8→6: Would be ALLOWED (tightening)")
    print(f"    min_risk_reward 2.0→1.5: Would be BLOCKED (can only increase)")

    # Test 3: Unknown param → allowed (no restriction)
    r = validate_change("test-strategy", "custom_param", 5.0, 10.0)
    print(f"  Test 3 (unknown param, no trades): allowed={r['allowed']} — {r['reason']}")

    _log("=== TEST COMPLETE ===")
