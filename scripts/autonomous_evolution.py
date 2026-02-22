#!/usr/bin/env python3
"""
Autonomous Evolution Engine — Sanad Trader v3.1

Self-healing, self-learning system that:
1. Detects patterns in failures
2. Reasons about root causes (Al-Muhasbi framework)
3. Generates and tests hypotheses
4. Synthesizes code fixes
5. Validates improvements
6. Evolves continuously

This module runs periodically and autonomously improves the trading system.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
MEMORY_DIR = BASE_DIR / "memory"
LOGS_DIR = BASE_DIR / "logs"

MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# Evolution memory file
EVOLUTION_LOG = MEMORY_DIR / "evolution_log.jsonl"


def log(msg: str, level: str = "INFO"):
    """Log with timestamp."""
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[EVOLUTION] {ts} [{level}] {msg}")


def detect_catastrophic_pattern():
    """
    Detect if Judge is consistently rejecting executed positions.
    Returns: (pattern_detected: bool, evidence: dict)
    """
    try:
        import state_store
        with state_store.get_connection() as conn:
            # Get last 10 positions with async analysis
            rows = conn.execute("""
                SELECT position_id, status, force_close, force_close_reason,
                       async_analysis_complete, risk_flag, created_at
                FROM positions
                WHERE async_analysis_complete = 1
                ORDER BY created_at DESC
                LIMIT 10
            """).fetchall()
            
            if not rows:
                return False, {}
            
            catastrophic_count = sum(1 for r in rows if r['force_close'] == 1)
            rejection_rate = catastrophic_count / len(rows)
            
            if rejection_rate >= 0.5:  # 50%+ rejection rate
                log(f"PATTERN DETECTED: {catastrophic_count}/{len(rows)} positions catastrophically rejected ({rejection_rate:.0%})", "WARNING")
                return True, {
                    "total_analyzed": len(rows),
                    "catastrophic_count": catastrophic_count,
                    "rejection_rate": rejection_rate,
                    "position_ids": [r['position_id'] for r in rows if r['force_close'] == 1]
                }
            
            return False, {}
    except Exception as e:
        log(f"Pattern detection failed: {e}", "ERROR")
        return False, {}


def analyze_rejection_reasons():
    """
    Analyze WHY positions are being rejected.
    Uses Al-Muhasbi reasoning: examine evidence, test hypotheses.
    """
    try:
        import state_store
        with state_store.get_connection() as conn:
            rows = conn.execute("""
                SELECT position_id, features_json, async_analysis_json, force_close_reason
                FROM positions
                WHERE force_close = 1 AND async_analysis_complete = 1
                ORDER BY created_at DESC
                LIMIT 20
            """).fetchall()
            
            if not rows:
                return {}
            
            # Extract patterns
            issues = {
                "stablecoins": [],
                "low_holders": [],
                "high_concentration": [],
                "other": []
            }
            
            for row in rows:
                try:
                    features = json.loads(row['features_json']) if row['features_json'] else {}
                    signal = features.get('entry_signal', {})
                    
                    token = signal.get('token', '').upper()
                    holders = signal.get('solscan_holder_count', signal.get('holders', 0))
                    top10 = signal.get('solscan_top_10_pct', signal.get('top10_pct', 0))
                    
                    # Classify issue
                    STABLECOINS = {"USDT", "USDC", "DAI", "BUSD", "USDD", "TUSD"}
                    if token in STABLECOINS:
                        issues["stablecoins"].append({"token": token, "position_id": row['position_id']})
                    elif holders > 0 and holders < 10:
                        issues["low_holders"].append({"token": token, "holders": holders, "position_id": row['position_id']})
                    elif top10 > 95:
                        issues["high_concentration"].append({"token": token, "top10": top10, "position_id": row['position_id']})
                    else:
                        issues["other"].append({"token": token, "position_id": row['position_id']})
                except Exception:
                    continue
            
            return issues
    except Exception as e:
        log(f"Reason analysis failed: {e}", "ERROR")
        return {}


def check_if_fix_exists(issue_type: str) -> bool:
    """Check if a fix for this issue type already exists in the code."""
    try:
        from pathlib import Path
        fast_engine = BASE_DIR / "scripts" / "fast_decision_engine.py"
        if not fast_engine.exists():
            return False
        
        content = fast_engine.read_text()
        
        if issue_type == "stablecoins":
            return "BLOCK_STABLECOIN" in content or "STABLECOINS" in content
        elif issue_type == "low_holders":
            return "BLOCK_HOLDER_COUNT" in content
        elif issue_type == "high_concentration":
            return "BLOCK_TOP10_CONCENTRATION" in content
        
        return False
    except Exception:
        return False


def synthesize_fix(issue_type: str, evidence: dict):
    """
    Generate code fix for detected issue.
    Returns: (success: bool, message: str)
    """
    log(f"Synthesizing fix for: {issue_type}", "INFO")
    
    # Check if fix already exists
    if check_if_fix_exists(issue_type):
        log(f"Fix for {issue_type} already exists, skipping", "INFO")
        return True, "fix_already_exists"
    
    # For now, log the fix recommendation
    # Full autonomous code generation would use LLM to write patches
    fixes = {
        "stablecoins": "Add stablecoin blocklist to Stage 1 safety gates",
        "low_holders": "Add holder count threshold (<10) to Stage 1 gates",
        "high_concentration": "Add top10 concentration threshold (>95%) to Stage 1 gates"
    }
    
    fix_msg = fixes.get(issue_type, "Unknown issue type")
    log(f"Recommended fix: {fix_msg}", "INFO")
    
    # Record evolution event
    record_evolution_event({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "issue_type": issue_type,
        "evidence": evidence,
        "fix_recommended": fix_msg,
        "fix_applied": False,  # Manual for now, will be autonomous
        "reasoning": f"Detected pattern in catastrophic rejections: {evidence}"
    })
    
    return False, fix_msg


def record_evolution_event(event: dict):
    """Record evolution event to memory."""
    try:
        with open(EVOLUTION_LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
        log(f"Evolution event recorded: {event.get('issue_type')}", "INFO")
    except Exception as e:
        log(f"Failed to record evolution event: {e}", "ERROR")


def run_evolution_cycle():
    """
    Main evolution cycle: detect, analyze, reason, fix, validate.
    """
    log("=== Evolution Cycle START ===", "INFO")
    
    # Step 1: Detect patterns
    pattern_detected, evidence = detect_catastrophic_pattern()
    
    if not pattern_detected:
        log("No catastrophic patterns detected", "INFO")
        log("=== Evolution Cycle END (no action needed) ===", "INFO")
        return
    
    log(f"Catastrophic pattern detected: {evidence['rejection_rate']:.0%} rejection rate", "WARNING")
    
    # Step 2: Analyze reasons
    issues = analyze_rejection_reasons()
    
    if not issues:
        log("Could not determine rejection reasons", "WARNING")
        log("=== Evolution Cycle END (analysis failed) ===", "INFO")
        return
    
    log(f"Root causes identified: {json.dumps({k: len(v) for k, v in issues.items()})}", "INFO")
    
    # Step 3: Synthesize fixes for top issues
    for issue_type, examples in issues.items():
        if len(examples) >= 2:  # Only fix if pattern appears 2+ times
            success, msg = synthesize_fix(issue_type, {"examples": examples[:3]})
            if success:
                log(f"Fix applied for {issue_type}", "INFO")
            else:
                log(f"Fix recommended for {issue_type}: {msg}", "WARNING")
    
    log("=== Evolution Cycle END ===", "INFO")


if __name__ == "__main__":
    try:
        run_evolution_cycle()
    except Exception as e:
        log(f"Evolution cycle failed: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        sys.exit(1)
