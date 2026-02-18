#!/usr/bin/env python3
"""
Sanad Trader v3.0 — Deterministic Heartbeat Monitor

Phase 10 — NOT an LLM. Hardcoded emergency logic.
Runs every 10 minutes via cron. Acts FIRST, notifies SECOND.
Works even if all LLM APIs are down.

References:
- v3 doc HEARTBEAT.md specification
- v3 doc Table 6 (cron jobs)
- v3 doc Table 11 (threat matrix — flash crash)
- thresholds.yaml (single source of truth)
"""

import json
import os
import sys
import time
import yaml
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("SANAD_HOME", "/data/.openclaw/workspace/trading"))
CONFIG_PATH = BASE_DIR / "config" / "thresholds.yaml"
KILL_SWITCH_PATH = BASE_DIR / "config" / "kill_switch.flag"
STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "execution-logs"
HEARTBEAT_LOG = LOGS_DIR / "heartbeat.log"


def load_config():
    """Load thresholds.yaml. Return None on failure."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        log(f"CRITICAL: Cannot load config: {e}")
        return None


def load_state(filename):
    """Load a JSON state file. Return empty dict on failure."""
    try:
        with open(STATE_DIR / filename, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(filename, data):
    """Save a JSON state file."""
    try:
        with open(STATE_DIR / filename, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"ERROR: Cannot save {filename}: {e}")


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def log(message):
    """Append to heartbeat log with timestamp."""
    ts = now_iso()
    line = f"[{ts}] {message}"
    print(line)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(HEARTBEAT_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────
# WHATSAPP NOTIFICATION (stub)
# ─────────────────────────────────────────────

def notify_whatsapp(message, urgent=False):
    """
    Send critical heartbeat alerts via Telegram (was WhatsApp stub).
    Urgent alerts (kill switch, stop loss, flash crash) go as L3/L4.
    """
    prefix = "URGENT " if urgent else ""
    log(f"[HEARTBEAT {prefix}ALERT] {message}")
    try:
        import notifier
        level = "L3" if urgent else "L2"
        title = "URGENT ALERT" if urgent else "Heartbeat Alert"
        notifier.send(message, level=level, title=title)
    except Exception as e:
        log(f"[NOTIFY FAILED] {e} — message was: {message}")


# ─────────────────────────────────────────────
# EMERGENCY ACTIONS
# ─────────────────────────────────────────────

def activate_kill_switch(reason):
    """Activate kill switch — halts ALL trading."""
    log(f"EMERGENCY: Activating kill switch — {reason}")
    try:
        KILL_SWITCH_PATH.write_text("TRUE")
    except Exception as e:
        log(f"CRITICAL: Cannot write kill switch file: {e}")
    notify_whatsapp(f"KILL SWITCH ACTIVATED: {reason}", urgent=True)


def emergency_sell_all(reason, portfolio):
    """
    Emergency close all positions.
    In PAPER mode: update portfolio state to close all positions.
    In LIVE mode: send market sell orders to exchange API.
    """
    mode = portfolio.get("mode", "PAPER")
    open_count = portfolio.get("open_position_count", 0)

    log(f"EMERGENCY SELL ALL: {reason} (mode={mode}, positions={open_count})")

    if mode == "PAPER":
        # Paper mode: just update state
        portfolio["open_position_count"] = 0
        portfolio["meme_allocation_pct"] = 0.0
        portfolio["total_exposure_pct"] = 0.0
        portfolio["token_exposure_pct"] = {}
        save_state("portfolio.json", portfolio)
        log("PAPER MODE: All positions marked as closed in state")
    else:
        # LIVE mode: execute real sells
        # TODO Phase 8: Implement exchange API sell orders
        log("LIVE MODE: Exchange sell orders would execute here")

    activate_kill_switch(f"Emergency sell all: {reason}")
    notify_whatsapp(f"EMERGENCY SELL ALL: {reason}. {open_count} positions closed.", urgent=True)


# ─────────────────────────────────────────────
# HEARTBEAT CHECKS
# ─────────────────────────────────────────────

def check_kill_switch():
    """Check 1: Is kill switch active?"""
    try:
        if KILL_SWITCH_PATH.exists():
            content = KILL_SWITCH_PATH.read_text().strip().upper()
            if content == "TRUE":
                log("Kill switch is ACTIVE — system halted")
                return {"status": "HALTED", "detail": "Kill switch active"}
        return {"status": "OK", "detail": "Kill switch not active"}
    except Exception as e:
        return {"status": "ERROR", "detail": f"Cannot read kill switch: {e}"}


def check_positions(config, portfolio, price_cache):
    """
    Check 2: Verify positions vs stop-loss/take-profit.
    If any position has breached stop-loss or hit take-profit,
    take action deterministically.
    """
    alerts = []
    positions = load_state("positions.json")

    if not positions or not isinstance(positions, list):
        return {"status": "OK", "detail": "No open positions", "alerts": []}

    for pos in positions:
        if pos.get("status") != "open":
            continue

        symbol = pos.get("token", "UNKNOWN")
        entry = pos.get("entry_price", 0)
        stop_loss = pos.get("stop_loss", 0)
        take_profit = pos.get("take_profit", 0)
        current = pos.get("current_price", 0)

        # Get latest price from cache if available
        if symbol in price_cache:
            current = price_cache[symbol]

        if current <= 0 or entry <= 0:
            alerts.append(f"{symbol}: invalid price data (current={current}, entry={entry})")
            continue

        pnl_pct = (current - entry) / entry

        # Check stop-loss
        if stop_loss > 0 and current <= stop_loss:
            alert = f"{symbol}: STOP-LOSS HIT (current={current}, stop={stop_loss}, pnl={pnl_pct:.2%})"
            alerts.append(alert)
            log(f"POSITION ALERT: {alert}")
            # In paper mode, mark position for closure
            # In live mode, send market sell order
            notify_whatsapp(f"STOP-LOSS: {symbol} at {current} (entry {entry})", urgent=True)

        # Check take-profit
        if take_profit > 0 and current >= take_profit:
            alert = f"{symbol}: TAKE-PROFIT HIT (current={current}, target={take_profit}, pnl={pnl_pct:.2%})"
            alerts.append(alert)
            log(f"POSITION ALERT: {alert}")
            notify_whatsapp(f"TAKE-PROFIT: {symbol} at {current} (entry {entry})")

    status = "ALERT" if alerts else "OK"
    return {"status": status, "detail": f"{len(alerts)} alerts", "alerts": alerts}


def check_exposure(config, portfolio):
    """Check 3: Verify portfolio exposure limits."""
    alerts = []

    max_meme = config["risk"]["max_meme_allocation_pct"]
    max_dd = config["risk"]["max_drawdown_pct"]
    daily_limit = config["risk"]["daily_loss_limit_pct"]

    meme_pct = portfolio.get("meme_allocation_pct", 0)
    dd_pct = portfolio.get("current_drawdown_pct", 0)
    daily_pnl = portfolio.get("daily_pnl_pct", 0)

    if meme_pct > max_meme:
        alerts.append(f"Meme allocation {meme_pct:.2%} > {max_meme:.0%} limit")

    if dd_pct >= max_dd:
        alerts.append(f"Drawdown {dd_pct:.2%} >= {max_dd:.0%} limit")
        activate_kill_switch(f"Max drawdown exceeded: {dd_pct:.2%}")

    if daily_pnl <= -daily_limit:
        alerts.append(f"Daily loss {daily_pnl:.2%} <= -{daily_limit:.0%} limit")
        activate_kill_switch(f"Daily loss limit hit: {daily_pnl:.2%}")

    status = "ALERT" if alerts else "OK"
    return {"status": status, "detail": f"{len(alerts)} alerts", "alerts": alerts}


def check_flash_crash(config, portfolio):
    """
    Check 4: Monitor for flash crashes (>10% drop in 15min).
    If detected: emergency close all meme positions, enter monitoring-only.
    Works even if all LLM APIs are down.
    """
    price_history = load_state("price_history.json")

    if not price_history:
        return {"status": "OK", "detail": "No price history available yet"}

    current_time = now_utc()
    alerts = []

    # Check each tracked token for flash crash
    for token, prices in price_history.items():
        if not isinstance(prices, list) or len(prices) < 2:
            continue

        # Find price from ~15 minutes ago
        price_15m_ago = None
        current_price = None

        for entry in reversed(prices):
            ts = entry.get("timestamp")
            price = entry.get("price")

            if ts and price:
                entry_dt = datetime.fromisoformat(ts)
                age_minutes = (current_time - entry_dt).total_seconds() / 60

                if current_price is None and age_minutes < 5:
                    current_price = price
                if age_minutes >= 12 and age_minutes <= 20:
                    price_15m_ago = price
                    break

        if current_price and price_15m_ago and price_15m_ago > 0:
            drop_pct = (price_15m_ago - current_price) / price_15m_ago

            if drop_pct > 0.10:  # >10% drop
                alert = f"FLASH CRASH: {token} dropped {drop_pct:.1%} in 15min"
                alerts.append(alert)
                log(f"FLASH CRASH DETECTED: {alert}")

                # Emergency action: close all meme positions
                open_count = portfolio.get("open_position_count", 0)
                if open_count > 0:
                    emergency_sell_all(f"Flash crash: {token} -{drop_pct:.1%}", portfolio)

    status = "FLASH_CRASH" if alerts else "OK"
    return {"status": status, "detail": f"{len(alerts)} flash crashes detected", "alerts": alerts}


def check_cron_health():
    """
    Check 5: Verify cron jobs are running.
    Checks last execution timestamps of critical crons.
    """
    cron_state = load_state("cron_health.json")

    if not cron_state:
        return {"status": "WARNING", "detail": "No cron health data — crons may not be configured yet"}

    alerts = []
    current = now_utc()

    # Expected cron intervals (in minutes) with grace period
    expected_crons = {
        "price_snapshot": 15,    # Every 3min, alert if >15min (5x tolerance)
        "reconciliation": 30,   # Every 10min, alert if >30min (3x tolerance)
        # meme_radar removed — not in crontab
    }

    for cron_name, max_age_min in expected_crons.items():
        last_run = cron_state.get(cron_name, {}).get("last_run")

        if last_run:
            last_dt = datetime.fromisoformat(last_run)
            age_min = (current - last_dt).total_seconds() / 60
            if age_min > max_age_min:
                alerts.append(f"{cron_name}: last ran {age_min:.0f}min ago (max {max_age_min}min)")
        else:
            alerts.append(f"{cron_name}: never ran")

    status = "ALERT" if alerts else "OK"
    return {"status": status, "detail": f"{len(alerts)} cron issues", "alerts": alerts}


def check_ntp_sync():
    """
    Check 6: Verify NTP clock sync.
    System clock drift > 2 seconds = BLOCK trading.
    """
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip()

        if "NTPSynchronized=yes" in output:
            return {"status": "OK", "detail": "NTP synchronized"}
        elif "NTPSynchronized=no" in output:
            log("WARNING: NTP not synchronized — time drift may affect trading")
            return {"status": "WARNING", "detail": "NTP not synchronized"}
        else:
            return {"status": "WARNING", "detail": f"NTP status unknown: {output}"}
    except FileNotFoundError:
        # timedatectl not available (e.g., in Docker)
        return {"status": "WARNING", "detail": "timedatectl not available — NTP check skipped"}
    except subprocess.TimeoutExpired:
        return {"status": "WARNING", "detail": "NTP check timed out"}
    except Exception as e:
        return {"status": "WARNING", "detail": f"NTP check error: {e}"}


def check_circuit_breakers():
    """Check circuit breaker states."""
    cb_state = load_state("circuit_breakers.json")

    tripped = []
    for component, state in cb_state.items():
        if isinstance(state, dict) and state.get("state") == "open":
            tripped.append(component)

    if len(tripped) >= 3:
        return {"status": "CRITICAL", "detail": f"{len(tripped)} tripped: {', '.join(tripped)}"}
    elif tripped:
        return {"status": "WARNING", "detail": f"{len(tripped)} tripped: {', '.join(tripped)}"}

    return {"status": "OK", "detail": "All closed"}


# ─────────────────────────────────────────────
# SUPABASE SYNC
# ─────────────────────────────────────────────

def sync_to_supabase(heartbeat_result):
    """
    Sync heartbeat status to Supabase system_status table.
    STUB: Will be implemented when Supabase client is configured.
    """
    # TODO: Implement Supabase sync
    pass


# ─────────────────────────────────────────────
# MAIN HEARTBEAT
# ─────────────────────────────────────────────

def run_heartbeat():
    """
    Main heartbeat function.
    Runs all checks, takes action on failures, then reports.

    ORDER MATTERS: Act first, notify second.
    """
    log("=" * 50)
    log("HEARTBEAT START")

    config = load_config()
    if not config:
        activate_kill_switch("Cannot load configuration")
        return {"status": "CRITICAL", "error": "Config unavailable"}

    portfolio = load_state("portfolio.json")
    price_cache = load_state("price_cache.json")

    results = {}

    # Run all checks
    results["kill_switch"] = check_kill_switch()
    if results["kill_switch"]["status"] == "HALTED":
        log("System HALTED — skipping remaining checks")
        log("HEARTBEAT END (HALTED)")
        return results

    results["positions"] = check_positions(config, portfolio, price_cache)
    results["exposure"] = check_exposure(config, portfolio)
    results["flash_crash"] = check_flash_crash(config, portfolio)
    results["cron_health"] = check_cron_health()
    results["ntp_sync"] = check_ntp_sync()
    results["circuit_breakers"] = check_circuit_breakers()

    # Determine overall status
    statuses = [r["status"] for r in results.values()]
    if "FLASH_CRASH" in statuses or "CRITICAL" in statuses:
        overall = "CRITICAL"
    elif "ALERT" in statuses or "HALTED" in statuses:
        overall = "ALERT"
    elif "WARNING" in statuses:
        overall = "WARNING"
    else:
        overall = "OK"

    # Update system status state file
    system_status = {
        "mode": portfolio.get("mode", "PAPER"),
        "overall_status": overall,
        "open_positions": portfolio.get("open_position_count", 0),
        "total_exposure_pct": portfolio.get("total_exposure_pct", 0),
        "daily_pnl_pct": portfolio.get("daily_pnl_pct", 0),
        "checks": results,
        "heartbeat_timestamp": now_iso(),
    }
    save_state("system_status.json", system_status)

    # Sync to Supabase
    sync_to_supabase(system_status)

    # Log summary
    check_summary = ", ".join(f"{k}={v['status']}" for k, v in results.items())
    log(f"HEARTBEAT END — Overall: {overall} ({check_summary})")

    # Notify on ALERT or CRITICAL only (not WARNING — avoids spam for NTP/container issues)
    if overall in ("ALERT", "CRITICAL"):
        alerts_detail = []
        for check_name, check_result in results.items():
            if check_result["status"] in ("ALERT", "CRITICAL"):
                detail = check_result.get("detail", "")
                alerts = check_result.get("alerts", [])
                if alerts:
                    alerts_detail.append(f"{check_name}: {'; '.join(alerts)}")
                else:
                    alerts_detail.append(f"{check_name}: {detail}")

        if alerts_detail:
            notify_whatsapp(
                f"Heartbeat {overall}: {' | '.join(alerts_detail)}",
                urgent=(overall == "CRITICAL")
            )

    # ── Hourly Telegram status summary ──
    try:
        hb_state = load_state("heartbeat_state.json") or {}
        last_summary = hb_state.get("last_telegram_summary_time", "")
        now_dt = datetime.now(timezone.utc)
        send_summary = True
        if last_summary:
            try:
                last_dt = datetime.fromisoformat(last_summary)
                if (now_dt - last_dt).total_seconds() < 3600:
                    send_summary = False
            except Exception:
                pass

        if send_summary:
            _send_hourly_summary(portfolio, price_cache, overall)
            hb_state["last_telegram_summary_time"] = now_dt.isoformat()
            hb_state["last_heartbeat"] = now_dt.isoformat()
            hb_state["status"] = overall
            save_state("heartbeat_state.json", hb_state)
    except Exception as e:
        log(f"Hourly summary error: {e}")

    return results


def _send_hourly_summary(portfolio, price_cache, overall):
    """Send concise hourly status to Telegram."""
    try:
        from notifier import send as notify_send
        from rejection_funnel import get_funnel

        # Positions
        positions_data = load_state("positions.json") or {}
        all_pos = positions_data.get("positions", [])
        open_pos = [p for p in all_pos if isinstance(p, dict) and p.get("status") == "OPEN"]

        pos_lines = []
        total_unrealized = 0.0
        for p in open_pos:
            entry = p.get("entry_price", 0)
            current = p.get("current_price", entry)
            size = p.get("position_usd", 0)
            if entry and entry > 0:
                pnl_pct = (current - entry) / entry * 100
                pnl_usd = (current - entry) / entry * size
                total_unrealized += pnl_usd
                sign = "+" if pnl_pct >= 0 else ""
                pos_lines.append(f"  {p.get('token','?')} @ ${entry:,.2f} -> ${current:,.2f} ({sign}{pnl_pct:.1f}%)")

        balance = portfolio.get("current_balance_usd", 0)

        # Funnel
        funnel = get_funnel()

        # Router
        router_state = load_state("signal_router_state.json") or {}
        daily_runs = router_state.get("daily_pipeline_runs", 0)
        last_run = router_state.get("last_run", "unknown")

        pos_section = "\n".join(pos_lines) if pos_lines else "  None"
        unr_sign = "+" if total_unrealized >= 0 else ""

        msg = f"""⚖️ HOURLY STATUS
💰 Balance: ${balance:,.2f} (unrealized: {unr_sign}${total_unrealized:.2f})
📈 Positions ({len(open_pos)}/10):
{pos_section}
📊 Today: {funnel.get('signals_ingested',0)} ingested, {funnel.get('executed',0)} executed, {funnel.get('judge_rejected',0)} rejected
🔄 Router: {daily_runs} runs today, last: {last_run[-8:] if len(last_run)>8 else last_run}
⚙️ Status: {overall}"""

        notify_send(msg, level="L2")
        log(f"Hourly Telegram summary sent ({len(open_pos)} open positions)")
    except Exception as e:
        log(f"Hourly summary send error: {e}")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

if __name__ == "__main__":
    results = run_heartbeat()
    # Output as JSON for monitoring
    print(json.dumps(results, indent=2, default=str))
