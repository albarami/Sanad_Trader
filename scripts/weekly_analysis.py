#!/usr/bin/env python3
"""
Weekly Analysis — Sunday self-review.

The macro learning loop. Runs every Sunday at 8:00 AM Qatar (5:00 UTC).
Reviews the full week's performance:

1. Trade performance by strategy (win rate, avg P&L, best/worst)
2. UCB1 source rankings & changes
3. Counterfactual review (missed winners vs correct rejections)
4. Strategy effectiveness comparison
5. Portfolio metrics (cumulative P&L, drawdown, Sharpe approximation)
6. System health (cron reliability, pipeline budget efficiency)
7. Recommendations for threshold adjustments

Sends summary to Telegram. Saves to reports/weekly_YYYY-MM-DD.json.
"""
import json
import statistics
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
STATE_DIR = BASE_DIR / "state"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[WEEKLY] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def run():
    _log("=== WEEKLY ANALYSIS ===")
    now = _now()
    week_ago = now - timedelta(days=7)

    # ── 1. Trade History ──
    history = _load_json(STATE_DIR / "trade_history.json", {"trades": []})
    trades = history if isinstance(history, list) else history.get("trades", [])
    
    all_sells = [t for t in trades if t.get("side") == "SELL"]
    weekly_sells = []
    for t in all_sells:
        try:
            ts = datetime.fromisoformat(t.get("timestamp", "2000-01-01"))
            if ts > week_ago:
                weekly_sells.append(t)
        except Exception:
            pass

    total_trades = len(all_sells)
    weekly_trades = len(weekly_sells)
    
    # Win/loss
    weekly_wins = [t for t in weekly_sells if (t.get("pnl_pct", 0) or 0) > 0]
    weekly_losses = [t for t in weekly_sells if (t.get("pnl_pct", 0) or 0) <= 0]
    weekly_win_rate = (len(weekly_wins) / weekly_trades * 100) if weekly_trades > 0 else 0
    
    all_wins = [t for t in all_sells if (t.get("pnl_pct", 0) or 0) > 0]
    all_time_win_rate = (len(all_wins) / total_trades * 100) if total_trades > 0 else 0

    # P&L
    weekly_pnls = [(t.get("pnl_pct", 0) or 0) * 100 for t in weekly_sells]
    weekly_usd = [t.get("pnl_usd", 0) or 0 for t in weekly_sells]
    weekly_total_usd = sum(weekly_usd)
    weekly_avg_pnl = statistics.mean(weekly_pnls) if weekly_pnls else 0
    
    # Best/worst
    best = max(weekly_sells, key=lambda t: t.get("pnl_pct", 0) or 0, default=None)
    worst = min(weekly_sells, key=lambda t: t.get("pnl_pct", 0) or 0, default=None)

    # ── 2. Strategy Breakdown ──
    strategy_stats = {}
    for t in weekly_sells:
        strat = t.get("strategy", t.get("strategy_name", "unknown"))
        if strat not in strategy_stats:
            strategy_stats[strat] = {"trades": 0, "wins": 0, "pnls": [], "usd": []}
        strategy_stats[strat]["trades"] += 1
        pnl = (t.get("pnl_pct", 0) or 0) * 100
        strategy_stats[strat]["pnls"].append(pnl)
        strategy_stats[strat]["usd"].append(t.get("pnl_usd", 0) or 0)
        if pnl > 0:
            strategy_stats[strat]["wins"] += 1

    # ── 3. UCB1 Source Rankings ──
    ucb1_lines = []
    try:
        from ucb1_scorer import get_all_scores
        scores = get_all_scores()
        sorted_sources = sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)
        for name, info in sorted_sources[:8]:
            grade = info.get("grade", "?")
            score = info.get("score", 0)
            wr = info.get("win_rate", 0) * 100
            total = info.get("trades_executed", 0)
            cold = " (cold)" if info.get("cold_start") else ""
            ucb1_lines.append(f"  {grade} {name}: {score:.0f} ({wr:.0f}% WR, {total}t){cold}")
    except Exception:
        ucb1_lines.append("  (UCB1 unavailable)")

    # ── 4. Counterfactual Summary ──
    cf_report = _load_json(BASE_DIR / "genius-memory" / "counterfactual_report.json", {})
    cf_total = cf_report.get("total_checked", 0)
    cf_missed = cf_report.get("missed_winners", 0)
    cf_correct = cf_report.get("correct_rejections", 0)
    cf_accuracy = cf_report.get("gate_accuracy_pct", 0)

    # ── 5. Portfolio ──
    portfolio = _load_json(STATE_DIR / "portfolio.json", {})
    balance = portfolio.get("current_balance_usd", 10000)
    starting = portfolio.get("starting_balance_usd", 10000)
    cumulative_pnl = balance - starting
    cumulative_pct = (cumulative_pnl / starting) * 100 if starting > 0 else 0
    drawdown = portfolio.get("current_drawdown_pct", 0)
    peak = portfolio.get("peak_balance_usd", balance)

    # Sharpe approximation (if enough data)
    sharpe = "N/A"
    if len(weekly_pnls) >= 5:
        mean_r = statistics.mean(weekly_pnls)
        std_r = statistics.stdev(weekly_pnls) if len(weekly_pnls) > 1 else 1
        if std_r > 0:
            # Annualize: weekly trades, ~52 weeks
            sharpe_raw = mean_r / std_r
            sharpe = f"{sharpe_raw:.2f}"

    # ── 6. Graduation Progress ──
    graduation = _load_json(BASE_DIR / "config" / "thresholds.yaml", {})
    # Days since paper start
    try:
        activation = _load_json(STATE_DIR / "paper_mode_activation.json", {})
        start_date = datetime.fromisoformat(activation.get("activated_at", now.isoformat()))
        days_active = (now - start_date).days
    except Exception:
        days_active = 0

    grad_min_trades = 30
    grad_min_wr = 52
    grad_min_days = 90

    # ── 7. System Health ──
    cron_health = _load_json(STATE_DIR / "cron_health.json", {})
    healthy_crons = sum(1 for v in cron_health.values() if isinstance(v, dict) and v.get("status") == "ok")
    total_crons = sum(1 for v in cron_health.values() if isinstance(v, dict))

    # ── Build Report ──
    r = []
    r.append("⚖️ *SANAD TRADER — WEEKLY ANALYSIS*")
    r.append(f"📅 Week ending {now.strftime('%B %d, %Y')}")
    r.append("")

    # Portfolio
    pnl_emoji = "🟢" if cumulative_pnl >= 0 else "🔴"
    r.append(f"💰 *Portfolio:* ${balance:,.2f}")
    r.append(f"{pnl_emoji} Cumulative: ${cumulative_pnl:+,.2f} ({cumulative_pct:+.2f}%)")
    r.append(f"📈 Peak: ${peak:,.2f} | Drawdown: {drawdown:.2f}%")
    r.append(f"📊 Sharpe: {sharpe}")
    r.append("")

    # Weekly trades
    r.append(f"📋 *This Week ({weekly_trades} trades):*")
    r.append(f"  Win rate: {weekly_win_rate:.0f}% ({len(weekly_wins)}W/{len(weekly_losses)}L)")
    r.append(f"  P&L: ${weekly_total_usd:+.2f} (avg {weekly_avg_pnl:+.1f}%)")
    if best:
        r.append(f"  Best: {best.get('token','?')} {(best.get('pnl_pct',0) or 0)*100:+.1f}%")
    if worst:
        r.append(f"  Worst: {worst.get('token','?')} {(worst.get('pnl_pct',0) or 0)*100:+.1f}%")
    r.append("")

    # Strategy breakdown
    if strategy_stats:
        r.append(f"🎯 *Strategy Performance:*")
        for strat, data in sorted(strategy_stats.items(), key=lambda x: sum(x[1]["usd"]), reverse=True):
            wr = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0
            avg = statistics.mean(data["pnls"]) if data["pnls"] else 0
            total_usd = sum(data["usd"])
            r.append(f"  {strat}: {data['trades']}t, {wr:.0f}% WR, ${total_usd:+.2f} (avg {avg:+.1f}%)")
        r.append("")

    # UCB1
    if ucb1_lines:
        r.append(f"📡 *Source Rankings (UCB1):*")
        r.extend(ucb1_lines)
        r.append("")

    # Counterfactual
    if cf_total > 0:
        r.append(f"🔍 *Counterfactual (rejected signals):*")
        r.append(f"  Checked: {cf_total} | Missed: {cf_missed} | Correct: {cf_correct}")
        r.append(f"  Gate accuracy: {cf_accuracy}%")
        if cf_missed > cf_correct:
            r.append(f"  ⚠️ More missed winners than correct rejections — gates may be too tight")
        r.append("")

    # Graduation progress
    r.append(f"🎓 *Graduation Progress:*")
    r.append(f"  Day {days_active}/90 | Trades {total_trades}/30 min")
    r.append(f"  Win rate: {all_time_win_rate:.0f}% (need ≥52%)")
    r.append(f"  Max drawdown: {drawdown:.1f}% (limit 15%)")
    progress_pct = min(100, (days_active / grad_min_days * 33) + (total_trades / grad_min_trades * 33) + (min(all_time_win_rate, grad_min_wr) / grad_min_wr * 34))
    r.append(f"  Overall: {progress_pct:.0f}%")
    r.append("")

    # System
    r.append(f"⚙️ *System:*")
    r.append(f"  Cron health: {healthy_crons}/{total_crons} OK")
    r.append(f"  Mode: PAPER")

    # ── Run Pattern Extractor ──
    pattern_summary = ""
    try:
        from pattern_extractor import run as run_pattern_extractor
        _log("Running pattern extractor...")
        pattern_result = run_pattern_extractor()
        if pattern_result and isinstance(pattern_result, dict):
            patterns = pattern_result.get("patterns", [])
            if patterns:
                r.append(f"🧠 *Pattern Extractor ({len(patterns)} patterns):*")
                for pat in patterns[:3]:
                    name = pat.get("name", pat.get("pattern", "?"))
                    conf = pat.get("confidence", 0)
                    r.append(f"  • {name} ({conf:.0%} confidence)")
                r.append("")
                pattern_summary = f"{len(patterns)} patterns found"
        _log(f"Pattern extractor complete: {pattern_summary or 'no patterns'}")
    except Exception as e:
        _log(f"Pattern extractor failed: {e}")

    report_text = "\n".join(r)

    # ── Send Telegram ──
    try:
        import notifier
        notifier.send(report_text, level="L2", title="Weekly Analysis")
        _log("Weekly report sent to Telegram ✅")
    except Exception as e:
        _log(f"Telegram send failed: {e}")

    # ── Save ──
    report_dir = BASE_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_data = {
        "week_ending": now.strftime("%Y-%m-%d"),
        "balance": balance,
        "cumulative_pnl": cumulative_pnl,
        "weekly_trades": weekly_trades,
        "weekly_win_rate": weekly_win_rate,
        "weekly_pnl_usd": weekly_total_usd,
        "total_trades": total_trades,
        "all_time_win_rate": all_time_win_rate,
        "drawdown": drawdown,
        "sharpe": sharpe,
        "days_active": days_active,
        "strategy_stats": {k: {"trades": v["trades"], "wins": v["wins"], "avg_pnl": statistics.mean(v["pnls"]) if v["pnls"] else 0} for k, v in strategy_stats.items()},
        "cf_accuracy": cf_accuracy,
        "generated_at": now.isoformat(),
    }
    (report_dir / f"weekly_{now.strftime('%Y-%m-%d')}.json").write_text(json.dumps(report_data, indent=2, default=str))

    print(report_text)
    _log("=== WEEKLY ANALYSIS COMPLETE ===")


if __name__ == "__main__":
    run()
