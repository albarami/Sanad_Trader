#!/usr/bin/env python3
"""
Post-Trade Analyzer — Sprint 5.1
Deterministic Python core. No LLMs for data, LLM only for pattern notes.

Runs after every closed trade. Responsibilities:
1. Classify trade as win/loss
2. Log detailed post-mortem to genius-memory/wins/ or losses/
3. Update UCB1 source scores (via ucb1_scorer.py)
4. Tag trade with market regime (via regime_classifier.py)
5. Update master-stats.md rolling metrics
6. Update strategy-evolution/ tracker

Triggered by:
- position_monitor.py: after closing a position
- Can also run standalone: python3 post_trade_analyzer.py <trade_id>

All math is Python. Statistics are deterministic. No LLM estimation.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
POSITIONS_PATH = STATE_DIR / "positions.json"
PORTFOLIO_PATH = STATE_DIR / "portfolio.json"
TRADE_HISTORY_PATH = STATE_DIR / "trade_history.json"

MEMORY_DIR = BASE_DIR / "genius-memory"
WINS_DIR = MEMORY_DIR / "wins"
LOSSES_DIR = MEMORY_DIR / "losses"
STRATEGY_DIR = MEMORY_DIR / "strategy-evolution"
MASTER_STATS = MEMORY_DIR / "master-stats.md"

sys.path.insert(0, str(SCRIPT_DIR))

# Import sibling modules
try:
    from ucb1_scorer import record_trade_outcome, get_all_scores
except ImportError:
    def record_trade_outcome(*args, **kwargs):
        _log("WARNING: ucb1_scorer not available — skipping source update")

    def get_all_scores():
        return {}

try:
    from regime_classifier import get_current_regime
except ImportError:
    def get_current_regime():
        _log("WARNING: regime_classifier not available — using UNKNOWN")
        return {"regime_tag": "UNKNOWN", "primary": "UNKNOWN", "volatility": "UNKNOWN"}


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[POST_TRADE] {ts} {msg}", flush=True)


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
# Trade Finding
# ─────────────────────────────────────────────────────────
def _find_closed_trade(trade_id: str = None) -> dict | None:
    """Find a closed trade by ID. If no ID, find most recent unanalyzed trade."""
    positions = _load_json(POSITIONS_PATH, {"positions": []})
    pos_list = positions.get("positions", [])

    if trade_id:
        for p in pos_list:
            if p.get("id") == trade_id and p.get("status") == "CLOSED":
                return p
        _log(f"Trade {trade_id} not found or not CLOSED")
        return None

    # Find most recent CLOSED trade that hasn't been analyzed
    closed = [
        p for p in pos_list
        if p.get("status") == "CLOSED" and not p.get("post_analysis_done")
    ]
    if not closed:
        return None

    # Sort by exit time, most recent first
    closed.sort(
        key=lambda p: p.get("closed_at", p.get("exit_time", "")),
        reverse=True,
    )
    return closed[0]


def _mark_analyzed(trade_id: str):
    """Mark a trade as analyzed in positions.json."""
    positions = _load_json(POSITIONS_PATH, {"positions": []})
    for p in positions.get("positions", []):
        if p.get("id") == trade_id:
            p["post_analysis_done"] = True
            p["analyzed_at"] = _now_iso()
            break
    _save_json_atomic(POSITIONS_PATH, positions)


# ─────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────
def _calc_hold_duration(trade: dict) -> float:
    """Calculate hold duration in hours."""
    try:
        opened = datetime.fromisoformat(trade.get("opened_at", ""))
        closed = datetime.fromisoformat(
            trade.get("closed_at", trade.get("exit_time", ""))
        )
        return round((closed - opened).total_seconds() / 3600, 2)
    except (ValueError, TypeError):
        return 0.0


def _calc_max_adverse_excursion(trade: dict) -> float:
    """Calculate MAE — maximum adverse price move during the trade.
    Approximated from entry vs stop-loss hit.
    """
    entry = trade.get("entry_price", 0)
    exit_price = trade.get("exit_price", entry)
    if entry <= 0:
        return 0.0

    # If exit was at a loss, the adverse excursion is at least the loss
    pnl_pct = trade.get("pnl_pct", 0)
    if pnl_pct < 0:
        return round(abs(pnl_pct), 6)
    return 0.0


def _calc_max_favorable_excursion(trade: dict) -> float:
    """Calculate MFE — maximum favorable price move during the trade.
    Use high-water mark if available, otherwise approximate from TP level.
    """
    entry = trade.get("entry_price", 0)
    if entry <= 0:
        return 0.0

    # Check for high-water mark from trailing stop data
    hwm = trade.get("high_water_mark", 0)
    if hwm > entry:
        return round((hwm - entry) / entry, 6)

    # If trade was a win, MFE is at least the gain
    pnl_pct = trade.get("pnl_pct", 0)
    if pnl_pct > 0:
        return round(pnl_pct, 6)
    return 0.0


def _assess_exit_quality(trade: dict) -> dict:
    """Assess how well the exit was timed."""
    exit_reason = trade.get("exit_reason", "UNKNOWN")
    pnl_pct = trade.get("pnl_pct", 0)
    mfe = _calc_max_favorable_excursion(trade)

    quality = "NEUTRAL"
    notes = []

    if exit_reason == "TAKE_PROFIT":
        quality = "GOOD"
        notes.append("Exited at target — trade plan executed")
    elif exit_reason == "TRAILING_STOP":
        if mfe > 0 and pnl_pct > 0:
            captured = pnl_pct / mfe if mfe > 0 else 0
            quality = "GOOD" if captured > 0.6 else "FAIR"
            notes.append(f"Captured {captured:.0%} of max favorable excursion")
        else:
            quality = "POOR"
            notes.append("Trailing stop hit but trade was negative")
    elif exit_reason == "STOP_LOSS":
        quality = "EXPECTED"
        notes.append("Stop-loss executed as planned — risk management working")
    elif exit_reason == "TIME_BASED":
        if pnl_pct > 0:
            quality = "FAIR"
            notes.append("Time exit with profit — could have used trailing stop")
        else:
            quality = "POOR"
            notes.append("Time exit at a loss — thesis didn't play out in timeframe")
    elif exit_reason == "VOLUME_DEATH":
        quality = "GOOD"
        notes.append("Volume exit prevented potential illiquidity trap")
    elif exit_reason == "FLASH_CRASH":
        quality = "EMERGENCY"
        notes.append("Flash crash protection activated")

    return {
        "quality": quality,
        "exit_reason": exit_reason,
        "notes": notes,
    }


def analyze_trade(trade: dict) -> dict:
    """Run full post-trade analysis on a closed trade."""
    now = _now()
    trade_id = trade.get("id", "UNKNOWN")
    token = trade.get("token", "UNKNOWN")

    _log(f"=== ANALYZING TRADE: {trade_id} ({token}) ===")

    # Basic metrics
    entry = trade.get("entry_price", 0)
    exit_price = trade.get("exit_price", 0)
    pnl_usd = trade.get("pnl_usd", 0)
    pnl_pct = trade.get("pnl_pct", 0)
    is_win = pnl_usd > 0
    hold_hours = _calc_hold_duration(trade)
    source = trade.get("source", trade.get("signal_source", "unknown"))
    strategy = trade.get("strategy_name", "unknown")
    exit_reason = trade.get("exit_reason", "UNKNOWN")

    _log(f"  {'WIN' if is_win else 'LOSS'}: {pnl_pct:+.2%} (${pnl_usd:+.2f})")
    _log(f"  Entry: ${entry:,.6f} → Exit: ${exit_price:,.6f}")
    _log(f"  Hold: {hold_hours:.1f}h | Exit: {exit_reason} | Strategy: {strategy}")

    # Get current market regime
    regime = get_current_regime()
    regime_tag = regime.get("regime_tag", "UNKNOWN")
    _log(f"  Regime at exit: {regime_tag}")

    # Exit quality assessment
    exit_quality = _assess_exit_quality(trade)
    _log(f"  Exit quality: {exit_quality['quality']}")

    # Excursion analysis
    mae = _calc_max_adverse_excursion(trade)
    mfe = _calc_max_favorable_excursion(trade)

    # Build analysis record
    analysis = {
        "trade_id": trade_id,
        "token": token,
        "timestamp": now.isoformat(),
        "outcome": "WIN" if is_win else "LOSS",
        "metrics": {
            "entry_price": entry,
            "exit_price": exit_price,
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 6),
            "hold_duration_hours": hold_hours,
            "max_adverse_excursion": mae,
            "max_favorable_excursion": mfe,
        },
        "exit": exit_quality,
        "regime": {
            "tag": regime_tag,
            "primary": regime.get("primary", "UNKNOWN"),
            "volatility": regime.get("volatility", "UNKNOWN"),
        },
        "trade_details": {
            "source": source,
            "strategy": strategy,
            "sanad_score": trade.get("sanad_score"),
            "risk_reward_ratio": trade.get("risk_reward_ratio"),
            "bull_stop_loss": trade.get("bull_stop_loss"),
            "bull_target_price": trade.get("bull_target_price"),
            "stop_loss_pct": trade.get("stop_loss_pct"),
            "take_profit_pct": trade.get("take_profit_pct"),
            "position_usd": trade.get("position_usd"),
        },
    }

    # ── Save to wins/ or losses/ ──
    target_dir = WINS_DIR if is_win else LOSSES_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{now.strftime('%Y%m%d_%H%M')}_{token}_{trade_id[:8]}.json"
    _save_json_atomic(target_dir / filename, analysis)
    _log(f"  Saved to {'wins' if is_win else 'losses'}/{filename}")

    # ── Index in Vector DB for RAG retrieval ──
    try:
        from vector_db import index_trade
        index_trade(analysis, trade_id=trade_id)
        _log(f"  Vector DB: indexed trade {trade_id}")
    except Exception as e:
        _log(f"  WARNING: Vector DB indexing failed: {e}")

    # ── Update UCB1 source score ──
    record_trade_outcome(
        source_name=source,
        is_win=is_win,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        token=token,
        trade_id=trade_id,
    )

    # ── Update Thompson Sampling (adaptive strategy selection) ──
    try:
        from thompson_sampler import record_outcome as thompson_record_outcome
        thompson_record_outcome(
            strategy_name=strategy,
            is_win=is_win,
            pnl_pct=pnl_pct,
        )
        _log(f"  Thompson updated: {strategy} {'WIN' if is_win else 'LOSS'}")
    except Exception as e:
        _log(f"  WARNING: Thompson update failed: {e}")

    # ── Update strategy tracker ──
    _update_strategy_tracker(strategy, is_win, pnl_pct, pnl_usd, regime_tag)

    # ── Update master stats ──
    _update_master_stats()

    # ── Mark trade as analyzed ──
    _mark_analyzed(trade_id)

    _log(f"=== ANALYSIS COMPLETE: {trade_id} ===")
    return analysis


# ─────────────────────────────────────────────────────────
# Strategy Tracker
# ─────────────────────────────────────────────────────────
def _update_strategy_tracker(
    strategy: str, is_win: bool, pnl_pct: float, pnl_usd: float, regime_tag: str
):
    """Update strategy-evolution tracker."""
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = strategy.replace(" ", "_").replace("/", "_")
    path = STRATEGY_DIR / f"{safe_name}.json"

    data = _load_json(path, {
        "strategy_name": strategy,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_usd": 0.0,
        "total_pnl_pct": 0.0,
        "win_rate": 0.0,
        "avg_pnl_pct": 0.0,
        "best_trade_pct": 0.0,
        "worst_trade_pct": 0.0,
        "by_regime": {},
        "recent_trades": [],
        "parameter_changes": [],
        "created_at": _now_iso(),
    })

    data["total_trades"] += 1
    if is_win:
        data["wins"] += 1
    else:
        data["losses"] += 1

    data["total_pnl_usd"] = round(data["total_pnl_usd"] + pnl_usd, 4)
    data["total_pnl_pct"] = round(data["total_pnl_pct"] + pnl_pct, 6)

    if data["total_trades"] > 0:
        data["win_rate"] = round(data["wins"] / data["total_trades"], 4)
        data["avg_pnl_pct"] = round(data["total_pnl_pct"] / data["total_trades"], 6)

    data["best_trade_pct"] = max(data["best_trade_pct"], pnl_pct)
    data["worst_trade_pct"] = min(data["worst_trade_pct"], pnl_pct)

    # Track by regime
    if regime_tag not in data["by_regime"]:
        data["by_regime"][regime_tag] = {"trades": 0, "wins": 0, "pnl_pct": 0.0}
    data["by_regime"][regime_tag]["trades"] += 1
    if is_win:
        data["by_regime"][regime_tag]["wins"] += 1
    data["by_regime"][regime_tag]["pnl_pct"] = round(
        data["by_regime"][regime_tag]["pnl_pct"] + pnl_pct, 6
    )

    # Recent trades (last 20)
    data["recent_trades"].append({
        "timestamp": _now_iso(),
        "pnl_pct": round(pnl_pct, 6),
        "is_win": is_win,
        "regime": regime_tag,
    })
    data["recent_trades"] = data["recent_trades"][-20:]

    data["updated_at"] = _now_iso()

    _save_json_atomic(path, data)
    _log(f"  Strategy '{strategy}': {data['wins']}/{data['total_trades']} "
         f"({data['win_rate']:.0%}) avg={data['avg_pnl_pct']:+.2%}")


# ─────────────────────────────────────────────────────────
# Master Stats Update
# ─────────────────────────────────────────────────────────
def _update_master_stats():
    """Regenerate master-stats.md from all trade data."""
    positions = _load_json(POSITIONS_PATH, {"positions": []})
    all_trades = [p for p in positions.get("positions", []) if p.get("status") == "CLOSED"]

    if not all_trades:
        return

    now = _now()
    wins = [t for t in all_trades if t.get("pnl_usd", 0) > 0]
    losses = [t for t in all_trades if t.get("pnl_usd", 0) <= 0]
    total_pnl = sum(t.get("pnl_usd", 0) for t in all_trades)
    pnl_list = [t.get("pnl_pct", 0) for t in all_trades]

    # Win/Loss stats
    avg_win = (
        sum(t.get("pnl_pct", 0) for t in wins) / len(wins) if wins else 0
    )
    avg_loss = (
        sum(t.get("pnl_pct", 0) for t in losses) / len(losses) if losses else 0
    )
    best = max(pnl_list) if pnl_list else 0
    worst = min(pnl_list) if pnl_list else 0

    # Profit factor
    gross_profit = sum(t.get("pnl_usd", 0) for t in wins)
    gross_loss = abs(sum(t.get("pnl_usd", 0) for t in losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    # Sharpe ratio (simplified: mean/std of returns)
    if len(pnl_list) >= 30:
        mean_r = sum(pnl_list) / len(pnl_list)
        var_r = sum((r - mean_r) ** 2 for r in pnl_list) / len(pnl_list)
        std_r = math.sqrt(var_r) if var_r > 0 else 0
        sharpe = round(mean_r / std_r, 2) if std_r > 0 else 0
    else:
        sharpe = None

    # Max drawdown from portfolio
    portfolio = _load_json(PORTFOLIO_PATH, {})
    max_dd = portfolio.get("current_drawdown_pct", 0)

    # Rolling periods
    def rolling_stats(trades, days):
        cutoff = now - timedelta(days=days)
        recent = [
            t for t in trades
            if _parse_dt(t.get("closed_at", t.get("exit_time", "")))
            and _parse_dt(t.get("closed_at", t.get("exit_time", ""))) >= cutoff
        ]
        if not recent:
            return {"trades": 0, "win_rate": "N/A", "pnl": 0}
        w = len([t for t in recent if t.get("pnl_usd", 0) > 0])
        pnl = sum(t.get("pnl_usd", 0) for t in recent)
        return {
            "trades": len(recent),
            "win_rate": f"{w/len(recent):.0%}",
            "pnl": round(pnl, 2),
        }

    r7 = rolling_stats(all_trades, 7)
    r30 = rolling_stats(all_trades, 30)

    # By strategy
    strat_stats = {}
    for t in all_trades:
        s = t.get("strategy_name", "unknown")
        if s not in strat_stats:
            strat_stats[s] = {"trades": 0, "wins": 0, "pnl": 0}
        strat_stats[s]["trades"] += 1
        if t.get("pnl_usd", 0) > 0:
            strat_stats[s]["wins"] += 1
        strat_stats[s]["pnl"] += t.get("pnl_usd", 0)

    # By source
    source_scores = get_all_scores()

    # By regime — load from strategy files
    regime_stats = {}
    for t in all_trades:
        # Try to get regime from the analysis file
        regime = t.get("regime_at_exit", t.get("regime_tag", "UNKNOWN"))
        if regime not in regime_stats:
            regime_stats[regime] = {"trades": 0, "wins": 0, "pnl": 0}
        regime_stats[regime]["trades"] += 1
        if t.get("pnl_usd", 0) > 0:
            regime_stats[regime]["wins"] += 1
        regime_stats[regime]["pnl"] += t.get("pnl_usd", 0)

    # ── Write master-stats.md ──
    win_rate = f"{len(wins)/len(all_trades):.0%}" if all_trades else "N/A"

    md = f"""# Sanad Trader v3.0 — Master Performance Stats

> Auto-updated by post_trade_analyzer.py after every closed trade.
> Manual edits will be overwritten.

Last updated: {now.strftime('%Y-%m-%d %H:%M UTC')}

## Lifetime Stats
- Total Trades: {len(all_trades)}
- Wins: {len(wins)} | Losses: {len(losses)}
- Win Rate: {win_rate}
- Total P&L (USD): ${total_pnl:,.2f}
- Average Win: {avg_win:+.2%}
- Average Loss: {avg_loss:+.2%}
- Largest Win: {best:+.2%}
- Largest Loss: {worst:+.2%}
- Profit Factor: {profit_factor}
- Sharpe Ratio: {sharpe if sharpe is not None else 'N/A (need 30+ trades)'}
- Max Drawdown: {max_dd*100:.2f}%

## Rolling 7-Day
- Trades: {r7['trades']}
- Win Rate: {r7['win_rate']}
- P&L: ${r7['pnl']:,.2f}

## Rolling 30-Day
- Trades: {r30['trades']}
- Win Rate: {r30['win_rate']}
- P&L: ${r30['pnl']:,.2f}

## By Strategy
| Strategy | Trades | Win Rate | Avg P&L | Total P&L |
|----------|--------|----------|---------|-----------|\n"""

    for s, d in sorted(strat_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = f"{d['wins']/d['trades']:.0%}" if d["trades"] > 0 else "N/A"
        avg = f"{d['pnl']/d['trades']:+.2f}" if d["trades"] > 0 else "N/A"
        md += f"| {s} | {d['trades']} | {wr} | ${avg} | ${d['pnl']:,.2f} |\n"

    if not strat_stats:
        md += "| (no trades yet) | | | | |\n"

    md += """
## By Source
| Source | Signals | Trades | Win Rate | UCB1 Score |
|--------|---------|--------|----------|------------|\n"""

    for name, info in sorted(source_scores.items(), key=lambda x: x[1]["score"], reverse=True):
        wr = f"{info['win_rate']:.0%}" if info["trades_executed"] > 0 else "N/A"
        md += (
            f"| {name} | {info['total_signals']} | {info['trades_executed']} "
            f"| {wr} | {info['score']:.1f} ({info['grade']}) |\n"
        )

    if not source_scores:
        md += "| (no trades yet) | | | | |\n"

    md += """
## Regime Performance
| Regime | Trades | Win Rate | Avg P&L |
|--------|--------|----------|---------|
"""

    for regime, d in sorted(regime_stats.items()):
        wr = f"{d['wins']/d['trades']:.0%}" if d["trades"] > 0 else "N/A"
        avg = f"${d['pnl']/d['trades']:+.2f}" if d["trades"] > 0 else "N/A"
        md += f"| {regime} | {d['trades']} | {wr} | {avg} |\n"

    if not regime_stats:
        md += "| (no trades yet) | | | |\n"

    MASTER_STATS.parent.mkdir(parents=True, exist_ok=True)
    MASTER_STATS.write_text(md)
    _log(f"  Master stats updated ({len(all_trades)} trades)")


def _parse_dt(iso_str: str):
    """Safely parse ISO datetime."""
    try:
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────
# Batch: Analyze All Unanalyzed
# ─────────────────────────────────────────────────────────
def analyze_all_pending():
    """Find and analyze all closed trades that haven't been analyzed yet."""
    positions = _load_json(POSITIONS_PATH, {"positions": []})
    pending = [
        p for p in positions.get("positions", [])
        if p.get("status") == "CLOSED" and not p.get("post_analysis_done")
    ]

    if not pending:
        _log("No pending trades to analyze")
        return []

    _log(f"Found {len(pending)} unanalyzed closed trades")
    results = []
    for trade in pending:
        try:
            result = analyze_trade(trade)
            results.append(result)
        except Exception as e:
            _log(f"ERROR analyzing trade {trade.get('id')}: {e}")
            import traceback
            traceback.print_exc()

    return results


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "all":
        _log("=== ANALYZING ALL PENDING TRADES ===")
        results = analyze_all_pending()
        _log(f"Analyzed {len(results)} trades")
    elif len(sys.argv) > 1:
        trade_id = sys.argv[1]
        _log(f"=== ANALYZING TRADE: {trade_id} ===")
        trade = _find_closed_trade(trade_id)
        if trade:
            analyze_trade(trade)
        else:
            _log(f"Trade {trade_id} not found or not closed")
            sys.exit(1)
    else:
        _log("=== ANALYZING MOST RECENT UNANALYZED TRADE ===")
        trade = _find_closed_trade()
        if trade:
            analyze_trade(trade)
        else:
            _log("No unanalyzed closed trades found")
