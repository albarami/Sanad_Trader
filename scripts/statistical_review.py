#!/usr/bin/env python3
"""
Statistical Review — Sprint 5.1.10

Uses GPT (via OpenClaw /codex) for rigorous statistical validation.
Rolling 7/30/90-day metrics. Validates Opus pattern findings.
Runs weekly (Sunday 06:30 QAT) after pattern_extractor.

Deterministic stats computed in Python, GPT validates and interprets.

Reads: state/trade_history.json, genius-memory/patterns/
Writes: genius-memory/statistical_reviews/weekly_YYYYMMDD.json
"""

import json
import os
import statistics
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
TRADE_HISTORY = BASE_DIR / "state" / "trade_history.json"
PATTERNS_DIR = BASE_DIR / "genius-memory" / "patterns"
REVIEWS_DIR = BASE_DIR / "genius-memory" / "statistical-reviews"
REGIME_LATEST = BASE_DIR / "genius-memory" / "regime-data" / "latest.json"


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[STATS] {ts} {msg}", flush=True)


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


def _get_trades() -> list:
    history = _load_json(TRADE_HISTORY, [])
    trades = history if isinstance(history, list) else history.get("trades", [])
    return [t for t in trades if t.get("status") in ("closed", "CLOSED", None)]


# ─────────────────────────────────────────────────────────
# Deterministic Statistics (Python — no LLM)
# ─────────────────────────────────────────────────────────

def calc_rolling_stats(trades: list, days: int) -> dict:
    """Calculate rolling statistics for a window."""
    now = _now()
    cutoff = (now - timedelta(days=days)).isoformat()
    window = [t for t in trades if t.get("closed_at", t.get("exit_time", "")) >= cutoff]

    if not window:
        return {"trades": 0, "period_days": days}

    pnls = []
    wins = 0
    losses = 0
    win_amounts = []
    loss_amounts = []
    hold_durations = []

    for t in window:
        pnl = t.get("pnl_pct", t.get("pnl_percent", 0))
        if isinstance(pnl, str):
            try:
                pnl = float(pnl.replace("%", ""))
            except ValueError:
                pnl = 0
        pnls.append(pnl)

        if pnl > 0:
            wins += 1
            win_amounts.append(pnl)
        elif pnl < 0:
            losses += 1
            loss_amounts.append(abs(pnl))

        hold = t.get("hold_duration_hours", t.get("hold_hours", 0))
        if hold:
            hold_durations.append(hold)

    total = len(window)
    win_rate = wins / total if total > 0 else 0
    avg_win = statistics.mean(win_amounts) if win_amounts else 0
    avg_loss = statistics.mean(loss_amounts) if loss_amounts else 0
    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    # Sharpe-like ratio (using daily returns approximation)
    if len(pnls) > 1 and statistics.stdev(pnls) > 0:
        sharpe = (statistics.mean(pnls) / statistics.stdev(pnls)) * math.sqrt(252)
    else:
        sharpe = 0

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for pnl in pnls:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Worst streak
    worst_streak = 0
    current_streak = 0
    for pnl in pnls:
        if pnl < 0:
            current_streak += 1
            worst_streak = max(worst_streak, current_streak)
        else:
            current_streak = 0

    return {
        "period_days": days,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "payoff_ratio": round(payoff_ratio, 2),
        "expectancy_pct": round(expectancy, 2),
        "total_return_pct": round(sum(pnls), 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "worst_losing_streak": worst_streak,
        "avg_hold_hours": round(statistics.mean(hold_durations), 1) if hold_durations else 0,
        "pnl_stddev": round(statistics.stdev(pnls), 2) if len(pnls) > 1 else 0,
    }


def calc_by_strategy(trades: list) -> dict:
    """Break down stats by strategy."""
    strategies = {}
    for t in trades:
        strat = t.get("strategy", "unknown")
        if strat not in strategies:
            strategies[strat] = []
        strategies[strat].append(t)

    result = {}
    for strat, strat_trades in strategies.items():
        pnls = []
        wins = 0
        for t in strat_trades:
            pnl = t.get("pnl_pct", t.get("pnl_percent", 0))
            if isinstance(pnl, str):
                try:
                    pnl = float(pnl.replace("%", ""))
                except ValueError:
                    pnl = 0
            pnls.append(pnl)
            if pnl > 0:
                wins += 1

        result[strat] = {
            "trades": len(strat_trades),
            "win_rate": round(wins / len(strat_trades), 4) if strat_trades else 0,
            "total_return_pct": round(sum(pnls), 2),
            "avg_pnl_pct": round(statistics.mean(pnls), 2) if pnls else 0,
        }
    return result


def calc_by_source(trades: list) -> dict:
    """Break down stats by signal source."""
    sources = {}
    for t in trades:
        src = t.get("source", t.get("signal_source", "unknown"))
        if src not in sources:
            sources[src] = {"wins": 0, "total": 0, "pnls": []}
        sources[src]["total"] += 1
        pnl = t.get("pnl_pct", 0)
        if isinstance(pnl, str):
            try:
                pnl = float(pnl.replace("%", ""))
            except ValueError:
                pnl = 0
        sources[src]["pnls"].append(pnl)
        if pnl > 0:
            sources[src]["wins"] += 1

    result = {}
    for src, data in sources.items():
        result[src] = {
            "trades": data["total"],
            "win_rate": round(data["wins"] / data["total"], 4) if data["total"] else 0,
            "avg_pnl_pct": round(statistics.mean(data["pnls"]), 2) if data["pnls"] else 0,
        }
    return result


# ─────────────────────────────────────────────────────────
# GPT Statistical Validation
# ─────────────────────────────────────────────────────────

def build_gpt_prompt(stats_7d: dict, stats_30d: dict, stats_90d: dict,
                     by_strategy: dict, by_source: dict, latest_patterns: dict) -> str:
    """Build prompt for GPT statistical review."""
    return f"""You are a quantitative analyst reviewing a cryptocurrency trading system's performance.
Your role is RIGOROUS STATISTICAL VALIDATION. Be skeptical. Challenge any claimed patterns.

PERFORMANCE DATA (deterministic Python calculations):

7-DAY ROLLING:
{json.dumps(stats_7d, indent=2)}

30-DAY ROLLING:
{json.dumps(stats_30d, indent=2)}

90-DAY ROLLING:
{json.dumps(stats_90d, indent=2)}

BY STRATEGY:
{json.dumps(by_strategy, indent=2)}

BY SOURCE:
{json.dumps(by_source, indent=2)}

LATEST PATTERN ANALYSIS (from Opus):
{json.dumps(latest_patterns, indent=2, default=str)[:2000]}

YOUR TASK:
1. Are the sample sizes sufficient for statistical significance?
2. Are any claimed winning patterns likely just noise?
3. Which strategies show REAL edge vs random? (Use simple significance tests)
4. Is the system's Sharpe ratio meaningful given the sample size?
5. Any strategy that should be DEACTIVATED based on the data?
6. Specific parameter changes you'd recommend WITH statistical justification?

Return JSON:
{{
  "verdict": "HEALTHY/CAUTION/CRITICAL",
  "sample_size_adequate": true/false,
  "statistically_significant_patterns": ["..."],
  "noise_patterns": ["..."],
  "strategy_verdicts": {{"strategy_name": "KEEP/WATCH/DEACTIVATE"}},
  "recommendations": ["..."],
  "confidence_level": "high/medium/low",
  "summary": "2-3 sentences"
}}
"""


def call_gpt(prompt: str) -> dict | None:
    """Call GPT via OpenClaw or direct API."""
    _log("Calling GPT for statistical validation...")

    try:
        # Load env
        import env_loader

        # Try OpenAI API
        api_key = env_loader.get_key("OPENAI_API_KEY")
        if api_key:
            import requests

            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-5.2-chat-latest",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_completion_tokens": 2048,
                    "response_format": {"type": "json_object"},
                },
                timeout=90,
            )

            if resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"]
                return json.loads(text)
            else:
                _log(f"GPT API error {resp.status_code}: {resp.text[:200]}")

        # Try Anthropic as fallback for statistical review
        api_key = env_loader.get_key("ANTHROPIC_API_KEY")
        if api_key:
            import requests

            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-5-20250929",
                    "max_completion_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=90,
            )

            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"]
                import re
                json_match = re.search(r'\{[\s\S]*\}', text)
                if json_match:
                    return json.loads(json_match.group())

        _log("No API key available for statistical review")
        return None

    except Exception as e:
        _log(f"GPT/LLM call failed: {e}")
        return None


def _get_latest_patterns() -> dict:
    """Load most recent pattern extraction result."""
    if not PATTERNS_DIR.exists():
        return {}
    files = sorted(PATTERNS_DIR.glob("weekly_*.json"), reverse=True)
    if files:
        return _load_json(files[0])
    return {}


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def run():
    _log("=== STATISTICAL REVIEW ===")

    trades = _get_trades()
    if len(trades) < 3:
        _log(f"Only {len(trades)} trades — need 3+ for review")
        return None

    _log(f"Analyzing {len(trades)} completed trades")

    # Deterministic stats (5.1.10 core — always runs)
    stats_7d = calc_rolling_stats(trades, 7)
    stats_30d = calc_rolling_stats(trades, 30)
    stats_90d = calc_rolling_stats(trades, 90)
    by_strategy = calc_by_strategy(trades)
    by_source = calc_by_source(trades)

    print(f"\n  === ROLLING STATS ===")
    for label, s in [("7d", stats_7d), ("30d", stats_30d), ("90d", stats_90d)]:
        if s["trades"] > 0:
            print(f"    {label}: {s['trades']} trades, WR={s['win_rate']:.1%}, "
                  f"Return={s['total_return_pct']}%, Sharpe={s['sharpe_ratio']}, "
                  f"MaxDD={s['max_drawdown_pct']}%")

    print(f"\n  === BY STRATEGY ===")
    for strat, s in by_strategy.items():
        print(f"    {strat}: {s['trades']} trades, WR={s['win_rate']:.1%}, Avg={s['avg_pnl_pct']}%")

    print(f"\n  === BY SOURCE ===")
    for src, s in by_source.items():
        print(f"    {src}: {s['trades']} trades, WR={s['win_rate']:.1%}, Avg={s['avg_pnl_pct']}%")

    # GPT validation (optional — enhances but not required)
    latest_patterns = _get_latest_patterns()
    gpt_prompt = build_gpt_prompt(stats_7d, stats_30d, stats_90d, by_strategy, by_source, latest_patterns)
    gpt_result = call_gpt(gpt_prompt)

    # Save review
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = _now().strftime("%Y%m%d")

    review = {
        "date": date_str,
        "trades_analyzed": len(trades),
        "regime": _load_json(REGIME_LATEST, {}).get("combined_tag", "UNKNOWN"),
        "rolling_7d": stats_7d,
        "rolling_30d": stats_30d,
        "rolling_90d": stats_90d,
        "by_strategy": by_strategy,
        "by_source": by_source,
        "gpt_validation": gpt_result,
        "generated_at": _now().isoformat(),
    }

    _save_json(REVIEWS_DIR / f"weekly_{date_str}.json", review)

    if gpt_result:
        verdict = gpt_result.get("verdict", "UNKNOWN")
        summary = gpt_result.get("summary", "No summary")
        _log(f"GPT Verdict: {verdict}")
        _log(f"Summary: {summary[:200]}")
    else:
        _log("Deterministic stats saved. GPT validation skipped (no API key).")

    _log("=== REVIEW COMPLETE ===")
    return review


if __name__ == "__main__":
    run()
