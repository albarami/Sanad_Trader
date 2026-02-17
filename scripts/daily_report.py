#!/usr/bin/env python3
"""
Daily Performance Report — sends Telegram summary every morning.

Covers:
- Portfolio P&L (daily + cumulative)
- Open positions with current P&L
- Trades opened/closed in last 24h
- Win rate + best/worst trade
- UCB1 source rankings
- Budget usage
- Counterfactual summary (if available)
- System health

Runs at 7:00 AM Qatar time (4:00 UTC) via cron.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[DAILY-REPORT] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def run():
    _log("=== GENERATING DAILY REPORT ===")
    now = _now()
    cutoff = now - timedelta(hours=24)

    # ── Portfolio ──
    portfolio = _load_json(STATE_DIR / "portfolio.json", {})
    balance = portfolio.get("current_balance_usd", 10000)
    starting = portfolio.get("starting_balance_usd", 10000)
    cumulative_pnl = balance - starting
    cumulative_pct = (cumulative_pnl / starting) * 100 if starting > 0 else 0
    peak = portfolio.get("peak_balance_usd", balance)
    drawdown = portfolio.get("current_drawdown_pct", 0)

    # ── Open Positions ──
    positions = _load_json(STATE_DIR / "positions.json", {"positions": []})
    opens = [p for p in positions.get("positions", []) if p.get("status") == "OPEN"]

    position_lines = []
    total_unrealized = 0
    try:
        from binance_client import get_price
        for p in opens:
            cur = float(get_price(p["symbol"]) or 0)
            pnl_pct = ((cur - p["entry_price"]) / p["entry_price"]) * 100 if p["entry_price"] > 0 else 0
            pnl_usd = (cur - p["entry_price"]) * p.get("quantity", 0)
            total_unrealized += pnl_usd
            be = " 🔒" if p.get("breakeven_activated") else ""
            position_lines.append(f"  {p['token']}: ${cur:.4f} ({pnl_pct:+.1f}%) ${pnl_usd:+.2f}{be}")
    except Exception as e:
        position_lines.append(f"  (price fetch failed: {e})")

    # ── Trade History (last 24h) ──
    history = _load_json(STATE_DIR / "trade_history.json", {"trades": []})
    trades = history if isinstance(history, list) else history.get("trades", [])

    recent_trades = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(t.get("timestamp", "2000-01-01"))
            if ts > cutoff:
                recent_trades.append(t)
        except Exception:
            pass

    # All-time stats
    all_sells = [t for t in trades if t.get("side") == "SELL"]
    wins = [t for t in all_sells if (t.get("pnl_pct", 0) or 0) > 0]
    losses = [t for t in all_sells if (t.get("pnl_pct", 0) or 0) <= 0]
    win_rate = (len(wins) / len(all_sells) * 100) if all_sells else 0

    # Best/worst
    best_trade = max(all_sells, key=lambda t: t.get("pnl_pct", 0), default=None) if all_sells else None
    worst_trade = min(all_sells, key=lambda t: t.get("pnl_pct", 0), default=None) if all_sells else None

    # Recent 24h
    recent_buys = [t for t in recent_trades if t.get("side") == "BUY"]
    recent_sells = [t for t in recent_trades if t.get("side") == "SELL"]
    recent_pnl = sum(t.get("pnl_usd", 0) or 0 for t in recent_sells)

    # ── UCB1 Source Rankings ──
    ucb1_lines = []
    try:
        from ucb1_scorer import get_all_scores
        scores = get_all_scores()
        if scores:
            sorted_sources = sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)
            for name, info in sorted_sources[:5]:
                grade = info.get("grade", "?")
                score = info.get("score", 0)
                wr = info.get("win_rate", 0) * 100
                total = info.get("trades_executed", 0)
                cold = " (cold)" if info.get("cold_start") else ""
                ucb1_lines.append(f"  {grade} {name}: {score:.0f} ({wr:.0f}% WR, {total} trades){cold}")
    except Exception:
        ucb1_lines.append("  (UCB1 unavailable)")

    # ── Router Budget ──
    router_state = _load_json(STATE_DIR / "signal_router_state.json", {})
    daily_runs = router_state.get("daily_pipeline_runs", 0)
    last_pick = router_state.get("signal_selected", {}).get("token", "none")
    last_result = router_state.get("pipeline_result", "?")

    # ── Counterfactual Summary ──
    cf_lines = []
    cf_report = _load_json(BASE_DIR / "genius-memory" / "counterfactual_report.json", {})
    if cf_report.get("total_checked", 0) > 0:
        cf_lines.append(f"  Checked: {cf_report['total_checked']} rejections")
        cf_lines.append(f"  Missed winners: {cf_report.get('missed_winners', 0)}")
        cf_lines.append(f"  Correct rejections: {cf_report.get('correct_rejections', 0)}")
        cf_lines.append(f"  Gate accuracy: {cf_report.get('gate_accuracy_pct', 0)}%")

    # ── Cron Health ──
    cron_health = _load_json(STATE_DIR / "cron_health.json", {})
    healthy = sum(1 for v in cron_health.values() if isinstance(v, dict) and v.get("status") == "ok")
    total_crons = sum(1 for v in cron_health.values() if isinstance(v, dict))

    # ── Build Report ──
    report = []
    report.append("⚖️ *SANAD TRADER — DAILY REPORT*")
    report.append(f"📅 {now.strftime('%A, %B %d %Y')}")
    report.append("")

    # Portfolio
    pnl_emoji = "🟢" if cumulative_pnl >= 0 else "🔴"
    report.append(f"💰 *Portfolio:* ${balance:,.2f}")
    report.append(f"{pnl_emoji} Cumulative P&L: ${cumulative_pnl:+,.2f} ({cumulative_pct:+.2f}%)")
    report.append(f"📈 Peak: ${peak:,.2f} | Drawdown: {drawdown:.2f}%")
    report.append(f"💵 Unrealized: ${total_unrealized:+.2f}")
    report.append("")

    # Positions
    report.append(f"📊 *Open Positions ({len(opens)}/5):*")
    if position_lines:
        report.extend(position_lines)
    else:
        report.append("  No open positions")
    report.append("")

    # 24h Activity
    report.append(f"📋 *Last 24h:*")
    report.append(f"  Opened: {len(recent_buys)} | Closed: {len(recent_sells)}")
    report.append(f"  24h Realized P&L: ${recent_pnl:+.2f}")
    report.append("")

    # All-time Stats
    report.append(f"📈 *All-Time ({len(all_sells)} closed trades):*")
    report.append(f"  Win rate: {win_rate:.0f}% ({len(wins)}W / {len(losses)}L)")
    if best_trade:
        report.append(f"  Best: {best_trade.get('token','?')} {best_trade.get('pnl_pct',0)*100:+.2f}%")
    if worst_trade:
        report.append(f"  Worst: {worst_trade.get('token','?')} {worst_trade.get('pnl_pct',0)*100:+.2f}%")
    report.append("")

    # UCB1
    if ucb1_lines:
        report.append(f"🎯 *Source Rankings (UCB1):*")
        report.extend(ucb1_lines)
        report.append("")

    # Counterfactual
    if cf_lines:
        report.append(f"🔍 *Counterfactual (rejected signals):*")
        report.extend(cf_lines)
        report.append("")

    # System
    report.append(f"⚙️ *System:*")
    report.append(f"  Pipeline runs: {daily_runs}/75")
    report.append(f"  Last pick: {last_pick} → {last_result}")
    report.append(f"  Cron health: {healthy}/{total_crons} OK")
    report.append(f"  Mode: PAPER")

    report_text = "\n".join(report)

    # ── Send via Telegram ──
    try:
        import notifier
        notifier.send(report_text, level="L2", title="Daily Report")
        _log("Report sent to Telegram ✅")
    except Exception as e:
        _log(f"Telegram send failed: {e}")

    # ── Save report ──
    report_dir = BASE_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"daily_{now.strftime('%Y-%m-%d')}.json"
    report_data = {
        "date": now.strftime("%Y-%m-%d"),
        "balance": balance,
        "cumulative_pnl": cumulative_pnl,
        "cumulative_pct": cumulative_pct,
        "open_positions": len(opens),
        "trades_24h": len(recent_trades),
        "total_closed_trades": len(all_sells),
        "win_rate": win_rate,
        "unrealized_pnl": total_unrealized,
        "daily_runs": daily_runs,
        "generated_at": now.isoformat(),
    }
    report_file.write_text(json.dumps(report_data, indent=2, default=str))

    print(report_text)
    _log("=== REPORT COMPLETE ===")


if __name__ == "__main__":
    run()
