#!/usr/bin/env python3
"""
WATCHDOG — Self-Healing System Monitor
Runs every 2 minutes. Detects and AUTO-FIXES recoverable problems.
Zero LLM calls. Pure deterministic logic.

Watchdog Philosophy:
1. Detect the problem
2. FIX the problem
3. TELL the user what it found and how it fixed it
4. Only escalate if auto-fix fails 3 times

Telegram message format:
- Auto-fix success: 🔧 WATCHDOG AUTO-FIX: [problem] → [action] → ✅ [result]
- Auto-fix failed: 🚨 WATCHDOG NEEDS HELP: [problem] → [attempted 3x] → ❌ [error] → [impact]
"""

import os
import sys
import json
import time
import psutil
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone

# --- CONFIG ---
BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
SCRIPTS_DIR = BASE_DIR / "scripts"
STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "logs"
GENIUS_DIR = BASE_DIR / "genius-memory" / "watchdog-actions"
WATCHDOG_LOG = LOGS_DIR / "watchdog.log"
ACTIONS_LOG = GENIUS_DIR / "actions.jsonl"

# Import state_store for unified state management (Ticket 12)
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    import state_store
    state_store.install_ssot_guard()
    HAS_STATE_STORE = True
except ImportError:
    HAS_STATE_STORE = False

# Thresholds (adjusted to reduce noise)
CONSECUTIVE_ERROR_THRESHOLD = 3
ROUTER_STALL_MIN = 30
POSITION_FRESHNESS_MIN = 5
DATA_FRESHNESS_MIN = 15
RECONCILIATION_STALE_SEC = 1500  # 25 minutes (was 15, too noisy)
DEXSCREENER_STALE_MIN = 15  # 15 minutes (was 10)
LOCK_AGE_MIN = 10
LONG_RUNNING_PROCESS_SEC = 600
COST_WARNING_PCT = 0.8
DISK_WARNING_PCT = 0.9
MEMORY_WARNING_PCT = 0.9

# Alerts
ALERT_LEVEL_INFO = 1
ALERT_LEVEL_WARNING = 2
ALERT_LEVEL_CRITICAL = 3

# Auto-fix attempt tracking (MUST persist across cron runs)
_fix_attempts = {}
ATTEMPTS_FILE = STATE_DIR / "watchdog_attempts.json"

def _load_attempts():
    """Load persisted attempt counters from disk."""
    global _fix_attempts
    if ATTEMPTS_FILE.exists():
        try:
            _fix_attempts = json.load(open(ATTEMPTS_FILE))
        except:
            _fix_attempts = {}
    else:
        _fix_attempts = {}

def _save_attempts():
    """Persist attempt counters to disk."""
    try:
        ATTEMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ATTEMPTS_FILE, "w") as f:
            json.dump(_fix_attempts, f, indent=2)
    except Exception as e:
        _log(f"Failed to save attempts: {e}", "ERROR")

# --- HELPERS ---
def _log(msg, level="INFO"):
    """Log to watchdog.log with timestamp."""
    ts = datetime.now(timezone.utc).isoformat() + "Z"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(WATCHDOG_LOG, "a") as f:
        f.write(f"[{level}] {ts} {msg}\n")
    print(f"[{level}] {msg}")


def _log_action(component, problem, action, result, duration_sec=None, attempts=1, error=None, escalated=False):
    """Log structured action to genius-memory/watchdog-actions/actions.jsonl."""
    GENIUS_DIR.mkdir(parents=True, exist_ok=True)
    
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "component": component,
        "problem": problem,
        "action": action,
        "result": result,
        "attempts": attempts,
    }
    
    if duration_sec is not None:
        entry["duration_sec"] = duration_sec
    if error:
        entry["error"] = str(error)[:200]
    if escalated:
        entry["escalated"] = escalated
    
    with open(ACTIONS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _alert(msg, level=ALERT_LEVEL_WARNING):
    """Send Telegram alert."""
    try:
        sys.path.insert(0, str(BASE_DIR / "scripts"))
        from notifier import send
        send(msg, level=level)
    except Exception as e:
        _log(f"Alert failed: {e}", "ERROR")


def _kill_process(pattern):
    """Kill process matching pattern."""
    try:
        result = subprocess.run(
            ["pkill", "-f", pattern],
            capture_output=True,
            timeout=5
        )
        if result.returncode == 0:
            _log(f"Killed process: {pattern}")
            return True
        else:
            _log(f"No process found: {pattern}", "WARNING")
            return False
    except Exception as e:
        _log(f"Kill failed for {pattern}: {e}", "ERROR")
        return False


def _remove_lock(lock_path):
    """Remove stale lock file."""
    try:
        if lock_path.exists():
            lock_path.unlink()
            _log(f"Removed stale lock: {lock_path}")
            return True
    except Exception as e:
        _log(f"Failed to remove lock {lock_path}: {e}", "ERROR")
    return False


def _run_script(script_name, timeout=60):
    """Force-run a script."""
    start = time.time()
    try:
        script_path = BASE_DIR / "scripts" / script_name
        result = subprocess.run(
            ["python3", str(script_path)],
            cwd=BASE_DIR,
            capture_output=True,
            timeout=timeout,
            text=True
        )
        duration = time.time() - start
        
        if result.returncode == 0:
            _log(f"Force-ran: {script_name} ({duration:.1f}s)")
            return True, duration, None
        else:
            error = result.stderr[:200] if result.stderr else "Unknown error"
            _log(f"Script failed {script_name}: {error}", "ERROR")
            return False, duration, error
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        error = f"Timeout after {timeout}s"
        _log(f"Force-run timeout {script_name}: {error}", "ERROR")
        return False, duration, error
    except Exception as e:
        duration = time.time() - start
        error = str(e)
        _log(f"Force-run failed {script_name}: {error}", "ERROR")
        return False, duration, error


def _compile_diagnostic_package(context, age_minutes):
    """
    Compile diagnostic package for OpenClaw or human escalation.
    Returns formatted string with:
    - Last 20 lines of signal_router.log
    - Stall context (token, stage, errors)
    - Stall frequency (last 2 hours)
    - Circuit breaker states
    """
    try:
        diagnostic = []
        
        # 1. Router log tail
        router_log = LOGS_DIR / "signal_router.log"
        if router_log.exists():
            with open(router_log) as f:
                lines = f.readlines()[-20:]
                diagnostic.append("📋 Last 20 lines of signal_router.log:")
                diagnostic.append("```")
                diagnostic.extend([line.rstrip() for line in lines])
                diagnostic.append("```")
        
        # 2. Stall context
        if context:
            diagnostic.append("\n🎯 Stall Context:")
            if context.get("last_token"):
                diagnostic.append(f"  Token: {context['last_token']}")
            if context.get("last_stage"):
                diagnostic.append(f"  Stage: {context['last_stage']}")
            if context.get("last_api_call"):
                diagnostic.append(f"  API: {context['last_api_call']}")
            if context.get("hints"):
                diagnostic.append(f"  Hints: {', '.join(context['hints'])}")
        
        # 3. Stall frequency
        actions_log = GENIUS_DIR / "actions.jsonl"
        if actions_log.exists():
            two_hours_ago = time.time() - 7200
            stalls = []
            with open(actions_log) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("component") == "signal_router" and "stalled" in entry.get("problem", ""):
                            ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                            if ts.timestamp() > two_hours_ago:
                                stalls.append(entry)
                    except:
                        pass
            
            diagnostic.append(f"\n📊 Stall Frequency (last 2h): {len(stalls)} stalls")
            if stalls:
                diagnostic.append("  Recent stalls:")
                for s in stalls[-5:]:
                    diagnostic.append(f"    - {s.get('problem')} → {s.get('action')} (attempt {s.get('attempts')})")
        
        # 4. Circuit breakers
        try:
            circuit_file = STATE_DIR / "circuit_breakers.json"
            if circuit_file.exists():
                breakers = json.load(open(circuit_file))
                open_breakers = [k for k, v in breakers.items() if v.get("state") == "OPEN"]
                if open_breakers:
                    diagnostic.append(f"\n⚠️ Circuit Breakers OPEN: {', '.join(open_breakers)}")
                else:
                    diagnostic.append("\n✅ Circuit Breakers: All closed")
        except:
            pass
        
        return "\n".join(diagnostic)
    
    except Exception as e:
        return f"Error compiling diagnostic: {e}"


def _escalate_to_openclaw(diagnostic):
    """
    Send diagnostic package to OpenClaw main session for code-level fix.
    Uses sessions_send to route to main session.
    Returns True if sent successfully, False otherwise.
    """
    try:
        message = (
            f"🔧 WATCHDOG ESCALATION (Tier 3.5):\n"
            f"\nRouter has stalled repeatedly. Watchdog auto-fixes (kill, force-run, fast-path) all failed.\n"
            f"\n**Your task:** Diagnose the root cause and apply a code-level fix.\n"
            f"\n**Diagnostic Package:**\n"
            f"{diagnostic}\n"
            f"\n**Possible fixes:**\n"
            f"- Increase API timeout in sanad_pipeline.py\n"
            f"- Add retry logic for 500/502/503 errors\n"
            f"- Skip problematic tokens temporarily\n"
            f"- Switch to backup model if primary is down\n"
            f"- Adjust rate limiting\n"
            f"\n**Time limit:** 30 minutes. If unresolved, I will escalate to Salim.\n"
            f"\n**Action:** Read logs, edit code, test fix, report back."
        )
        
        # Write to a flag file so OpenClaw can detect it via heartbeat
        escalation_file = STATE_DIR / "openclaw_escalation.json"
        escalation_data = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "component": "signal_router",
            "tier": "3.5",
            "diagnostic": diagnostic,
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat() + "Z",
            "status": "pending"
        }
        
        with open(escalation_file, "w") as f:
            json.dump(escalation_data, f, indent=2)
        
        _log(f"Escalation package written to {escalation_file}")
        
        # Also try to send via sessions_send (may fail if not in OpenClaw context)
        try:
            # This will only work if we're running inside OpenClaw
            # For now, just log it - the heartbeat will pick it up
            _log("Escalation flag set for OpenClaw heartbeat detection")
        except:
            pass
        
        return True
    
    except Exception as e:
        _log(f"Failed to escalate to OpenClaw: {e}", "ERROR")
        return False


def _get_escalation_history():
    """
    Get history of OpenClaw escalation attempts from state file.
    Returns formatted string describing what OpenClaw tried.
    """
    try:
        escalation_file = STATE_DIR / "openclaw_escalation.json"
        if not escalation_file.exists():
            return "  (No OpenClaw escalation attempted yet)"
        
        data = json.load(open(escalation_file))
        status = data.get("status", "unknown")
        
        if status == "pending":
            return "  • OpenClaw escalation in progress (no response yet)"
        elif status == "resolved":
            actions = data.get("actions_taken", [])
            if actions:
                history = ["  • OpenClaw attempted:"]
                history.extend([f"    - {action}" for action in actions])
                history.append(f"  • Result: {data.get('result', 'Unknown')}")
                return "\n".join(history)
            else:
                return "  • OpenClaw resolved (no details available)"
        elif status == "failed":
            return f"  • OpenClaw attempted fix but failed: {data.get('error', 'Unknown error')}"
        else:
            return f"  • OpenClaw escalation status: {status}"
    
    except Exception as e:
        return f"  (Error reading escalation history: {e})"


def _analyze_router_context():
    """
    Analyze router log to find context of stall:
    - Which token was being processed?
    - Which pipeline stage (Sanad, Bull, Bear, Judge)?
    - Which API call might have timed out?
    Returns dict with context or None if can't determine.
    """
    try:
        router_log = LOGS_DIR / "signal_router.log"
        if not router_log.exists():
            return None
        
        # Read last 100 lines
        with open(router_log) as f:
            lines = f.readlines()[-100:]
        
        context = {
            "last_token": None,
            "last_stage": None,
            "last_api_call": None,
            "hints": []
        }
        
        for line in reversed(lines):
            # Extract token
            if "Selected:" in line and not context["last_token"]:
                parts = line.split("Selected: ")
                if len(parts) > 1:
                    context["last_token"] = parts[1].split()[0].strip()
            
            # Extract stage
            if "Pipeline result:" in line and not context["last_stage"]:
                context["last_stage"] = "completed"
            elif "Al-Muhasbi Judge" in line or "GPT call" in line:
                context["last_stage"] = "judge"
                context["last_api_call"] = "openai_gpt"
            elif "Bull" in line or "Al-Baqarah" in line:
                context["last_stage"] = "bull"
                context["last_api_call"] = "anthropic_opus"
            elif "Bear" in line or "Al-Dahhak" in line:
                context["last_stage"] = "bear"
                context["last_api_call"] = "anthropic_opus"
            elif "Sanad" in line or "verification" in line:
                context["last_stage"] = "sanad"
                context["last_api_call"] = "anthropic_opus"
            
            # Hints for patterns
            if "timeout" in line.lower():
                context["hints"].append("timeout_detected")
            if "429" in line or "rate limit" in line.lower():
                context["hints"].append("rate_limit")
            if "500" in line or "502" in line or "503" in line:
                context["hints"].append("api_server_error")
        
        return context if context["last_token"] or context["last_stage"] else None
    
    except Exception as e:
        _log(f"Context analysis failed: {e}", "ERROR")
        return None


def _track_attempts(component):
    """Track fix attempts for escalation logic. PERSISTS to disk."""
    if component not in _fix_attempts:
        _fix_attempts[component] = {"count": 0, "last_attempt": 0}
    
    now = time.time()
    # Reset if last attempt was >10 minutes ago
    if now - _fix_attempts[component]["last_attempt"] > 600:
        _fix_attempts[component] = {"count": 0, "last_attempt": 0}
    
    _fix_attempts[component]["count"] += 1
    _fix_attempts[component]["last_attempt"] = now
    
    _save_attempts()  # PERSIST to survive cron restarts
    return _fix_attempts[component]["count"]


def _reset_attempts(component):
    """Reset attempt counter after successful fix. PERSISTS to disk."""
    if component in _fix_attempts:
        _fix_attempts[component] = {"count": 0, "last_attempt": 0}
        _save_attempts()  # PERSIST


def _clear_escalation_artifacts(component):
    """
    Clear escalation artifacts when component recovers.
    Called when lease/heartbeat shows component is healthy.
    """
    files_to_clear = [
        STATE_DIR / "openclaw_escalation.json",
        STATE_DIR / "router_paused.flag",
        STATE_DIR / "router_fast_path.flag"
    ]
    
    cleared = []
    for f in files_to_clear:
        if f.exists():
            try:
                f.unlink()
                cleared.append(f.name)
            except Exception as e:
                _log(f"Failed to clear {f.name}: {e}", "WARNING")
    
    # Reset attempt counters
    _reset_attempts(component)
    
    if cleared:
        _log(f"Cleared escalation artifacts for {component}: {cleared}", "INFO")


# --- CHECKS ---

def check_reconciliation_staleness():
    """
    Check if reconciliation is stale (>15min).
    Auto-fix: clear lock + force reconciliation.py.
    Only escalate if 3 consecutive fix attempts fail.
    """
    issues = []
    try:
        recon_file = STATE_DIR / "reconciliation.json"
        if not recon_file.exists():
            return []
        
        recon = json.load(open(recon_file))
        last_run_str = recon.get("last_reconciliation_timestamp")
        
        if not last_run_str:
            return []
        
        # Parse ISO timestamp
        last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
        age_sec = (datetime.now(timezone.utc).replace(tzinfo=last_run.tzinfo) - last_run).total_seconds()
        
        if age_sec > RECONCILIATION_STALE_SEC:
            age_min = int(age_sec / 60)
            threshold_min = int(RECONCILIATION_STALE_SEC / 60)
            
            _log(f"Reconciliation stale: {age_sec:.0f}s ({age_min}min) ago, threshold {threshold_min}min", "WARNING")
            
            # Track attempts
            attempts = _track_attempts("reconciliation")
            
            # AUTO-FIX
            lock_file = STATE_DIR / "reconciliation.lock"
            _remove_lock(lock_file)
            
            success, duration, error = _run_script("reconciliation.py", timeout=120)
            
            if success:
                # Success!
                _reset_attempts("reconciliation")
                
                _log_action(
                    component="reconciliation",
                    problem=f"stale_{age_min}min",
                    action="clear_lock+rerun",
                    result="success",
                    duration_sec=int(duration),
                    attempts=attempts
                )
                
                # Tier 0/1: Log only, no Telegram (reduces noise)
                _log(f"Auto-fix successful: Reconciliation ({age_min}min → fresh in {duration:.1f}s)")
                
                return []  # Fixed, no issue
            
            else:
                # Failed
                _log_action(
                    component="reconciliation",
                    problem=f"stale_{age_min}min",
                    action="clear_lock+rerun",
                    result="failed",
                    duration_sec=int(duration),
                    attempts=attempts,
                    error=error,
                    escalated=(attempts >= 3)
                )
                
                if attempts >= 3:
                    # Escalate
                    _alert(
                        f"🚨 WATCHDOG NEEDS HELP:\n"
                        f"• Problem: Reconciliation stale ({age_min}min)\n"
                        f"• Attempted: Cleared lock + re-ran {attempts} times\n"
                        f"• Result: ❌ Still failing\n"
                        f"• Error: {error}\n"
                        f"• Impact: Gate 11 blocking all trades",
                        ALERT_LEVEL_CRITICAL
                    )
                    # Reset attempts so we don't spam every 2 minutes
                    _reset_attempts("reconciliation")
                
                issues.append(f"Reconciliation stale {age_min}min (auto-fix failed {attempts}x)")
    
    except Exception as e:
        _log(f"Reconciliation staleness check failed: {e}", "ERROR")
    
    return issues


def check_dexscreener_freshness():
    """
    Check if DexScreener signals are fresh (<10 min).
    Auto-fix: force-run dexscreener_client.py.
    """
    issues = []
    try:
        dex_signals_dir = BASE_DIR / "signals" / "dexscreener"
        if not dex_signals_dir.exists():
            return []
        
        # Find most recent file
        files = sorted(dex_signals_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        if not files:
            return []
        
        latest_file = files[0]
        age_sec = time.time() - latest_file.stat().st_mtime
        age_min = int(age_sec / 60)
        
        # Threshold: 15 minutes (was 10, reduced noise)
        if age_sec > (DEXSCREENER_STALE_MIN * 60):
            _log(f"DexScreener signals stale: {age_min}min ago (max 10min)", "WARNING")
            
            # Track attempts
            attempts = _track_attempts("dexscreener")
            
            # AUTO-FIX: force-run scanner
            success, duration, error = _run_script("dexscreener_client.py", timeout=120)
            
            if success:
                _reset_attempts("dexscreener")
                
                _log_action(
                    component="dexscreener",
                    problem=f"stale_{age_min}min",
                    action="force_rerun",
                    result="success",
                    duration_sec=int(duration),
                    attempts=attempts
                )
                
                # Tier 0/1: Log only, no Telegram
                _log(f"Auto-fix successful: DexScreener ({age_min}min → fresh in {duration:.1f}s)")
                
                return []
            else:
                _log_action(
                    component="dexscreener",
                    problem=f"stale_{age_min}min",
                    action="force_rerun",
                    result="failed",
                    duration_sec=int(duration),
                    attempts=attempts,
                    error=error,
                    escalated=(attempts >= 3)
                )
                
                if attempts >= 3:
                    _alert(
                        f"🚨 WATCHDOG NEEDS HELP:\n"
                        f"• Problem: DexScreener signals stale ({age_min}min)\n"
                        f"• Attempted: Force-ran scanner {attempts} times\n"
                        f"• Result: ❌ Still failing\n"
                        f"• Error: {error}\n"
                        f"• Impact: Missing 3rd corroboration source for meme tokens",
                        ALERT_LEVEL_WARNING
                    )
                    _reset_attempts("dexscreener")
                
                issues.append(f"DexScreener stale {age_min}min (auto-fix failed {attempts}x)")
    
    except Exception as e:
        _log(f"DexScreener freshness check failed: {e}", "ERROR")
    
    return issues


def check_cron_health():
    """Check for stuck crons with consecutive errors >= 3."""
    issues = []
    try:
        # OpenClaw cron command not available in container, skip for now
        # This check requires gateway API access or cron tool integration
        return []
        
        data = {}
        jobs = data.get("jobs", [])
        
        for job in jobs:
            if not job.get("enabled"):
                continue
            
            state = job.get("state", {})
            errors = state.get("consecutiveErrors", 0)
            name = job.get("name", "unknown")
            
            if errors >= CONSECUTIVE_ERROR_THRESHOLD:
                _log(f"Cron stuck: {name} ({errors} consecutive errors)", "WARNING")
                
                # Try to fix
                # 1. Kill related process
                process_map = {
                    "Signal Router": "signal_router.py",
                    "Position Monitor": "position_monitor.py",
                    "Reconciliation": "reconciliation.py"
                }
                
                pattern = process_map.get(name)
                if pattern:
                    _kill_process(pattern)
                
                # 2. Clear related locks
                lock_map = {
                    "Signal Router": "signal_window.lock",
                    "Position Monitor": "portfolio.lock",
                    "Reconciliation": "reconciliation.lock"
                }
                
                lock_name = lock_map.get(name)
                if lock_name:
                    _remove_lock(STATE_DIR / lock_name)
                
                issues.append(f"{name}: {errors} errors (killed + unlocked)")
                _alert(f"🔧 Cron stuck: {name} ({errors} errors). Auto-fixed (killed + unlocked).", ALERT_LEVEL_WARNING)
    
    except Exception as e:
        _log(f"Cron health check failed: {e}", "ERROR")
    
    return issues


def check_stale_locks():
    """
    Check for stale lock files and clean them up automatically.
    Lock TTL: 15 minutes - any lock older than this is considered stale.
    """
    issues = []
    lock_ttl_minutes = 15
    
    try:
        # Check signal_window.lock
        lock_file = STATE_DIR / "signal_window.lock"
        if lock_file.exists():
            age_sec = time.time() - lock_file.stat().st_mtime
            age_min = age_sec / 60
            if age_min > lock_ttl_minutes:
                try:
                    lock_file.unlink()
                    _log(f"🔧 Cleared stale lock: signal_window.lock (age: {age_min:.1f}min)", "WARNING")
                    issues.append(f"Cleared stale signal_window.lock ({age_min:.0f}min old)")
                except Exception as e:
                    _log(f"Failed to clear stale lock: {e}", "ERROR")
        
        # Check signal_mutex.json entries
        mutex_file = STATE_DIR / "signal_mutex.json"
        if mutex_file.exists():
            try:
                mutex_data = json.load(open(mutex_file))
                locks = mutex_data.get("locks", {})
                cleared = []
                
                for token, timestamp_str in list(locks.items()):
                    try:
                        lock_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                        age_sec = (datetime.now(timezone.utc).replace(tzinfo=lock_time.tzinfo) - lock_time).total_seconds()
                        age_min = age_sec / 60
                        
                        if age_min > lock_ttl_minutes:
                            del locks[token]
                            cleared.append(f"{token}({age_min:.0f}min)")
                    except:
                        # Invalid timestamp - remove it
                        del locks[token]
                        cleared.append(f"{token}(invalid)")
                
                if cleared:
                    mutex_data["locks"] = locks
                    with open(mutex_file, "w") as f:
                        json.dump(mutex_data, f, indent=2)
                    _log(f"🔧 Cleared {len(cleared)} stale mutex locks: {', '.join(cleared)}", "WARNING")
                    issues.append(f"Cleared {len(cleared)} stale mutex locks")
            except Exception as e:
                _log(f"Failed to check/clear mutex locks: {e}", "ERROR")
    
    except Exception as e:
        _log(f"Stale lock check failed: {e}", "ERROR")
    
    return issues


def check_router_stall():
    """
    Check if signal router is stalled using LEASE AS TRUTH.
    ADAPTIVE ESCALATION (3-LAYER SELF-HEALING):
      Tier 1 (attempt 1): Kill + clear lock (standard restart)
      Tier 2 (attempt 2): Kill + force manual router run to verify it completes
      Tier 3 (attempt 3): Kill + enable fast-path mode (skip LLM debates)
      Tier 3.5 (attempt 4): Send diagnostic package to OpenClaw for code-level fix
      Tier 4 (attempt 5+): ESCALATE to human - pause router + alert Salim
    """
    try:
        # PRIORITY 1: Check lease file (deterministic truth)
        lease_path = STATE_DIR / "leases" / "signal_router.json"
        if lease_path.exists():
            try:
                lease = json.load(open(lease_path))
                heartbeat_at = lease.get("heartbeat_at", lease.get("started_at"))
                ttl = lease.get("ttl_seconds", 720)
                
                heartbeat_dt = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                age_sec = (now - heartbeat_dt).total_seconds()
                
                # If lease is fresh, router is HEALTHY - clear any escalations
                if age_sec < ttl:
                    _clear_escalation_artifacts("signal_router")
                    return []  # No issues
            except Exception as e:
                _log(f"Lease check failed: {e}", "WARNING")
        
        # FALLBACK: Use signal_router_heartbeat.json (router-specific liveness file)
        heartbeat_file = STATE_DIR / "signal_router_heartbeat.json"
        if not heartbeat_file.exists():
            return []  # No heartbeat yet, router might not have run
        
        try:
            heartbeat = json.load(open(heartbeat_file))
            last_run_str = heartbeat.get("timestamp")
            heartbeat_status = heartbeat.get("status", "unknown")
        except Exception as e:
            _log(f"Heartbeat read failed: {e}", "WARNING")
            return []
        
        if not last_run_str:
            return []
        
        # Get daily run count from router state (for 200-run limit check)
        state_file = STATE_DIR / "signal_router_state.json"
        daily_runs = 0
        if state_file.exists():
            try:
                state = json.load(open(state_file))
                daily_runs = state.get("daily_pipeline_runs", 0)
            except:
                pass
        
        # Parse ISO timestamp
        last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc).replace(tzinfo=last_run.tzinfo) - last_run).total_seconds() / 60
        
        if age_minutes > ROUTER_STALL_MIN and daily_runs < 200:
            _log(f"Router stalled: last run {age_minutes:.0f}min ago, {daily_runs}/200 runs", "WARNING")
            
            # Analyze context to understand WHERE it stalled
            context = _analyze_router_context()
            context_str = ""
            if context:
                parts = []
                if context.get("last_token"):
                    parts.append(f"token={context['last_token']}")
                if context.get("last_stage"):
                    parts.append(f"stage={context['last_stage']}")
                if context.get("hints"):
                    parts.append(f"hints={','.join(context['hints'])}")
                if parts:
                    context_str = f" ({' | '.join(parts)})"
            
            # Track attempts for ADAPTIVE ESCALATION
            attempts = _track_attempts("router_stall")
            
            if attempts == 1:
                # 1st attempt: Standard kill + restart
                _kill_process("signal_router.py")
                _remove_lock(STATE_DIR / "signal_window.lock")
                
                _log_action(
                    component="signal_router",
                    problem=f"stalled_{int(age_minutes)}min{context_str}",
                    action="kill_process+clear_lock",
                    result="success",
                    attempts=attempts
                )
                
                context_msg = f"\n• Context:{context_str}" if context_str else ""
                
                _alert(
                    f"🔧 WATCHDOG AUTO-FIX (attempt {attempts}/4):\n"
                    f"• Problem: Router stalled ({int(age_minutes)}min since last run){context_msg}\n"
                    f"• Action: Killed process + cleared lock\n"
                    f"• Result: ✅ Restarted (cron will pick up next cycle)",
                    ALERT_LEVEL_INFO
                )
                
                # DON'T reset attempts - let it accumulate if problem persists
                # _reset_attempts("router_stall")  # REMOVED - was preventing escalation
                return []
            
            elif attempts == 2:
                # 2nd attempt: Force manual run to verify completion
                _kill_process("signal_router.py")
                _remove_lock(STATE_DIR / "signal_window.lock")
                
                success, duration, error = _run_script("signal_router.py", timeout=300)
                
                if success:
                    _log_action(
                        component="signal_router",
                        problem=f"stalled_{int(age_minutes)}min",
                        action="kill+force_manual_run",
                        result="success",
                        duration_sec=int(duration),
                        attempts=attempts
                    )
                    
                    _alert(
                        f"🔧 WATCHDOG AUTO-FIX (attempt {attempts}/4):\n"
                        f"• Problem: Router stalled again ({int(age_minutes)}min)\n"
                        f"• Action: Killed + force manual run\n"
                        f"• Result: ✅ Completed successfully ({duration:.1f}s)",
                        ALERT_LEVEL_INFO
                    )
                    
                    # DON'T reset - let it escalate if problem continues
                    # _reset_attempts("router_stall")  # REMOVED
                    return []
                else:
                    _log_action(
                        component="signal_router",
                        problem=f"stalled_{int(age_minutes)}min",
                        action="kill+force_manual_run",
                        result="failed",
                        duration_sec=int(duration),
                        attempts=attempts,
                        error=error
                    )
                    
                    return [f"Router stall (attempt {attempts}: manual run failed)"]
            
            elif attempts == 3:
                # 3rd attempt: Emergency fast-path mode (skip LLM debate)
                _kill_process("signal_router.py")
                _remove_lock(STATE_DIR / "signal_window.lock")
                
                # Create flag file for fast-path mode
                fast_path_flag = STATE_DIR / "router_fast_path.flag"
                fast_path_flag.write_text(f"emergency_mode: watchdog attempt {attempts} at {datetime.now(timezone.utc).isoformat()}Z")
                
                _log_action(
                    component="signal_router",
                    problem=f"stalled_{int(age_minutes)}min",
                    action="kill+enable_fast_path",
                    result="success",
                    attempts=attempts
                )
                
                _alert(
                    f"⚠️ WATCHDOG ESCALATION (attempt {attempts}/4):\n"
                    f"• Problem: Router stalled AGAIN ({int(age_minutes)}min)\n"
                    f"• Action: Enabled FAST-PATH mode (skip LLM debates)\n"
                    f"• Next: If this fails, I'll pause router and escalate to you",
                    ALERT_LEVEL_WARNING
                )
                
                # DON'T reset - attempt 4 must escalate
                # _reset_attempts("router_stall")  # REMOVED
                return []
            
            elif attempts == 4:
                # Tier 3.5: Send diagnostic package to OpenClaw for code-level fix
                _kill_process("signal_router.py")
                _remove_lock(STATE_DIR / "signal_window.lock")
                
                # Compile diagnostic package
                diagnostic = _compile_diagnostic_package(context, age_minutes)
                
                # Send to OpenClaw main session
                openclaw_result = _escalate_to_openclaw(diagnostic)
                
                _log_action(
                    component="signal_router",
                    problem=f"stalled_{int(age_minutes)}min{context_str}",
                    action="escalate_to_openclaw",
                    result="sent" if openclaw_result else "failed",
                    attempts=attempts
                )
                
                if openclaw_result:
                    _alert(
                        f"🤖 WATCHDOG → OPENCLAW (attempt {attempts}/5):\n"
                        f"• Problem: Router stalled repeatedly{context_str}\n"
                        f"• Watchdog tried: Kill+restart, force run, fast-path mode\n"
                        f"• Action: Sent diagnostic package to OpenClaw for code-level fix\n"
                        f"• OpenClaw has 30min to diagnose and patch\n"
                        f"• If unresolved, will escalate to Salim",
                        ALERT_LEVEL_WARNING
                    )
                else:
                    _alert(
                        f"⚠️ WATCHDOG ESCALATION FAILED (attempt {attempts}):\n"
                        f"• Problem: Router stalled, couldn't reach OpenClaw\n"
                        f"• Next: Will escalate directly to Salim on next failure",
                        ALERT_LEVEL_WARNING
                    )
                
                return [f"Router stalled (sent to OpenClaw for diagnosis)"]
            
            else:
                # Tier 4 (attempt 5+): ESCALATE to human - pause router and alert Salim
                _kill_process("signal_router.py")
                _remove_lock(STATE_DIR / "signal_window.lock")
                
                # Create pause flag (router should check this and exit early)
                pause_flag = STATE_DIR / "router_paused.flag"
                pause_until = datetime.now(timezone.utc) + timedelta(minutes=30)
                pause_flag.write_text(f"paused_until: {pause_until.isoformat()}Z\nreason: watchdog_escalation_tier4_attempt_{attempts}")
                
                # Compile full history for human
                diagnostic = _compile_diagnostic_package(context, age_minutes)
                history = _get_escalation_history()
                
                _log_action(
                    component="signal_router",
                    problem=f"stalled_{int(age_minutes)}min{context_str}",
                    action="pause_router_alert_human",
                    result="escalated",
                    attempts=attempts,
                    escalated=True
                )
                
                _alert(
                    f"🚨 SALIM: HUMAN INTERVENTION NEEDED (Tier 4):\n"
                    f"\n📊 PROBLEM:\n"
                    f"  Router stalled repeatedly ({int(age_minutes)}min){context_str}\n"
                    f"\n🔧 WATCHDOG TRIED:\n"
                    f"  • Tier 1: Kill + restart\n"
                    f"  • Tier 2: Force manual run\n"
                    f"  • Tier 3: Fast-path mode (skip LLM debates)\n"
                    f"\n🤖 OPENCLAW TRIED:\n"
                    f"{history}\n"
                    f"\n❌ RESULT:\n"
                    f"  All automated fixes failed\n"
                    f"\n⏸️ ACTION TAKEN:\n"
                    f"  PAUSED router until {pause_until.strftime('%H:%M UTC')}\n"
                    f"\n🔍 DIAGNOSTIC DATA:\n"
                    f"{diagnostic}\n"
                    f"\n📂 CHECK:\n"
                    f"  logs/signal_router.log (last 50 lines)\n"
                    f"  genius-memory/watchdog-actions/actions.jsonl",
                    ALERT_LEVEL_CRITICAL
                )
                
                _reset_attempts("router_stall")
                return [f"Router stalled (PAUSED - human needed)"]
            
            return [f"Router stalled {age_minutes:.0f}min (auto-fix attempt {attempts})"]
        else:
            # Router is healthy - reset attempts if they exist
            if "router_stall" in _fix_attempts and _fix_attempts["router_stall"]["count"] > 0:
                _log(f"Router healthy again - resetting {_fix_attempts['router_stall']['count']} attempt(s)")
                _reset_attempts("router_stall")
    
    except Exception as e:
        _log(f"Router stall check failed: {e}", "ERROR")
    
    return []


def check_position_freshness():
    """Check if open positions have stale prices (>5 min old)."""
    try:
        # Load portfolio from SQLite (single source of truth)
        if HAS_STATE_STORE:
            try:
                portfolio = state_store.get_portfolio()
            except Exception as e:
                _log(f"state_store.get_portfolio failed: {e}, using JSON fallback")
                portfolio_file = STATE_DIR / "portfolio.json"
                if not portfolio_file.exists():
                    return []
                portfolio = json.load(open(portfolio_file))
        else:
            portfolio_file = STATE_DIR / "portfolio.json"
            if not portfolio_file.exists():
                return []
            portfolio = json.load(open(portfolio_file))
        
        positions = portfolio.get("positions", {})
        
        stale = []
        for token, pos in positions.items():
            updated_str = pos.get("current_price_updated")
            if not updated_str:
                continue
            
            updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc).replace(tzinfo=updated.tzinfo) - updated).total_seconds() / 60
            
            if age_minutes > POSITION_FRESHNESS_MIN:
                stale.append(f"{token} ({age_minutes:.0f}min old)")
        
        if stale:
            _log(f"Stale position prices: {stale}", "WARNING")
            
            # Fix: force position monitor
            success, duration, error = _run_script("position_monitor.py", timeout=45)
            
            if success:
                _log_action(
                    component="position_monitor",
                    problem=f"stale_prices_{len(stale)}",
                    action="force_rerun",
                    result="success",
                    duration_sec=int(duration),
                    attempts=1
                )
                
                _alert(
                    f"🔧 WATCHDOG AUTO-FIX:\n"
                    f"• Problem: {len(stale)} position(s) with stale prices\n"
                    f"• Action: Force-ran position_monitor.py\n"
                    f"• Result: ✅ Refreshed ({duration:.1f}s)",
                    ALERT_LEVEL_INFO
                )
            else:
                _log_action(
                    component="position_monitor",
                    problem=f"stale_prices_{len(stale)}",
                    action="force_rerun",
                    result="failed",
                    duration_sec=int(duration),
                    attempts=1,
                    error=error
                )
                
                _alert(
                    f"🚨 WATCHDOG: Position monitor failed to refresh\n"
                    f"• Error: {error}",
                    ALERT_LEVEL_WARNING
                )
            
            return stale
    
    except Exception as e:
        _log(f"Position freshness check failed: {e}", "ERROR")
    
    return []


def check_data_freshness():
    """Check if scanners have fresh data using LEASE-BASED TRUTH first."""
    issues = []
    try:
        # PRIORITY 1: Check leases (deterministic truth)
        lease_dir = STATE_DIR / "leases"
        scanner_leases = {
            "coingecko": {"name": "coingecko_scanner", "ttl": 300},
            "onchain": {"name": "onchain_analytics", "ttl": 600},
            "dex": {"name": "dex_scanner", "ttl": 300}
        }
        
        for scanner_key, lease_info in scanner_leases.items():
            lease_path = lease_dir / f"{lease_info['name']}.json"
            if lease_path.exists():
                try:
                    lease = json.load(open(lease_path))
                    heartbeat_at = lease.get("heartbeat_at", lease.get("started_at"))
                    ttl = lease.get("ttl_seconds", lease_info["ttl"])
                    
                    heartbeat_dt = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    age_sec = (now - heartbeat_dt).total_seconds()
                    
                    # If lease is fresh, scanner is HEALTHY
                    if age_sec < ttl:
                        _log(f"{scanner_key} lease fresh ({age_sec:.0f}s < {ttl}s TTL)", "DEBUG")
                        continue  # Skip to next scanner
                    else:
                        _log(f"{scanner_key} lease STALE ({age_sec:.0f}s > {ttl}s TTL)", "WARNING")
                except Exception as e:
                    _log(f"Lease check failed for {scanner_key}: {e}", "WARNING")
        
        # FALLBACK: Check onchain heartbeat (scanner writes this EVERY run, even if 0 signals)
        onchain_heartbeat = BASE_DIR / "signals" / "onchain" / "_heartbeat.json"
        if onchain_heartbeat.exists():
            heartbeat_data = json.load(open(onchain_heartbeat))
            last_run_str = heartbeat_data.get("last_run", "")
            if last_run_str:
                try:
                    last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - last_run).total_seconds() / 60
                    if age_min > 30:  # Onchain should run every 15min
                        issues.append(f"onchain ({age_min:.0f}min old)")
                        _log(f"Onchain heartbeat STALE: {age_min:.0f}min", "WARNING")
                        
                        # Tier 2 intervention: force rerun
                        _log("Tier 2: Force rerunning onchain_analytics.py...")
                        try:
                            result = subprocess.run(
                                ["python3", str(SCRIPTS_DIR / "onchain_analytics.py")],
                                timeout=120,
                                capture_output=True
                            )
                            if result.returncode == 0:
                                _log("Onchain analytics rerun: SUCCESS")
                                _log_action("onchain", f"stale_{age_min:.0f}min", "force_rerun", "success", attempts=1)
                            else:
                                _log(f"Onchain analytics rerun FAILED: {result.stderr[:200]}")
                        except Exception as e:
                            _log(f"Onchain rerun failed: {e}")
                except Exception as e:
                    _log(f"Onchain heartbeat parse error: {e}", "ERROR")
        else:
            # No heartbeat file yet - force initial run
            _log("Onchain heartbeat missing - forcing initial run", "WARNING")
            try:
                subprocess.run(
                    ["python3", str(SCRIPTS_DIR / "onchain_analytics.py")],
                    timeout=120,
                    capture_output=True
                )
            except Exception as e:
                _log(f"Onchain initial run failed: {e}")
        
        # Check scanner output files directly (not signal_window which has rolling history)
        from collections import defaultdict
        import os
        
        signals_dir = BASE_DIR / "signals"
        scanner_dirs = {
            "coingecko": signals_dir / "coingecko",
            "dexscreener": signals_dir / "dexscreener", 
            "birdeye": signals_dir / "birdeye"
        }
        
        now = time.time()
        source_ages = {}
        
        for source_name, source_dir in scanner_dirs.items():
            if not source_dir.exists():
                continue
            
            # Find newest file (exclude global_latest.json)
            files = [f for f in source_dir.glob("*.json") if f.name != "global_latest.json"]
            if not files:
                continue
            
            newest_file = max(files, key=lambda f: f.stat().st_mtime)
            age_minutes = (now - newest_file.stat().st_mtime) / 60
            source_ages[source_name] = age_minutes
        
        # Check age - only alert if >20min (scanners run every 5min, allow 4x buffer)
        for source, age_minutes in source_ages.items():
            if age_minutes > 20:  # Changed from 15 to 20min
                issues.append(f"{source} ({age_minutes:.0f}min old)")
                _log(f"Data source stale: {source} ({age_minutes:.0f}min)", "WARNING")
    
    except Exception as e:
        _log(f"Data freshness check failed: {e}", "ERROR")
    
    if issues:
        _alert(
            f"⚠️ WATCHDOG: Stale data sources detected\n"
            f"• Sources: {', '.join(issues)}\n"
            f"• Note: This usually means scanners haven't run or are failing",
            ALERT_LEVEL_WARNING
        )
    
    return issues


def check_stale_locks():
    """Remove any .lock files older than 10 minutes."""
    removed = []
    try:
        lock_files = list(STATE_DIR.glob("*.lock"))
        now = time.time()
        
        for lock in lock_files:
            age_minutes = (now - lock.stat().st_mtime) / 60
            if age_minutes > LOCK_AGE_MIN:
                _remove_lock(lock)
                removed.append(f"{lock.name} ({age_minutes:.0f}min old)")
    
    except Exception as e:
        _log(f"Lock check failed: {e}", "ERROR")
    
    if removed:
        _log_action(
            component="locks",
            problem=f"stale_locks_{len(removed)}",
            action="remove",
            result="success",
            attempts=1
        )
        
        _alert(
            f"🔧 WATCHDOG AUTO-FIX:\n"
            f"• Problem: {len(removed)} stale lock file(s)\n"
            f"• Action: Removed locks\n"
            f"• Result: ✅ Cleared",
            ALERT_LEVEL_INFO
        )
    
    return removed


def check_zombie_processes():
    """Kill any trading script running >600s."""
    killed = []
    try:
        patterns = [
            "signal_router.py",
            "sanad_pipeline.py",
            "position_monitor.py",
            "reconciliation.py"
        ]
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                cmdline = " ".join(proc.info['cmdline'] or [])
                
                for pattern in patterns:
                    if pattern in cmdline:
                        runtime = time.time() - proc.info['create_time']
                        
                        if runtime > LONG_RUNNING_PROCESS_SEC:
                            proc.kill()
                            killed.append(f"{pattern} (PID {proc.info['pid']}, {runtime:.0f}s)")
                            _log(f"Killed zombie: {pattern} (PID {proc.info['pid']})", "WARNING")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    
    except Exception as e:
        _log(f"Zombie check failed: {e}", "ERROR")
    
    if killed:
        _log_action(
            component="processes",
            problem=f"zombie_processes_{len(killed)}",
            action="kill",
            result="success",
            attempts=1
        )
        
        _alert(
            f"🔧 WATCHDOG AUTO-FIX:\n"
            f"• Problem: {len(killed)} zombie process(es)\n"
            f"• Action: Killed processes\n"
            f"• Details: {', '.join(killed)}\n"
            f"• Result: ✅ Cleaned",
            ALERT_LEVEL_WARNING
        )
    
    return killed


def check_cost_runaway():
    """Check actual daily spend from api_costs.jsonl (not just daily_cost.json)."""
    try:
        costs_log = STATE_DIR / "api_costs.jsonl"
        if not costs_log.exists():
            return []
        
        # Calculate actual 24h spend from api_costs.jsonl
        now = datetime.now(timezone.utc)
        last_24h = now - timedelta(hours=24)
        total_24h = 0.0
        
        with open(costs_log) as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                if ts >= last_24h:  # Both are now timezone-aware
                    total_24h += entry["cost_usd"]
        
        # Thresholds for paper mode
        WARNING_THRESHOLD = 15.0
        CRITICAL_THRESHOLD = 30.0
        
        if total_24h >= CRITICAL_THRESHOLD:
            _log(f"CRITICAL: 24h cost ${total_24h:.2f} >= ${CRITICAL_THRESHOLD}", "CRITICAL")
            _alert(
                f"🚨 WATCHDOG CRITICAL: Daily cost overrun\n"
                f"• 24h spend: ${total_24h:.2f}\n"
                f"• Threshold: ${CRITICAL_THRESHOLD}\n"
                f"• Action: Review api_costs.jsonl for untracked calls",
                ALERT_LEVEL_CRITICAL
            )
            return [f"CRITICAL: ${total_24h:.2f} >= ${CRITICAL_THRESHOLD}"]
        
        elif total_24h >= WARNING_THRESHOLD:
            _log(f"WARNING: 24h cost ${total_24h:.2f} >= ${WARNING_THRESHOLD}", "WARNING")
            _alert(
                f"⚠️ WATCHDOG WARNING: Daily cost high\n"
                f"• 24h spend: ${total_24h:.2f}\n"
                f"• Warning threshold: ${WARNING_THRESHOLD}\n"
                f"• Critical threshold: ${CRITICAL_THRESHOLD}",
                ALERT_LEVEL_WARNING
            )
            return [f"WARNING: ${total_24h:.2f} >= ${WARNING_THRESHOLD}"]
    
    except Exception as e:
        _log(f"Cost check failed: {e}", "ERROR")
    
    return []


def check_disk_memory():
    """Check disk and memory usage."""
    issues = []
    try:
        # Disk
        disk = psutil.disk_usage(str(BASE_DIR))
        disk_pct = disk.percent / 100
        
        if disk_pct > DISK_WARNING_PCT:
            issues.append(f"Disk {disk_pct*100:.0f}%")
            _log(f"Disk usage high: {disk_pct*100:.0f}%", "WARNING")
            
            # Clean old logs (>7 days)
            week_ago = time.time() - (7 * 86400)
            cleaned = []
            for log_file in LOGS_DIR.glob("*.log"):
                if log_file.stat().st_mtime < week_ago:
                    log_file.unlink()
                    cleaned.append(log_file.name)
                    _log(f"Cleaned old log: {log_file.name}")
            
            if cleaned:
                _alert(
                    f"🔧 WATCHDOG AUTO-FIX:\n"
                    f"• Problem: Disk usage {disk_pct*100:.0f}%\n"
                    f"• Action: Cleaned {len(cleaned)} old log file(s)\n"
                    f"• Result: ✅ Freed space",
                    ALERT_LEVEL_INFO
                )
        
        # Memory
        mem = psutil.virtual_memory()
        mem_pct = mem.percent / 100
        
        if mem_pct > MEMORY_WARNING_PCT:
            issues.append(f"Memory {mem_pct*100:.0f}%")
            _log(f"Memory usage high: {mem_pct*100:.0f}%", "WARNING")
    
    except Exception as e:
        _log(f"Disk/memory check failed: {e}", "ERROR")
    
    if issues:
        _alert(
            f"⚠️ WATCHDOG: Resource warning\n"
            f"• Issues: {', '.join(issues)}\n"
            f"• Note: Monitor closely",
            ALERT_LEVEL_WARNING
        )
    
    return issues


def check_stuck_openclaw_jobs():
    """
    Auto-fix OpenClaw jobs stuck in runningAtMs state.
    
    This is the enterprise-grade fix for the scheduler bug where jobs
    get stuck in "running" state even after completion/timeout.
    
    Strategy:
    1. Check critical jobs for stuck runningAtMs
    2. Auto disable→enable to clear scheduler state
    3. Only escalate if auto-fix fails twice
    """
    issues = []
    fixed = []
    
    # Critical jobs with their expected timeouts
    critical_jobs = {
        "Signal Router": {
            "id": "00079d3a-0206-4afc-9dd9-8263521e1bf3",
            "timeout": 600,
            "grace": 120  # Extra grace period
        },
        "CoinGecko Scanner": {
            "id": "3a7f742b-889a-4c05-9697-f5f873fea02c",
            "timeout": 60,
            "grace": 60
        },
        "On-Chain Analytics": {
            "id": "0d84f5fc-1e9f-4480-96c9-c27369db1259",
            "timeout": 90,
            "grace": 60
        },
        "DEX Scanner": {
            "id": "c8e7bf57-9014-4e04-83f8-9bc758485b34",
            "timeout": 120,
            "grace": 60
        },
        "Price Snapshot": {
            "id": "4eea530c-3db5-4cdc-904a-76583704dccd",
            "timeout": 60,
            "grace": 60
        }
    }
    
    try:
        # Get job states from OpenClaw
        result = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            _log(f"Failed to get cron list: {result.stderr}", "ERROR")
            return issues
        
        cron_data = json.loads(result.stdout)
        jobs = cron_data.get("jobs", [])
        
        now_ms = time.time() * 1000
        
        for job_name, config in critical_jobs.items():
            job_id = config["id"]
            timeout = config["timeout"]
            grace = config["grace"]
            max_age_ms = (timeout + grace) * 1000
            
            # Find this job in the list
            job = next((j for j in jobs if j["id"] == job_id), None)
            if not job:
                continue
            
            state = job.get("state", {})
            running_at_ms = state.get("runningAtMs")
            
            if not running_at_ms:
                continue  # Job not stuck
            
            # Calculate how long it's been stuck
            stuck_age_ms = now_ms - running_at_ms
            stuck_age_sec = stuck_age_ms / 1000
            
            if stuck_age_ms > max_age_ms:
                # Job is stuck!
                _log(f"Detected stuck job: {job_name} (stuck for {stuck_age_sec:.0f}s, max={timeout+grace}s)", "WARNING")
                
                attempt_key = f"stuck_openclaw_{job_name.lower().replace(' ', '_')}"
                attempts = _track_attempts(attempt_key)
                
                # ALWAYS queue reset request for Reset Daemon (unlimited retries)
                try:
                    _log(f"Queueing reset for {job_name} (attempt {attempts+1})", "INFO")
                    
                    reset_request = {
                        "job_id": job_id,
                        "job_name": job_name,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                        "reason": f"stuck in runningAtMs for {stuck_age_sec:.0f}s (max {timeout+grace}s)",
                        "attempt": attempts + 1
                    }
                    
                    # Append to reset queue
                    reset_queue = STATE_DIR / "reset_requests.jsonl"
                    with open(reset_queue, "a") as f:
                        f.write(json.dumps(reset_request) + "\n")
                    
                    fixed.append(f"{job_name} (queued reset)")
                    
                    _log_action(
                        component="openclaw_scheduler",
                        problem=f"{job_name}_stuck_{stuck_age_sec:.0f}s",
                        action="queue_reset",
                        result="success",
                        attempts=attempts + 1
                    )
                    
                    _log(f"Reset request queued for {job_name}", "INFO")
                    
                except Exception as e:
                    _log(f"Failed to queue reset for {job_name}: {e}", "ERROR")
                    issues.append(f"{job_name} (queue failed)")
                
                # Escalate ONLY after many attempts (but keep queueing resets)
                if attempts > 5:
                    issues.append(f"{job_name} (stuck {stuck_age_sec:.0f}s, 5+ reset attempts)")
                    _log(f"Escalating {job_name}: 5+ reset attempts, still stuck", "ERROR")
        
        # Success notification
        if fixed:
            _alert(
                f"🔧 WATCHDOG AUTO-FIX:\n"
                f"• Problem: {len(fixed)} OpenClaw job(s) stuck in runningAtMs\n"
                f"• Action: disable→enable to clear scheduler state\n"
                f"• Fixed: {', '.join(fixed)}\n"
                f"• Result: ✅ Jobs should resume",
                ALERT_LEVEL_INFO
            )
            
            # Clear attempt counters on success
            for job_name in critical_jobs.keys():
                attempt_key = f"stuck_openclaw_{job_name.lower().replace(' ', '_')}"
                _reset_attempts(attempt_key)
        
        # Escalation for persistent failures (5+ attempts)
        if issues:
            _alert(
                f"🚨 WATCHDOG CRITICAL:\n"
                f"• Problem: OpenClaw jobs stuck despite Reset Daemon\n"
                f"• Jobs: {', '.join(issues)}\n"
                f"• Attempted: Reset Daemon auto-fix (5+ attempts)\n"
                f"• Impact: Jobs not dispatching, stale data\n"
                f"• Next: Manual intervention or OpenClaw restart needed",
                ALERT_LEVEL_CRITICAL
            )
    
    except subprocess.TimeoutExpired:
        _log("Timeout getting cron list", "ERROR")
        issues.append("cron list timeout")
    except json.JSONDecodeError as e:
        _log(f"Failed to parse cron list JSON: {e}", "ERROR")
        issues.append("cron parse error")
    except Exception as e:
        _log(f"Stuck OpenClaw jobs check failed: {e}", "ERROR")
        issues.append(f"check error: {e}")
    
    return issues


# --- MAIN ---

def run():
    """Run all watchdog checks."""
    # Load persisted attempt counters FIRST
    _load_attempts()
    
    _log("=== WATCHDOG START ===")
    
    all_issues = {}
    
    checks = [
        ("Lease + Output Staleness", check_lease_and_output_staleness),  # ENTERPRISE: catches "phantom OK"
        ("Stuck OpenClaw Jobs", check_stuck_openclaw_jobs),  # PRIORITY: auto-fix scheduler bugs
        ("Reconciliation Staleness", check_reconciliation_staleness),  # NEW: critical for Gate 11
        ("DexScreener Freshness", check_dexscreener_freshness),  # NEW: critical for 3-source corroboration
        ("Cron Health", check_cron_health),
        ("Router Stall", check_router_stall),
        ("Position Freshness", check_position_freshness),
        ("Data Freshness", check_data_freshness),
        ("Stale Locks", check_stale_locks),
        ("Zombie Processes", check_zombie_processes),
        ("Cost Runaway", check_cost_runaway),
        ("Disk/Memory", check_disk_memory)
    ]
    
    for check_name, check_func in checks:
        try:
            issues = check_func()
            if issues:
                all_issues[check_name] = issues
                _log(f"{check_name}: {len(issues)} issue(s)")
        except Exception as e:
            _log(f"{check_name} check crashed: {e}", "ERROR")
    
    if not all_issues:
        _log("=== WATCHDOG: ALL CLEAR ===")
    else:
        _log(f"=== WATCHDOG: {len(all_issues)} CHECK(S) TRIGGERED ===")
        for check, issues in all_issues.items():
            _log(f"  {check}: {issues}")


# ---------------------------------------------------------------------------
# Enterprise Lease + Output Staleness Check
# ---------------------------------------------------------------------------

def _latest_mtime_seconds(glob_pattern: str) -> float | None:
    """Get age in seconds of the newest file matching pattern."""
    import glob
    files = glob.glob(glob_pattern)
    if not files:
        return None
    newest = max(files, key=os.path.getmtime)
    return time.time() - os.path.getmtime(newest)


def _lease_age_seconds(path: Path) -> float | None:
    """Get age in seconds of the lease file."""
    if not path.exists():
        return None
    try:
        data = json.load(open(path))
        # Prefer completed_at, fallback to heartbeat_at
        ts = data.get("completed_at") or data.get("heartbeat_at")
        if not ts:
            return None
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception as e:
        _log(f"Lease age parse error for {path}: {e}", "WARNING")
        return None


def _queue_reset_for_stale_job(job_id: str, job_name: str, reason: str):
    """Queue a reset request for Reset Daemon to process."""
    req = {
        "job_id": job_id,
        "job_name": job_name,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    q = STATE_DIR / "reset_requests.jsonl"
    with open(q, "a") as f:
        f.write(json.dumps(req) + "\n")
    _log(f"Queued reset for {job_name}: {reason}", "INFO")


def check_lease_and_output_staleness():
    """
    Enterprise truth: if BOTH lease AND outputs are stale → queue reset.
    
    This catches "phantom OK" where OpenClaw claims the job ran but
    no lease or output files were actually created.
    
    Runs continuously - always queues resets when stale, no "give up" logic.
    """
    issues = []
    
    jobs = [
        {
            "name": "CoinGecko Scanner",
            "id": "3a7f742b-889a-4c05-9697-f5f873fea02c",
            "lease": STATE_DIR / "leases" / "coingecko_scanner.json",
            "outputs": str(BASE_DIR / "signals" / "coingecko" / "*.json"),
            "ttl_sec": 420,  # 7min for a 5min cron (2min grace)
        },
        {
            "name": "On-Chain Analytics",
            "id": "0d84f5fc-1e9f-4480-96c9-c27369db1259",
            "lease": STATE_DIR / "leases" / "onchain_analytics.json",
            "outputs": str(BASE_DIR / "signals" / "onchain" / "_heartbeat.json"),
            "ttl_sec": 900,  # 15min for a 10min cron (5min grace)
        },
        {
            "name": "DEX Scanner",
            "id": "c8e7bf57-9014-4e04-83f8-9bc758485b34",
            "lease": STATE_DIR / "leases" / "dex_scanner.json",
            "outputs": str(BASE_DIR / "signals" / "dexscreener" / "*.json"),
            "ttl_sec": 420,  # 7min for a 5min cron
        },
        {
            "name": "Birdeye Scanner",
            "id": "76592dfe-7831-4a80-aa30-760618ca049b",
            "lease": STATE_DIR / "leases" / "birdeye_scanner.json",
            "outputs": str(BASE_DIR / "signals" / "birdeye" / "*.json"),
            "ttl_sec": 420,  # 7min for a 5min cron
        },
        {
            "name": "Birdeye DEX Scanner",
            "id": "3729bd4c-2f8c-458f-9d93-4784233550bb",
            "lease": STATE_DIR / "leases" / "birdeye_dex_scanner.json",
            "outputs": str(BASE_DIR / "signals" / "birdeye" / "*.json"),
            "ttl_sec": 420,  # 7min for a 5min cron
        },
        # Router uses its own monitoring in check_router_stall()
    ]
    
    for j in jobs:
        lease_age = _lease_age_seconds(j["lease"])
        out_age = _latest_mtime_seconds(j["outputs"])
        
        # If either missing, treat as stale
        lease_stale = (lease_age is None) or (lease_age > j["ttl_sec"])
        out_stale = (out_age is None) or (out_age > j["ttl_sec"])
        
        if lease_stale and out_stale:
            # BOTH stale - job is truly not running
            reason = f"lease_age={lease_age if lease_age else 'missing'}s output_age={out_age if out_age else 'missing'}s ttl={j['ttl_sec']}s"
            _queue_reset_for_stale_job(j["id"], j["name"], reason)
            issues.append(f"{j['name']} stale (queued reset)")
            _log(f"STALE JOB DETECTED: {j['name']} - {reason}", "WARNING")
        elif lease_stale or out_stale:
            # Only one stale - might be mid-run or transient
            _log(f"{j['name']}: lease_stale={lease_stale} ({lease_age}s), out_stale={out_stale} ({out_age}s) - monitoring", "DEBUG")
    
    return issues


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        _log("Interrupted", "WARNING")
    except Exception as e:
        _log(f"WATCHDOG CRASHED: {e}", "CRITICAL")
        _alert(f"🚨 WATCHDOG CRASHED: {e}", ALERT_LEVEL_CRITICAL)
        import traceback
        traceback.print_exc()
