#!/usr/bin/env python3
"""
EXIT QUALITY ANALYZER — Find Money Leaks
Analyzes closed trades to identify:
- Max Favorable Excursion (MFE): how high did price go before exit?
- Max Adverse Excursion (MAE): how low did price go before exit?
- Optimal exit timing vs actual exit
- Stop-loss/take-profit calibration

Goal: Discover if stops are too tight, profits taken too early, or hold duration wrong.
This identifies WHY avg loser > avg winner (negative expectancy).
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# --- CONFIG ---
BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
GENIUS_DIR = BASE_DIR / "genius-memory"
EXIT_ANALYSIS_DIR = GENIUS_DIR / "exit-analysis"

TRADE_HISTORY = STATE_DIR / "trade_history.json"
PRICE_CACHE = STATE_DIR / "price_cache.json"

# --- HELPERS ---
def _log(msg):
    ts = datetime.utcnow().isoformat() + "Z"
    print(f"[EXIT-ANALYSIS] {ts} {msg}")


def _load_trades():
    """Load trade history."""
    if not TRADE_HISTORY.exists():
        return []
    
    data = json.load(open(TRADE_HISTORY))
    if isinstance(data, dict):
        return data.get("trades", [])
    return data


def _load_price_history(token, start_time, end_time):
    """
    Load price history for token between start and end time.
    In real implementation, this would query price_cache.json or a database.
    For now, return mock data structure.
    """
    # TODO: Implement actual price history loading from logs/cache
    # For MVP, we can only work with entry/exit prices from trade history
    return None


def analyze_exit(trade):
    """
    Analyze a single trade's exit quality.
    Calculate MFE, MAE, optimal exit vs actual.
    """
    token = trade.get("token", "UNKNOWN")
    entry_price = trade.get("entry_price", 0)
    exit_price = trade.get("exit_price", 0)
    pnl_pct = trade.get("pnl_pct", 0)
    exit_reason = trade.get("exit_reason", "unknown")
    
    if not entry_price or not exit_price:
        return None
    
    # Calculate basic metrics
    price_change_pct = ((exit_price - entry_price) / entry_price) * 100
    
    # Classify exit quality
    is_win = pnl_pct > 0
    exit_type = None
    
    if exit_reason == "STOP_LOSS":
        exit_type = "stop_loss"
    elif exit_reason == "TAKE_PROFIT":
        exit_type = "take_profit"
    elif exit_reason == "TIME_LIMIT":
        exit_type = "time_exit"
    elif exit_reason == "MANUAL":
        exit_type = "manual"
    else:
        exit_type = "other"
    
    analysis = {
        "token": token,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "price_change_pct": price_change_pct,
        "exit_reason": exit_reason,
        "exit_type": exit_type,
        "is_win": is_win,
        "hold_duration_hours": trade.get("hold_duration_hours", 0),
        
        # TODO: These require price history data
        "mfe_pct": None,  # Max favorable excursion
        "mae_pct": None,  # Max adverse excursion
        "optimal_exit_pct": None,  # Best exit point
        "exit_efficiency": None,  # actual_pnl / optimal_pnl
        
        "analyzed_at": datetime.utcnow().isoformat() + "Z"
    }
    
    # Qualitative assessment
    insights = []
    
    if exit_type == "stop_loss":
        if abs(pnl_pct) < 0.05:  # Lost <5%
            insights.append("Stop hit early — possibly too tight")
        elif abs(pnl_pct) > 0.15:  # Lost >15%
            insights.append("Stop hit late — possibly too wide")
        else:
            insights.append("Stop loss triggered at reasonable level")
    
    elif exit_type == "take_profit":
        if pnl_pct < 0.10:  # Won <10%
            insights.append("Take-profit hit early — consider trailing stops")
        elif pnl_pct > 0.20:  # Won >20%
            insights.append("Large winner — take-profit level well-calibrated")
        else:
            insights.append("Take-profit triggered at target level")
    
    analysis["insights"] = insights
    
    return analysis


def aggregate_exit_patterns(analyses):
    """
    Aggregate exit analyses to find systemic patterns.
    """
    if not analyses:
        return {}
    
    # Group by exit type
    by_exit_type = defaultdict(list)
    for a in analyses:
        by_exit_type[a["exit_type"]].append(a)
    
    # Stats
    all_wins = [a for a in analyses if a["is_win"]]
    all_losses = [a for a in analyses if not a["is_win"]]
    
    stop_loss_exits = by_exit_type.get("stop_loss", [])
    take_profit_exits = by_exit_type.get("take_profit", [])
    
    avg_win_pct = sum(a["pnl_pct"] for a in all_wins) / len(all_wins) if all_wins else 0
    avg_loss_pct = sum(a["pnl_pct"] for a in all_losses) / len(all_losses) if all_losses else 0
    
    avg_win_hold = sum(a["hold_duration_hours"] for a in all_wins) / len(all_wins) if all_wins else 0
    avg_loss_hold = sum(a["hold_duration_hours"] for a in all_losses) / len(all_losses) if all_losses else 0
    
    patterns = {
        "total_analyzed": len(analyses),
        "wins": len(all_wins),
        "losses": len(all_losses),
        "win_rate": len(all_wins) / len(analyses) if analyses else 0,
        
        "avg_win_pct": round(avg_win_pct * 100, 2),
        "avg_loss_pct": round(avg_loss_pct * 100, 2),
        "expectancy": round((avg_win_pct + avg_loss_pct) * 100, 2),
        
        "avg_win_hold_hours": round(avg_win_hold, 1),
        "avg_loss_hold_hours": round(avg_loss_hold, 1),
        
        "exits_by_type": {
            "stop_loss": len(stop_loss_exits),
            "take_profit": len(take_profit_exits),
            "time_exit": len(by_exit_type.get("time_exit", [])),
            "manual": len(by_exit_type.get("manual", [])),
            "other": len(by_exit_type.get("other", []))
        },
        
        "insights": [],
        "recommendations": []
    }
    
    # Generate insights
    if patterns["expectancy"] < 0:
        patterns["insights"].append(f"⚠️ NEGATIVE EXPECTANCY: Avg loser ({patterns['avg_loss_pct']:.1f}%) larger than avg winner ({patterns['avg_win_pct']:.1f}%)")
    else:
        patterns["insights"].append(f"✅ POSITIVE EXPECTANCY: {patterns['expectancy']:.2f}%")
    
    # Stop-loss analysis
    if len(stop_loss_exits) >= 3:
        avg_sl_loss = sum(a["pnl_pct"] for a in stop_loss_exits) / len(stop_loss_exits)
        patterns["avg_stop_loss_pct"] = round(avg_sl_loss * 100, 2)
        
        if abs(avg_sl_loss) > 0.12:  # Average stop loss > 12%
            patterns["recommendations"].append(f"TIGHTEN STOPS: Avg stop-loss hit at {abs(avg_sl_loss)*100:.1f}% — consider 8-10% stops")
        elif abs(avg_sl_loss) < 0.05:  # Average stop loss < 5%
            patterns["recommendations"].append(f"WIDEN STOPS: Avg stop-loss hit at {abs(avg_sl_loss)*100:.1f}% — may be too tight, consider 7-10%")
    
    # Take-profit analysis
    if len(take_profit_exits) >= 3:
        avg_tp_win = sum(a["pnl_pct"] for a in take_profit_exits) / len(take_profit_exits)
        patterns["avg_take_profit_pct"] = round(avg_tp_win * 100, 2)
        
        if avg_tp_win < 0.08:  # Average take-profit < 8%
            patterns["recommendations"].append(f"RAISE TAKE-PROFITS: Avg TP hit at {avg_tp_win*100:.1f}% — consider 12-15% targets or trailing stops")
    
    # Hold duration analysis
    if avg_win_hold > 0 and avg_loss_hold > 0:
        if avg_loss_hold > avg_win_hold * 1.5:
            patterns["recommendations"].append(f"CUT LOSSES FASTER: Losers held {avg_loss_hold:.1f}h vs winners {avg_win_hold:.1f}h")
    
    return patterns


def run():
    """Main exit analyzer."""
    _log("=== EXIT QUALITY ANALYZER START ===")
    
    trades = _load_trades()
    closed_trades = [t for t in trades if t.get("exit_time")]
    
    if len(closed_trades) < 5:
        _log(f"Not enough closed trades for analysis ({len(closed_trades)}/5)")
        return
    
    _log(f"Analyzing {len(closed_trades)} closed trade(s)")
    
    # Analyze each exit
    analyses = []
    for trade in closed_trades:
        try:
            analysis = analyze_exit(trade)
            if analysis:
                analyses.append(analysis)
        except Exception as e:
            _log(f"Analysis failed for {trade.get('token')}: {e}")
    
    if not analyses:
        _log("No analyses generated")
        return
    
    # Aggregate patterns
    patterns = aggregate_exit_patterns(analyses)
    
    # Save results
    EXIT_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Save individual analyses
    for analysis in analyses:
        filename = f"{analysis['token']}_{int(datetime.fromisoformat(analysis['analyzed_at'].replace('Z', '+00:00')).timestamp())}.json"
        filepath = EXIT_ANALYSIS_DIR / filename
        
        with open(filepath, "w") as f:
            json.dump(analysis, f, indent=2)
    
    # Save aggregate patterns
    patterns_file = EXIT_ANALYSIS_DIR / "exit_patterns_summary.json"
    with open(patterns_file, "w") as f:
        json.dump(patterns, f, indent=2)
    
    _log(f"Exit patterns summary:")
    _log(f"  Win rate: {patterns['win_rate']:.1%}")
    _log(f"  Avg winner: {patterns['avg_win_pct']:.2f}%")
    _log(f"  Avg loser: {patterns['avg_loss_pct']:.2f}%")
    _log(f"  Expectancy: {patterns['expectancy']:.2f}%")
    
    if patterns["insights"]:
        _log("  Insights:")
        for insight in patterns["insights"]:
            _log(f"    • {insight}")
    
    if patterns["recommendations"]:
        _log("  Recommendations:")
        for rec in patterns["recommendations"]:
            _log(f"    • {rec}")
        
        # Alert on critical recommendations
        try:
            sys.path.insert(0, str(BASE_DIR / "scripts"))
            from notifier import send
            
            msg = "📉 EXIT ANALYSIS RECOMMENDATIONS:\n" + "\n".join(f"• {r}" for r in patterns["recommendations"][:3])
            send(msg, level=2)
        except Exception:
            pass
    
    _log("=== EXIT QUALITY ANALYZER COMPLETE ===")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        _log("Interrupted")
    except Exception as e:
        _log(f"ANALYZER CRASHED: {e}")
        import traceback
        traceback.print_exc()
