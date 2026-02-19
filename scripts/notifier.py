#!/usr/bin/env python3
"""
Unified Notification System — Sprint 6.2.1 through 6.2.8

Routes alerts to Telegram (and WhatsApp when available).

Alert Levels:
L1 (INFO): Console only — logged, no push
L2 (NORMAL): Telegram message — trade alerts, daily reports
L3 (URGENT): Telegram + sound — rejections, warnings
L4 (EMERGENCY): Telegram + deterministic action — flash crash, kill switch

Covers:
6.2.1 — Channel setup (Telegram live, WhatsApp pending)
6.2.2 — Notification function
6.2.3 — Trade execution notifications
6.2.4 — Al-Muhasbi rejection notifications
6.2.5 — Daily performance summary
6.2.6 — Weekly intelligence brief
6.2.7 — Security/flash crash alerts
6.2.8 — Alert levels (L1-L4)
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
NOTIFIER_STATE = STATE_DIR / "notifier_state.json"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[NOTIFY] {ts} {msg}", flush=True)


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


# ─────────────────────────────────────────────────────────
# 6.2.8 — Alert Levels
# ─────────────────────────────────────────────────────────

class AlertLevel:
    INFO = "L1"       # Console log only
    NORMAL = "L2"     # Telegram message
    URGENT = "L3"     # Telegram + emphasis
    EMERGENCY = "L4"  # Telegram + deterministic action


# ─────────────────────────────────────────────────────────
# 6.2.1 — Channel Configuration
# ─────────────────────────────────────────────────────────

def _get_telegram_config() -> dict:
    """Load Telegram bot config."""
    import env_loader
    token = env_loader.get_key("TELEGRAM_BOT_TOKEN")
    chat_id = env_loader.get_key("TELEGRAM_CHAT_ID")
    if not chat_id:
        chat_id = "5551371143"  # Salim's Telegram user ID
    return {"token": token, "chat_id": chat_id}


# ─────────────────────────────────────────────────────────
# 6.2.2 — Core Notification Function
# ─────────────────────────────────────────────────────────

def send(message: str, level: str = AlertLevel.NORMAL, title: str = None,
         parse_mode: str = None) -> bool:
    """Send a notification through available channels.

    Args:
        message: The notification text
        level: AlertLevel (L1-L4)
        title: Optional title/header
        parse_mode: Telegram parse mode (Markdown/HTML)

    Returns: True if sent successfully
    """
    now = _now()

    # Format message with level indicator
    level_emoji = {
        AlertLevel.INFO: "ℹ️",
        AlertLevel.NORMAL: "📊",
        AlertLevel.URGENT: "⚠️",
        AlertLevel.EMERGENCY: "🚨",
    }
    emoji = level_emoji.get(level, "📊")

    if title:
        full_msg = f"{emoji} *{title}*\n{message}"
    else:
        full_msg = f"{emoji} {message}"

    # L1: Log only
    if level == AlertLevel.INFO:
        _log(f"[L1] {title or ''}: {message[:100]}")
        _record_notification(level, title, message, "log_only")
        return True

    # L2-L4: Send via Telegram
    sent = _send_telegram(full_msg, parse_mode)

    # L4: Also trigger deterministic emergency actions
    if level == AlertLevel.EMERGENCY:
        _log(f"[L4 EMERGENCY] {title}: {message[:100]}")
        # Emergency actions are handled by the caller (heartbeat/emergency_sell)
        # This just ensures the alert gets out

    _record_notification(level, title, message, "telegram" if sent else "failed")
    return sent


def _send_telegram(message: str, parse_mode: str = None) -> bool:
    """Send message via Telegram Bot API."""
    config = _get_telegram_config()
    token = config.get("token")
    chat_id = config.get("chat_id")

    if not token:
        # Try via OpenClaw's native Telegram channel
        return _send_via_openclaw_telegram(message)

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        resp = requests.post(url, json=payload, timeout=10)

        if resp.status_code == 200:
            _log(f"Telegram sent ({len(message)} chars)")
            return True
        else:
            _log(f"Telegram error {resp.status_code}: {resp.text[:100]}")
            # Retry without parse_mode (in case markdown breaks)
            if parse_mode:
                # Strip markdown characters and retry as plain text
                import re
                clean = re.sub(r'[*_`\[\]]', '', message)
                return _send_telegram(clean, parse_mode=None)
            return False

    except Exception as e:
        _log(f"Telegram send failed: {e}")
        return False


def _send_via_openclaw_telegram(message: str) -> bool:
    """Fallback: send via OpenClaw's native Telegram integration."""
    # OpenClaw handles this via its channel system
    # Log for pickup by OpenClaw's message queue
    alert_path = BASE_DIR / "state" / "pending_alerts.json"
    alerts = _load_json(alert_path, {"alerts": []})
    alerts["alerts"].append({
        "channel": "telegram",
        "message": message,
        "timestamp": _now().isoformat(),
        "sent": False,
    })
    alerts["alerts"] = alerts["alerts"][-50:]  # Keep last 50
    _save_json(alert_path, alerts)
    _log("Alert queued for OpenClaw Telegram channel")
    return True


def _record_notification(level, title, message, channel):
    """Record notification in state for auditing."""
    state = _load_json(NOTIFIER_STATE, {"sent": 0, "failed": 0, "history": []})
    state["history"].append({
        "level": level,
        "title": title,
        "message": message[:200],
        "channel": channel,
        "at": _now().isoformat(),
    })
    state["history"] = state["history"][-100:]
    if channel != "failed":
        state["sent"] = state.get("sent", 0) + 1
    else:
        state["failed"] = state.get("failed", 0) + 1
    state["last_sent"] = _now().isoformat()
    _save_json(NOTIFIER_STATE, state)


# ─────────────────────────────────────────────────────────
# 6.2.3 — Trade Execution Notifications
# ─────────────────────────────────────────────────────────

def notify_trade_executed(trade: dict):
    """Notify on trade entry or exit."""
    token = trade.get("token", trade.get("symbol", "?"))
    side = trade.get("side", "BUY")
    price = trade.get("price", trade.get("entry_price", 0))
    qty = trade.get("quantity", trade.get("amount", 0))
    strategy = trade.get("strategy", "unknown")
    exchange = trade.get("exchange", "binance")

    msg = (f"*{side}* {qty} {token}\n"
           f"Price: ${price:,.4f}\n"
           f"Strategy: {strategy}\n"
           f"Exchange: {exchange}")

    send(msg, AlertLevel.NORMAL, title=f"Trade {side}")


def notify_trade_closed(trade: dict):
    """Notify on position close."""
    token = trade.get("token", trade.get("symbol", "?"))
    pnl = trade.get("pnl_pct", 0)
    pnl_usd = trade.get("pnl_usd", 0)
    reason = trade.get("exit_reason", trade.get("close_reason", "unknown"))

    result = "WIN ✅" if pnl > 0 else "LOSS ❌"

    msg = (f"{result}\n"
           f"Token: {token}\n"
           f"P&L: {pnl:+.2f}% (${pnl_usd:+.2f})\n"
           f"Exit: {reason}")

    level = AlertLevel.NORMAL
    if abs(pnl) > 5:
        level = AlertLevel.URGENT

    send(msg, level, title=f"Position Closed — {result}")


# ─────────────────────────────────────────────────────────
# 6.2.4 — Al-Muhasbi Rejection Notifications
# ─────────────────────────────────────────────────────────

def notify_rejection(token: str, reason: str, sanad_score: float = 0, judge_verdict: str = ""):
    """Notify when a trade is rejected."""
    msg = (f"Token: {token}\n"
           f"Sanad Score: {sanad_score:.0f}/100\n"
           f"Reason: {reason}")
    if judge_verdict:
        msg += f"\nJudge: {judge_verdict[:100]}"

    send(msg, AlertLevel.INFO, title="Trade Rejected")


# ─────────────────────────────────────────────────────────
# 6.2.5 — Daily Performance Summary
# ─────────────────────────────────────────────────────────

def notify_daily_summary(stats: dict):
    """Send daily performance report."""
    trades = stats.get("trades_today", 0)
    pnl = stats.get("daily_pnl_pct", 0)
    pnl_usd = stats.get("daily_pnl_usd", 0)
    win_rate = stats.get("daily_win_rate", 0)
    balance = stats.get("portfolio_balance", 0)
    drawdown = stats.get("max_drawdown_pct", 0)
    signals = stats.get("signals_processed", 0)
    rejected = stats.get("signals_rejected", 0)

    msg = (f"Trades: {trades}\n"
           f"P&L: {pnl:+.2f}% (${pnl_usd:+.2f})\n"
           f"Win Rate: {win_rate:.0%}\n"
           f"Balance: ${balance:,.2f}\n"
           f"Max DD: {drawdown:.1f}%\n"
           f"Signals: {signals} processed, {rejected} rejected")

    send(msg, AlertLevel.NORMAL, title="Daily Report")


# ─────────────────────────────────────────────────────────
# 6.2.6 — Weekly Intelligence Brief
# ─────────────────────────────────────────────────────────

def notify_weekly_brief(brief: dict):
    """Send weekly intelligence summary."""
    msg = brief.get("summary", "No weekly brief generated")
    if len(msg) > 3000:
        msg = msg[:3000] + "\n_(truncated)_"
    send(msg, AlertLevel.NORMAL, title="Weekly Intelligence Brief")


# ─────────────────────────────────────────────────────────
# 6.2.7 — Security/Flash Crash Alerts
# ─────────────────────────────────────────────────────────

def notify_flash_crash(details: dict):
    """EMERGENCY: Flash crash detected."""
    token = details.get("token", "MARKET")
    drop = details.get("drop_pct", 0)
    msg = (f"Token: {token}\n"
           f"Drop: {drop:.1f}%\n"
           f"Action: Emergency sell triggered")
    send(msg, AlertLevel.EMERGENCY, title="FLASH CRASH DETECTED")


def notify_kill_switch(reason: str):
    """EMERGENCY: Kill switch activated."""
    msg = f"Reason: {reason}\nAll trading HALTED."
    send(msg, AlertLevel.EMERGENCY, title="KILL SWITCH ACTIVATED")


def notify_security_alert(details: str):
    """URGENT: Security issue detected."""
    send(details, AlertLevel.URGENT, title="Security Alert")


# ─────────────────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    _log("=== NOTIFIER TEST ===")

    # Test L1 (log only)
    send("System heartbeat OK", AlertLevel.INFO, title="Heartbeat")
    print("  L1 (INFO): Logged only")

    # Test L2 (Telegram)
    result = send("Test notification from Sanad Trader", AlertLevel.NORMAL, title="Test Alert")
    print(f"  L2 (NORMAL): Telegram {'sent' if result else 'queued'}")

    # Test trade notification
    notify_trade_executed({
        "token": "BTCUSDT", "side": "BUY", "price": 68500,
        "quantity": 0.001, "strategy": "meme-momentum", "exchange": "binance"
    })
    print("  Trade execution notification sent")

    # Test rejection
    notify_rejection("FAKECOIN", "Sanad score too low", sanad_score=25)
    print("  Rejection notification logged")

    # Status
    state = _load_json(NOTIFIER_STATE, {})
    print(f"  Total sent: {state.get('sent', 0)}, Failed: {state.get('failed', 0)}")

    _log("=== TEST COMPLETE ===")
