#!/usr/bin/env python3
"""
Trading Summary — Sends a comprehensive 2-hour summary to Telegram.
Covers: trades executed, positions closed, P&L, learning stats, system health.
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
sys.path.insert(0, str(SCRIPT_DIR))

import state_store


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[SUMMARY] {ts} {msg}", flush=True)


def generate_summary():
    """Generate 2-hour trading summary from SQLite."""
    now = datetime.now(timezone.utc)
    two_hours_ago = (now - timedelta(hours=2)).isoformat()
    today = now.strftime("%Y-%m-%d")

    with state_store.get_connection(state_store.DB_PATH) as con:
        # Recent decisions (last 2h)
        executes = con.execute(
            "SELECT COUNT(*) FROM decisions WHERE result='EXECUTE' AND created_at > ?",
            (two_hours_ago,)
        ).fetchone()[0]
        
        skips = con.execute(
            "SELECT COUNT(*) FROM decisions WHERE result='SKIP' AND created_at > ?",
            (two_hours_ago,)
        ).fetchone()[0]
        
        blocks = con.execute(
            "SELECT COUNT(*) FROM decisions WHERE result='BLOCK' AND created_at > ?",
            (two_hours_ago,)
        ).fetchone()[0]

        # Today totals
        today_executes = con.execute(
            "SELECT COUNT(*) FROM decisions WHERE result='EXECUTE' AND created_at LIKE ?",
            (today + '%',)
        ).fetchone()[0]

        today_total = con.execute(
            "SELECT COUNT(*) FROM decisions WHERE created_at LIKE ?",
            (today + '%',)
        ).fetchone()[0]

        # Open positions
        open_pos = con.execute(
            "SELECT token_address, entry_price, size_usd FROM positions WHERE status='OPEN'"
        ).fetchall()

        # Recent closes (last 2h)
        closes = con.execute(
            "SELECT token_address, pnl_pct, pnl_usd, close_reason FROM positions WHERE status='CLOSED' AND closed_at > ?",
            (two_hours_ago,)
        ).fetchall()

        # Total P&L today
        today_pnl = con.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) FROM positions WHERE status='CLOSED' AND closed_at LIKE ?",
            (today + '%',)
        ).fetchone()[0]

        # Learning stats
        bandit = con.execute(
            "SELECT strategy_id, alpha, beta, n FROM bandit_strategy_stats ORDER BY n DESC LIMIT 5"
        ).fetchall()

        ucb = con.execute(
            "SELECT source_id, n, reward_sum FROM source_ucb_stats ORDER BY n DESC LIMIT 5"
        ).fetchall()

    # Portfolio
    portfolio = state_store.get_portfolio()
    balance = portfolio.get("current_balance_usd", 0)

    # Build message
    lines = []
    lines.append("📋 *2-HOUR TRADING SUMMARY*\n")

    # Decisions
    lines.append(f"🎯 *Decisions (last 2h):* {executes} executed, {skips} skipped, {blocks} blocked")
    lines.append(f"📊 *Today total:* {today_executes} executed / {today_total} evaluated\n")

    # Open positions
    lines.append(f"📈 *Open Positions ({len(open_pos)}):*")
    if open_pos:
        total_exposure = 0
        for p in open_pos:
            token = p[0][:12]
            size = p[2] or 0
            total_exposure += size
            lines.append(f"  • {token} — ${size:.0f}")
        lines.append(f"  💰 Total exposure: ${total_exposure:.0f}")
    else:
        lines.append("  None")

    # Recent closes
    if closes:
        lines.append(f"\n🔴 *Closed (last 2h):*")
        for c in closes:
            token = c[0][:12]
            pnl_pct = (c[1] or 0) * 100
            pnl_usd = c[2] or 0
            reason = c[3] or "?"
            emoji = "🟢" if pnl_usd >= 0 else "🔴"
            lines.append(f"  {emoji} {token}: {pnl_pct:+.1f}% (${pnl_usd:+.2f}) — {reason}")

    # P&L
    lines.append(f"\n💰 *Balance:* ${balance:,.2f}")
    lines.append(f"📉 *Today P&L:* ${today_pnl:+.2f}")

    # Learning stats
    if bandit:
        lines.append(f"\n🧠 *Learning (Thompson):*")
        for b in bandit:
            ev = b[1] / (b[1] + b[2]) * 100 if (b[1] + b[2]) > 0 else 0
            lines.append(f"  • {b[0]}: {b[3]} trades, {ev:.0f}% E[win]")

    if ucb:
        lines.append(f"🔍 *Source Quality (UCB1):*")
        for u in ucb:
            wr = u[2] / u[1] * 100 if u[1] > 0 else 0
            lines.append(f"  • {u[0]}: {u[1]} signals, {wr:.0f}% win")

    return "\n".join(lines)


def main():
    try:
        summary = generate_summary()
        _log("Summary generated")

        from notifier import send as notify
        notify(summary, level="L2", title="2-Hour Summary")
        _log("Summary sent to Telegram")
        print(summary)
    except Exception as e:
        _log(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
