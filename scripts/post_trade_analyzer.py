#!/usr/bin/env python3
"""
POST-TRADE ANALYZER — Genius Memory Engine Core
Runs after every closed trade. Extracts patterns, updates source grades, learns.
This is how the system gets smarter over time.
"""

import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# --- CONFIG ---
BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
GENIUS_DIR = BASE_DIR / "genius-memory"
GENIUS_WINS = GENIUS_DIR / "wins"
GENIUS_LOSSES = GENIUS_DIR / "losses"
GENIUS_PATTERNS = GENIUS_DIR / "patterns.json"

TRADE_HISTORY = STATE_DIR / "trade_history.json"
UCB1_STATE = STATE_DIR / "ucb1_source_grades.json"
THOMPSON_STATE = STATE_DIR / "thompson_state.json"

# --- HELPERS ---
def _log(msg):
    ts = datetime.utcnow().isoformat() + "Z"
    print(f"[POST-TRADE] {ts} {msg}")


def _load_trades():
    """Load trade history."""
    if not TRADE_HISTORY.exists():
        return []
    data = json.load(open(TRADE_HISTORY))
    # Handle both formats: list or {"trades": [...]}
    if isinstance(data, dict):
        return data.get("trades", [])
    return data


def _save_pattern(pattern):
    """Save extracted pattern."""
    GENIUS_DIR.mkdir(parents=True, exist_ok=True)
    
    patterns = []
    if GENIUS_PATTERNS.exists():
        try:
            patterns = json.load(open(GENIUS_PATTERNS))
        except:
            patterns = []
    
    patterns.append({
        **pattern,
        "extracted_at": datetime.utcnow().isoformat() + "Z"
    })
    
    # Keep last 100 patterns
    patterns = patterns[-100:]
    
    with open(GENIUS_PATTERNS, "w") as f:
        json.dump(patterns, f, indent=2)


def _update_ucb1_score(source, win):
    """Update UCB1 source grade based on trade outcome."""
    UCB1_STATE.parent.mkdir(parents=True, exist_ok=True)
    
    ucb1 = {}
    if UCB1_STATE.exists():
        try:
            ucb1 = json.load(open(UCB1_STATE))
        except:
            ucb1 = {}
    
    if source not in ucb1:
        ucb1[source] = {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.5,
            "confidence": 0.5,
            "grade": "C",
            "last_updated": None
        }
    
    entry = ucb1[source]
    entry["total"] += 1
    
    if win:
        entry["wins"] += 1
    else:
        entry["losses"] += 1
    
    entry["win_rate"] = entry["wins"] / entry["total"] if entry["total"] > 0 else 0.5
    entry["last_updated"] = datetime.utcnow().isoformat() + "Z"
    
    # Assign grade
    wr = entry["win_rate"]
    if wr >= 0.7:
        entry["grade"] = "A"
    elif wr >= 0.6:
        entry["grade"] = "B"
    elif wr >= 0.5:
        entry["grade"] = "C"
    else:
        entry["grade"] = "D"
    
    # Confidence = proportion of trials (higher total = higher confidence)
    # UCB1 formula: win_rate + sqrt(2 * ln(total_all_sources) / total_this_source)
    total_all = sum(s["total"] for s in ucb1.values())
    if entry["total"] > 0 and total_all > 0:
        import math
        exploration = math.sqrt(2 * math.log(total_all) / entry["total"])
        entry["confidence"] = min(1.0, entry["win_rate"] + exploration)
    else:
        entry["confidence"] = 0.5
    
    with open(UCB1_STATE, "w") as f:
        json.dump(ucb1, f, indent=2)
    
    _log(f"UCB1 updated: {source} → {entry['grade']} (WR={wr:.1%}, n={entry['total']})")


def _update_thompson_state(strategy, win):
    """
    Update Thompson Sampling (Beta distribution) for a strategy based on trade outcome.
    
    Alpha = wins + 1 (prior)
    Beta = losses + 1 (prior)
    """
    THOMPSON_STATE.parent.mkdir(parents=True, exist_ok=True)
    
    thompson = {}
    if THOMPSON_STATE.exists():
        try:
            thompson = json.load(open(THOMPSON_STATE))
        except:
            thompson = {}
    
    # Initialize if needed
    if "strategies" not in thompson:
        thompson["strategies"] = {}
    
    if strategy not in thompson["strategies"]:
        thompson["strategies"][strategy] = {
            "alpha": 1,  # Prior
            "beta": 1,   # Prior
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_pct": 0.0,
            "last_trade_at": None,
            "status": "PAPER"
        }
    
    strat = thompson["strategies"][strategy]
    strat["trades"] += 1
    
    if win:
        strat["wins"] += 1
        strat["alpha"] += 1
    else:
        strat["losses"] += 1
        strat["beta"] += 1
    
    strat["last_trade_at"] = datetime.utcnow().isoformat() + "Z"
    
    # Update totals
    if "total_trades" not in thompson:
        thompson["total_trades"] = 0
    thompson["total_trades"] += 1
    
    if "first_trade_at" not in thompson:
        thompson["first_trade_at"] = datetime.utcnow().isoformat() + "Z"
    
    # Atomic save
    tmp = THOMPSON_STATE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(thompson, f, indent=2)
        os.replace(tmp, THOMPSON_STATE)
    except Exception as e:
        _log(f"ERROR saving Thompson state: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except:
            pass
    
    _log(f"Thompson updated: {strategy} → alpha={strat['alpha']}, beta={strat['beta']} (WR={strat['wins']}/{strat['trades']})")


def analyze_trade(trade):
    """Analyze a single closed trade."""
    _log(f"Analyzing: {trade.get('token')} (exit: {trade.get('exit_reason')})")
    
    # Extract fields
    token = trade.get("token", "UNKNOWN")
    entry_price = trade.get("entry_price", 0)
    exit_price = trade.get("exit_price", 0)
    pnl_pct = trade.get("pnl_pct", 0)
    strategy = trade.get("strategy", "unknown")
    raw_source = trade.get("source", "unknown")
    trust_score = trade.get("trust_score", 0)
    corroboration = trade.get("cross_source_count", 1)
    exit_reason = trade.get("exit_reason", "unknown")
    
    # Canonicalize source for UCB1 learning
    try:
        from signal_normalizer import canonical_source
        source_info = canonical_source(raw_source)
        source_key = source_info["source_key"]
    except Exception as e:
        _log(f"Warning: canonical_source failed: {e}, using raw source")
        source_key = raw_source
    
    is_win = pnl_pct > 0
    
    # Update UCB1 source grade with canonical key
    _update_ucb1_score(source_key, is_win)
    
    # Update Thompson Sampling state for strategy
    if strategy and strategy != "unknown":
        _update_thompson_state(strategy, is_win)
    
    # Save to genius memory
    category = "wins" if is_win else "losses"
    target_dir = GENIUS_WINS if is_win else GENIUS_LOSSES
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Create detailed analysis file
    analysis = {
        "token": token,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "strategy": strategy,
        "source": source_key,  # Use canonical source key
        "source_raw": raw_source,  # Keep raw for reference
        "trust_score": trust_score,
        "corroboration": corroboration,
        "exit_reason": exit_reason,
        "entry_time": trade.get("entry_time"),
        "exit_time": trade.get("exit_time"),
        "hold_duration_hours": trade.get("hold_duration_hours", 0),
        "analyzed_at": datetime.utcnow().isoformat() + "Z"
    }
    
    filename = f"{token}_{int(time.time())}.json"
    filepath = target_dir / filename
    
    with open(filepath, "w") as f:
        json.dump(analysis, f, indent=2)
    
    _log(f"Saved to {category}/{filename}")
    
    return analysis


def extract_patterns(recent_trades, window=20):
    """Extract patterns from recent trades (last N trades)."""
    if len(recent_trades) < 10:
        _log(f"Not enough trades for pattern extraction ({len(recent_trades)}/10)")
        return
    
    recent = recent_trades[-window:]
    
    wins = [t for t in recent if t.get("pnl_pct", 0) > 0]
    losses = [t for t in recent if t.get("pnl_pct", 0) <= 0]
    
    win_rate = len(wins) / len(recent) if recent else 0
    
    # Group by strategy
    by_strategy = defaultdict(list)
    for t in recent:
        by_strategy[t.get("strategy", "unknown")].append(t)
    
    # Group by source
    by_source = defaultdict(list)
    for t in recent:
        by_source[t.get("source", "unknown")].append(t)
    
    # Group by corroboration level
    by_corr = defaultdict(list)
    for t in recent:
        corr = t.get("cross_source_count", 1)
        if corr == 1:
            level = "AHAD"
        elif corr == 2:
            level = "MASHHUR"
        elif corr == 3:
            level = "TAWATUR"
        else:
            level = "TAWATUR_QAWIY"
        by_corr[level].append(t)
    
    pattern = {
        "window_size": len(recent),
        "win_rate": round(win_rate, 3),
        "wins": len(wins),
        "losses": len(losses),
        "by_strategy": {},
        "by_source": {},
        "by_corroboration": {},
        "insights": []
    }
    
    # Strategy performance
    for strat, trades in by_strategy.items():
        strat_wins = [t for t in trades if t.get("pnl_pct", 0) > 0]
        pattern["by_strategy"][strat] = {
            "count": len(trades),
            "win_rate": round(len(strat_wins) / len(trades), 3) if trades else 0
        }
    
    # Source performance
    for src, trades in by_source.items():
        src_wins = [t for t in trades if t.get("pnl_pct", 0) > 0]
        pattern["by_source"][src] = {
            "count": len(trades),
            "win_rate": round(len(src_wins) / len(trades), 3) if trades else 0
        }
    
    # Corroboration performance
    for level, trades in by_corr.items():
        corr_wins = [t for t in trades if t.get("pnl_pct", 0) > 0]
        pattern["by_corroboration"][level] = {
            "count": len(trades),
            "win_rate": round(len(corr_wins) / len(trades), 3) if trades else 0
        }
    
    # Generate insights
    # 1. Best performing strategy
    best_strat = max(pattern["by_strategy"].items(), key=lambda x: x[1]["win_rate"]) if pattern["by_strategy"] else None
    if best_strat and best_strat[1]["count"] >= 3:
        pattern["insights"].append(f"Best strategy: {best_strat[0]} (WR={best_strat[1]['win_rate']:.1%}, n={best_strat[1]['count']})")
    
    # 2. Best source
    best_src = max(pattern["by_source"].items(), key=lambda x: x[1]["win_rate"]) if pattern["by_source"] else None
    if best_src and best_src[1]["count"] >= 3:
        pattern["insights"].append(f"Best source: {best_src[0]} (WR={best_src[1]['win_rate']:.1%}, n={best_src[1]['count']})")
    
    # 3. Corroboration value
    if "TAWATUR" in pattern["by_corroboration"] and "AHAD" in pattern["by_corroboration"]:
        tawatur_wr = pattern["by_corroboration"]["TAWATUR"]["win_rate"]
        ahad_wr = pattern["by_corroboration"]["AHAD"]["win_rate"]
        if tawatur_wr > ahad_wr + 0.1:
            pattern["insights"].append(f"Corroboration works: TAWATUR {tawatur_wr:.1%} vs AHAD {ahad_wr:.1%}")
    
    _save_pattern(pattern)
    _log(f"Pattern extracted: WR={win_rate:.1%}, {len(pattern['insights'])} insights")


def run():
    """Main analyzer."""
    _log("=== POST-TRADE ANALYZER START ===")
    
    trades = _load_trades()
    
    if not trades:
        _log("No trades found")
        return
    
    _log(f"Loaded {len(trades)} trade(s)")
    
    # Find unanalyzed closed trades
    analyzed_file = STATE_DIR / "analyzed_trades.json"
    analyzed_ids = set()
    
    if analyzed_file.exists():
        try:
            analyzed_ids = set(json.load(open(analyzed_file)))
        except:
            pass
    
    # Normalize trade format (handle both old and new formats)
    closed_trades = []
    for t in trades:
        # Old format has "timestamp" (exit time), new format has "exit_time"
        if t.get("exit_time") or (t.get("timestamp") and t.get("side") == "SELL"):
            # Generate unique ID from timestamp+token if missing
            if not t.get("trade_id"):
                ts = t.get("timestamp") or t.get("exit_time")
                t["trade_id"] = f"{t.get('token')}_{ts}"
            # Normalize field names
            if "timestamp" in t and "exit_time" not in t:
                t["exit_time"] = t["timestamp"]
            if "reason" in t and "exit_reason" not in t:
                t["exit_reason"] = t["reason"]
            closed_trades.append(t)
    
    new_closed = [t for t in closed_trades if t.get("trade_id") not in analyzed_ids]
    
    if not new_closed:
        _log("No new closed trades to analyze")
        return
    
    _log(f"Analyzing {len(new_closed)} new closed trade(s)")
    
    for trade in new_closed:
        try:
            analyze_trade(trade)
            analyzed_ids.add(trade.get("trade_id"))
        except Exception as e:
            _log(f"Analysis failed for {trade.get('token')}: {e}")
            import traceback
            traceback.print_exc()
    
    # Save analyzed IDs
    with open(analyzed_file, "w") as f:
        json.dump(list(analyzed_ids), f)
    
    # Extract patterns from recent trades
    if len(closed_trades) >= 10:
        try:
            extract_patterns(closed_trades)
        except Exception as e:
            _log(f"Pattern extraction failed: {e}")
            import traceback
            traceback.print_exc()
    
    _log("=== POST-TRADE ANALYZER COMPLETE ===")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        _log("Interrupted")
    except Exception as e:
        _log(f"ANALYZER CRASHED: {e}")
        import traceback
        traceback.print_exc()
