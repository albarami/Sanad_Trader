#!/usr/bin/env python3
"""
COUNTERFACTUAL TRACKER — Learn from Rejections
Tracks signals that were REJECTED by Sanad or Judge.
Checks: what happened to those tokens 4/12/24 hours later?
Goal: Measure rejection accuracy. If we're rejecting winners, we're too conservative.
"""

import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timedelta

# --- CONFIG ---
BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
GENIUS_DIR = BASE_DIR / "genius-memory"
COUNTERFACTUAL_DIR = GENIUS_DIR / "counterfactual"

COUNTERFACTUAL_LOG = STATE_DIR / "counterfactual_rejections.json"
COUNTERFACTUAL_RESULTS = COUNTERFACTUAL_DIR / "rejection_accuracy.json"

# --- HELPERS ---
def _log(msg):
    ts = datetime.utcnow().isoformat() + "Z"
    print(f"[COUNTERFACTUAL] {ts} {msg}")


def load_rejections():
    """Load logged rejections."""
    if not COUNTERFACTUAL_LOG.exists():
        return []
    
    try:
        data = json.load(open(COUNTERFACTUAL_LOG))
        # Handle both formats: list or {"rejections": [...]}
        if isinstance(data, dict):
            return data.get("rejections", [])
        return data
    except:
        return []


def save_rejections(rejections):
    """Save rejection log."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(COUNTERFACTUAL_LOG, "w") as f:
        json.dump(rejections, f, indent=2)


def get_price_change(token, symbol, rejection_time_str, hours_later):
    """
    Get price change from rejection time to N hours later.
    Returns % change or None if data unavailable.
    """
    # TODO: Implement actual price history lookup
    # For MVP, we'd need to:
    # 1. Query Binance historical data for the token
    # 2. Compare price at rejection_time vs rejection_time + hours_later
    # 3. Return percentage change
    
    # For now, return None (will be implemented when price history is available)
    return None


def analyze_rejection(rejection):
    """
    Analyze a single rejection: what happened after?
    """
    token = rejection.get("token", "UNKNOWN")
    rejection_time_str = rejection.get("timestamp")
    rejection_price = rejection.get("price", 0)
    reason = rejection.get("reason", "unknown")
    
    if not rejection_time_str:
        return None
    
    try:
        rejection_time = datetime.fromisoformat(rejection_time_str.replace("Z", "+00:00"))
    except:
        return None
    
    now = datetime.utcnow().replace(tzinfo=rejection_time.tzinfo)
    hours_since = (now - rejection_time).total_seconds() / 3600
    
    # Only analyze if at least 24 hours have passed
    if hours_since < 24:
        return None
    
    # Get price changes (would use actual price data)
    price_4h = get_price_change(token, rejection.get("symbol"), rejection_time_str, 4)
    price_12h = get_price_change(token, rejection.get("symbol"), rejection_time_str, 12)
    price_24h = get_price_change(token, rejection.get("symbol"), rejection_time_str, 24)
    
    analysis = {
        "token": token,
        "rejected_at": rejection_time_str,
        "rejection_reason": reason,
        "rejection_price": rejection_price,
        "hours_since_rejection": round(hours_since, 1),
        
        # Price changes (None if data unavailable)
        "price_change_4h_pct": price_4h,
        "price_change_12h_pct": price_12h,
        "price_change_24h_pct": price_24h,
        
        # Outcome classification
        "outcome": None,  # "correct_rejection" or "missed_opportunity"
        "outcome_confidence": None,
        
        "analyzed_at": datetime.utcnow().isoformat() + "Z"
    }
    
    # Classify outcome (when price data available)
    if price_24h is not None:
        if price_24h > 20:  # Token went up >20%
            analysis["outcome"] = "missed_opportunity"
            analysis["outcome_confidence"] = "high" if price_24h > 50 else "medium"
        elif price_24h < -20:  # Token went down >20%
            analysis["outcome"] = "correct_rejection"
            analysis["outcome_confidence"] = "high"
        else:  # Token moved <20% either way
            analysis["outcome"] = "neutral"
            analysis["outcome_confidence"] = "low"
    
    return analysis


def aggregate_counterfactuals(analyses):
    """
    Aggregate counterfactual analyses to measure rejection accuracy.
    """
    if not analyses:
        return {}
    
    # Filter analyzed (those with outcome)
    analyzed = [a for a in analyses if a.get("outcome")]
    
    if not analyzed:
        return {
            "total_rejections": len(analyses),
            "analyzed": 0,
            "rejection_accuracy": None,
            "insights": ["Not enough data (need price history after rejections)"]
        }
    
    correct = [a for a in analyzed if a["outcome"] == "correct_rejection"]
    missed = [a for a in analyzed if a["outcome"] == "missed_opportunity"]
    neutral = [a for a in analyzed if a["outcome"] == "neutral"]
    
    rejection_accuracy = len(correct) / len(analyzed) if analyzed else 0
    
    agg = {
        "total_rejections": len(analyses),
        "analyzed": len(analyzed),
        "correct_rejections": len(correct),
        "missed_opportunities": len(missed),
        "neutral": len(neutral),
        "rejection_accuracy": round(rejection_accuracy, 3),
        "insights": [],
        "recommendations": []
    }
    
    # Generate insights
    if rejection_accuracy < 0.60:
        agg["insights"].append(f"⚠️ LOW ACCURACY: Only {rejection_accuracy:.0%} of rejections were correct")
        agg["recommendations"].append("LOOSEN GATES: System is too conservative, rejecting too many good signals")
    elif rejection_accuracy > 0.85:
        agg["insights"].append(f"✅ HIGH ACCURACY: {rejection_accuracy:.0%} of rejections were correct")
        agg["recommendations"].append("WELL-CALIBRATED: Current rejection thresholds are appropriate")
    else:
        agg["insights"].append(f"🎯 ACCEPTABLE ACCURACY: {rejection_accuracy:.0%} of rejections were correct")
    
    if len(missed) >= 3:
        agg["recommendations"].append(f"REVIEW MISSED SIGNALS: {len(missed)} rejected tokens went up >20%")
    
    return agg


def run():
    """Main counterfactual tracker."""
    _log("=== COUNTERFACTUAL TRACKER START ===")
    
    rejections = load_rejections()
    
    if not rejections:
        _log("No rejections logged yet")
        return
    
    _log(f"Loaded {len(rejections)} rejection(s)")
    
    # Analyze each rejection
    analyses = []
    for rejection in rejections:
        try:
            analysis = analyze_rejection(rejection)
            if analysis:
                analyses.append(analysis)
        except Exception as e:
            _log(f"Analysis failed for {rejection.get('token')}: {e}")
    
    if not analyses:
        _log("No rejections old enough to analyze (need 24h)")
        return
    
    _log(f"Analyzed {len(analyses)} rejection(s)")
    
    # Aggregate results
    results = aggregate_counterfactuals(analyses)
    
    # Save results
    COUNTERFACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    
    # Save individual analyses
    for analysis in analyses:
        filename = f"{analysis['token']}_{int(datetime.fromisoformat(analysis['analyzed_at'].replace('Z', '+00:00')).timestamp())}.json"
        filepath = COUNTERFACTUAL_DIR / filename
        
        with open(filepath, "w") as f:
            json.dump(analysis, f, indent=2)
    
    # Save aggregate results
    with open(COUNTERFACTUAL_RESULTS, "w") as f:
        json.dump(results, f, indent=2)
    
    _log(f"Counterfactual analysis:")
    _log(f"  Total rejections: {results['total_rejections']}")
    _log(f"  Analyzed (24h+ old): {results['analyzed']}")
    
    if results.get("rejection_accuracy") is not None:
        _log(f"  Rejection accuracy: {results['rejection_accuracy']:.1%}")
        _log(f"  Correct rejections: {results['correct_rejections']}")
        _log(f"  Missed opportunities: {results['missed_opportunities']}")
        
        if results["insights"]:
            _log("  Insights:")
            for insight in results["insights"]:
                _log(f"    • {insight}")
        
        if results["recommendations"]:
            _log("  Recommendations:")
            for rec in results["recommendations"]:
                _log(f"    • {rec}")
            
            # Alert on critical recommendations
            try:
                sys.path.insert(0, str(BASE_DIR / "scripts"))
                from notifier import send
                
                msg = "🔍 COUNTERFACTUAL ANALYSIS:\n" + "\n".join(f"• {r}" for r in results["recommendations"][:2])
                send(msg, level=2)
            except Exception:
                pass
    
    _log("=== COUNTERFACTUAL TRACKER COMPLETE ===")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        _log("Interrupted")
    except Exception as e:
        _log(f"TRACKER CRASHED: {e}")
        import traceback
        traceback.print_exc()
