#!/usr/bin/env python3
"""
Sanad Trader v3.1 — Quality Circuit Breaker

Autonomous safety mechanism that monitors cold path Judge reject rates
and automatically triggers safe mode when quality degrades.

Triggers safe mode if:
- Last N executed trades have reject_rate > threshold% (default: 50% over last 10)
- Catastrophic_rejects >= Y (default: ≥2 in last 10)

Safe mode actions:
- Sets config/safe_mode.flag with expiry timestamp
- Blocks EXECUTE decisions (paper-only mode)
- Requires synchronous cold path for next M trades after expiry
- Auto-expires after cooldown window (default: 1 hour)

Autonomous quality loop: runs every 10min via cron
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from state_store import DB_PATH

BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
CONFIG_DIR = BASE_DIR / "config"
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "quality_circuit_breaker.log"

LOGS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

SAFE_MODE_FLAG = CONFIG_DIR / "safe_mode.flag"
SAFE_MODE_HISTORY = CONFIG_DIR / "safe_mode_history.json"

# Thresholds (production-safe defaults)
LOOKBACK_COUNT = 10  # Last N trades
REJECT_RATE_THRESHOLD = 0.50  # 50%
CATASTROPHIC_COUNT_THRESHOLD = 2  # ≥2 catastrophic rejects
COOLDOWN_HOURS = 1  # Safe mode duration
SYNC_COLD_PATH_COUNT = 5  # Require sync cold path for next M trades after expiry


def _log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}\n"
    print(line.strip())
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def get_recent_executed_positions(limit: int = 10, since_timestamp: str = None):
    """
    Get last N EXECUTED positions (CLOSED + async_analysis_complete).
    
    Args:
        limit: Max positions to return
        since_timestamp: ISO timestamp - only return positions closed after this time
                         (prevents safe-mode oscillation by sampling only NEW trades)
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    
    if since_timestamp:
        rows = conn.execute("""
            SELECT position_id, token_address, async_analysis_json,
                   pnl_pct, force_close, force_close_reason
            FROM positions
            WHERE status = 'CLOSED'
              AND async_analysis_complete = 1
              AND closed_at > ?
            ORDER BY closed_at DESC
            LIMIT ?
        """, (since_timestamp, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT position_id, token_address, async_analysis_json,
                   pnl_pct, force_close, force_close_reason
            FROM positions
            WHERE status = 'CLOSED'
              AND async_analysis_complete = 1
            ORDER BY closed_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    
    conn.close()
    return [dict(r) for r in rows]


def get_last_safe_mode_activation():
    """Get timestamp of last safe mode activation from history file."""
    if not SAFE_MODE_HISTORY.exists():
        return None
    
    try:
        history = json.loads(SAFE_MODE_HISTORY.read_text())
        return history.get("last_activated_at")
    except Exception:
        return None


def record_safe_mode_activation(timestamp: str):
    """Record safe mode activation in persistent history."""
    history = {
        "last_activated_at": timestamp,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    SAFE_MODE_HISTORY.write_text(json.dumps(history, indent=2))


def check_quality_degradation():
    """
    Check if recent trades show quality degradation (high reject rate).
    
    Anti-oscillation: Only samples trades AFTER last safe-mode activation.
    If safe mode was triggered at T0, we only look at trades with closed_at > T0.
    
    Returns: (should_trigger_safe_mode: bool, stats: dict)
    """
    # Get baseline timestamp from persistent history
    since_timestamp = get_last_safe_mode_activation()
    
    if since_timestamp:
        _log(f"Baseline: sampling trades since last safe-mode activation ({since_timestamp})")
    else:
        _log("No baseline: first run or history cleared")
    
    recent = get_recent_executed_positions(LOOKBACK_COUNT, since_timestamp=since_timestamp)
    
    if len(recent) < LOOKBACK_COUNT:
        _log(f"Insufficient NEW data: {len(recent)}/{LOOKBACK_COUNT} positions since baseline. Skipping.")
        return False, {"count": len(recent), "reason": "insufficient_new_data", "baseline": since_timestamp}
    
    reject_count = 0
    catastrophic_count = 0
    
    for pos in recent:
        async_json_str = pos.get("async_analysis_json")
        if not async_json_str:
            continue
        
        try:
            async_json = json.loads(async_json_str)
            judge_parsed = async_json.get("judge", {}).get("parsed", {})
            verdict = judge_parsed.get("verdict")
            confidence = judge_parsed.get("confidence", 0)
            
            if verdict == "REJECT":
                reject_count += 1
                if confidence >= 85:
                    catastrophic_count += 1
        except Exception:
            pass
    
    reject_rate = reject_count / len(recent)
    
    stats = {
        "lookback_count": len(recent),
        "reject_count": reject_count,
        "catastrophic_count": catastrophic_count,
        "reject_rate": round(reject_rate, 3),
        "reject_rate_threshold": REJECT_RATE_THRESHOLD,
        "catastrophic_threshold": CATASTROPHIC_COUNT_THRESHOLD
    }
    
    _log(f"Quality check: {reject_count}/{len(recent)} rejects ({reject_rate:.1%}), "
         f"{catastrophic_count} catastrophic")
    
    # Trigger conditions
    if reject_rate > REJECT_RATE_THRESHOLD:
        _log(f"⚠️ QUALITY DEGRADATION: reject_rate {reject_rate:.1%} > {REJECT_RATE_THRESHOLD:.1%}")
        return True, stats
    
    if catastrophic_count >= CATASTROPHIC_COUNT_THRESHOLD:
        _log(f"⚠️ QUALITY DEGRADATION: {catastrophic_count} catastrophic rejects >= {CATASTROPHIC_COUNT_THRESHOLD}")
        return True, stats
    
    _log("✅ Quality OK")
    return False, stats


def activate_safe_mode(stats: dict):
    """
    Activate safe mode: write flag file with expiry timestamp.
    """
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=COOLDOWN_HOURS)
    activated_at = now.isoformat()

    flag_data = {
        "mode": "ACTIVE",
        "activated_at": activated_at,
        "expires_at": expiry.isoformat(),
        "reason": "quality_degradation",
        "stats": stats,
        # Canonical recovery fields
        "recovery_required": SYNC_COLD_PATH_COUNT,
        "recovery_remaining": SYNC_COLD_PATH_COUNT,
        # Legacy compat (other code may read this)
        "sync_cold_path_required": SYNC_COLD_PATH_COUNT,
    }

    SAFE_MODE_FLAG.write_text(json.dumps(flag_data, indent=2))

    # Record activation in persistent history
    record_safe_mode_activation(activated_at)

    _log(f"🚨 SAFE MODE ACTIVATED until {expiry.isoformat()}")
    _log(f"Stats: {json.dumps(stats)}")


def _parse_iso(ts: str):
    """Parse ISO timestamp safely (handles trailing 'Z')."""
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def advance_safe_mode_state():
    """
    Two-phase state machine (QCB is the sole owner of safe_mode.flag):

      ACTIVE   → blocks EXECUTE until expires_at; then transitions to RECOVERY
      RECOVERY → blocks EXECUTE until recovery_remaining reaches 0; then removes flag (CLEAR)
      CLEAR    → flag removed, normal trading resumes

    Returns: flag_data dict if flag still exists, else None.
    """
    if not SAFE_MODE_FLAG.exists():
        return None

    try:
        flag_data = json.loads(SAFE_MODE_FLAG.read_text())
    except Exception as e:
        _log(f"ERROR reading safe_mode.flag: {e}")
        return None

    now = datetime.now(timezone.utc)
    mode = (flag_data.get("mode") or "ACTIVE").upper()

    # Resolve canonical recovery counters (backward-compat: old flags may lack recovery_*)
    recovery_required = int(
        flag_data.get("recovery_required")
        or flag_data.get("sync_cold_path_required")
        or SYNC_COLD_PATH_COUNT
    )
    recovery_remaining = int(
        flag_data["recovery_remaining"]
        if flag_data.get("recovery_remaining") is not None
        else recovery_required
    )

    if mode == "ACTIVE":
        expires_at = _parse_iso(flag_data.get("expires_at"))
        if expires_at and now < expires_at:
            remaining_min = (expires_at - now).total_seconds() / 60.0
            _log(f"⏳ SAFE MODE ACTIVE, expires in {remaining_min:.0f}min")
            return flag_data  # Still in cooldown, no transition

        # Cooldown complete → transition to RECOVERY (do NOT delete flag)
        flag_data["mode"] = "RECOVERY"
        flag_data["recovery_required"] = recovery_required
        flag_data["recovery_remaining"] = recovery_remaining
        flag_data["recovery_started_at"] = now.isoformat()
        flag_data["updated_at"] = now.isoformat()
        SAFE_MODE_FLAG.write_text(json.dumps(flag_data, indent=2))
        _log(
            f"✅ SAFE MODE COOLDOWN COMPLETE → RECOVERY "
            f"({recovery_remaining}/{recovery_required} required)"
        )
        return flag_data

    if mode == "RECOVERY":
        if recovery_remaining <= 0:
            try:
                SAFE_MODE_FLAG.unlink()
            except Exception as e:
                _log(f"ERROR removing safe_mode.flag: {e}")
                return flag_data
            _log("✅ SAFE MODE RECOVERY COMPLETE, flag removed (CLEAR)")
            return None

        _log(
            f"🟡 SAFE MODE RECOVERY: "
            f"{recovery_remaining}/{recovery_required} sync approvals remaining"
        )
        return flag_data

    # Unknown mode: be conservative
    _log(f"WARNING: unknown safe mode mode={mode!r}. Treating as ACTIVE.")
    return flag_data


def main():
    _log("=" * 60)
    _log("Quality Circuit Breaker START")
    
    # Advance state machine (ACTIVE → RECOVERY → CLEAR)
    advance_safe_mode_state()

    # If safe mode is ACTIVE or RECOVERY, do not re-trigger; just manage state
    if SAFE_MODE_FLAG.exists():
        _log("Safe mode ACTIVE/RECOVERY, skipping quality check")
        return
    
    # Check quality degradation
    should_trigger, stats = check_quality_degradation()
    
    if should_trigger:
        activate_safe_mode(stats)
    else:
        _log("No action needed")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log(f"FATAL ERROR: {e}")
        import traceback
        _log(traceback.format_exc())
        sys.exit(1)
