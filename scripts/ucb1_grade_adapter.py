#!/usr/bin/env python3
"""
UCB1 Grade Adapter — Automatic Source Trust Upgrading/Downgrading
Reads perf_by_source.json from post-trade analyzer.
Auto-adjusts source grades (A/B/C/D) based on win rate after 10+ trades.
Feeds back into Sanad trust score calculation.
"""

import os
import json
from pathlib import Path
from datetime import datetime

# --- CONFIG ---
BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
GENIUS_DIR = BASE_DIR / "genius-memory"

UCB1_STATE = STATE_DIR / "ucb1_source_grades.json"
SOURCE_ACCURACY_DIR = GENIUS_DIR / "source-accuracy"
SOURCE_GRADES_FILE = STATE_DIR / "source_grades.json"

# Grade thresholds
GRADE_A_MIN_WR = 0.70  # 70%+ win rate
GRADE_B_MIN_WR = 0.60
GRADE_C_MIN_WR = 0.50
MIN_TRADES_FOR_UPGRADE = 10  # Need 10+ trades before auto-adjusting

# --- HELPERS ---
def _log(msg):
    ts = datetime.utcnow().isoformat() + "Z"
    print(f"[UCB1-ADAPTER] {ts} {msg}")


def load_ucb1_data():
    """Load UCB1 performance data."""
    if not UCB1_STATE.exists():
        return {}
    
    try:
        return json.load(open(UCB1_STATE))
    except Exception as e:
        _log(f"Failed to load UCB1 data: {e}")
        return {}


def load_current_grades():
    """Load current source grades."""
    if not SOURCE_GRADES_FILE.exists():
        # Initialize with default grades
        return {
            "coingecko": "C",
            "birdeye": "B",
            "dexscreener": "B",
            "telegram": "C",
            "sentiment": "C",
            "onchain": "B",
            "solscan": "B"
        }
    
    try:
        return json.load(open(SOURCE_GRADES_FILE))
    except Exception as e:
        _log(f"Failed to load grades: {e}")
        return {}


def save_grades(grades):
    """Save updated source grades."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(SOURCE_GRADES_FILE, "w") as f:
        json.dump(grades, f, indent=2)
    
    _log("Grades saved")


def log_grade_change(source, old_grade, new_grade, reason, stats):
    """Log grade change to genius memory."""
    SOURCE_ACCURACY_DIR.mkdir(parents=True, exist_ok=True)
    
    change_log = SOURCE_ACCURACY_DIR / "grade_changes.json"
    
    changes = []
    if change_log.exists():
        try:
            changes = json.load(open(change_log))
        except:
            changes = []
    
    changes.append({
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source": source,
        "old_grade": old_grade,
        "new_grade": new_grade,
        "reason": reason,
        "stats": stats
    })
    
    # Keep last 200 changes
    changes = changes[-200:]
    
    with open(change_log, "w") as f:
        json.dump(changes, f, indent=2)


def adjust_grades(ucb1_data, current_grades):
    """Adjust grades based on UCB1 performance."""
    changes = []
    
    for source, stats in ucb1_data.items():
        total = stats.get("total", 0)
        win_rate = stats.get("win_rate", 0.5)
        current_grade = current_grades.get(source, "C")
        
        # Need minimum trades to adjust
        if total < MIN_TRADES_FOR_UPGRADE:
            continue
        
        # Determine target grade based on win rate
        if win_rate >= GRADE_A_MIN_WR:
            target_grade = "A"
        elif win_rate >= GRADE_B_MIN_WR:
            target_grade = "B"
        elif win_rate >= GRADE_C_MIN_WR:
            target_grade = "C"
        else:
            target_grade = "D"
        
        # Check if upgrade or downgrade needed
        if target_grade != current_grade:
            old_grade = current_grade
            current_grades[source] = target_grade
            
            reason = f"Win rate {win_rate:.1%} after {total} trades"
            
            log_grade_change(source, old_grade, target_grade, reason, {
                "total_trades": total,
                "win_rate": win_rate,
                "wins": stats.get("wins", 0),
                "losses": stats.get("losses", 0)
            })
            
            changes.append({
                "source": source,
                "old": old_grade,
                "new": target_grade,
                "wr": win_rate,
                "n": total
            })
            
            _log(f"GRADE CHANGE: {source} {old_grade}→{target_grade} (WR={win_rate:.1%}, n={total})")
    
    return changes


def run():
    """Main adapter."""
    _log("=== UCB1 GRADE ADAPTER START ===")
    
    # Load UCB1 performance data
    ucb1_data = load_ucb1_data()
    
    if not ucb1_data:
        _log("No UCB1 data found")
        return
    
    _log(f"Loaded UCB1 data for {len(ucb1_data)} source(s)")
    
    # Load current grades
    current_grades = load_current_grades()
    _log(f"Current grades: {current_grades}")
    
    # Adjust grades based on performance
    changes = adjust_grades(ucb1_data, current_grades)
    
    if changes:
        _log(f"{len(changes)} grade change(s) made:")
        for change in changes:
            _log(f"  {change['source']}: {change['old']}→{change['new']} (WR={change['wr']:.1%}, n={change['n']})")
        
        # Save updated grades
        save_grades(current_grades)
        
        # Alert on grade changes
        try:
            import sys
            sys.path.insert(0, str(BASE_DIR / "scripts"))
            from notifier import send
            
            msg_parts = [f"📊 UCB1: {len(changes)} source grade(s) updated"]
            for change in changes[:3]:  # Show first 3
                msg_parts.append(f"• {change['source']}: {change['old']}→{change['new']} ({change['wr']:.0%})")
            
            send("\n".join(msg_parts), level=2)
        except Exception as e:
            _log(f"Alert failed: {e}")
    else:
        _log("No grade changes needed")
    
    _log("=== UCB1 GRADE ADAPTER COMPLETE ===")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        _log("Interrupted")
    except Exception as e:
        _log(f"ADAPTER CRASHED: {e}")
        import traceback
        traceback.print_exc()
