#!/usr/bin/env python3
"""
Weekly Deep Analysis — Sprint 6.1.14
Runs Sunday 06:00 QAT (03:00 UTC).
Orchestrates: pattern_extractor (Opus) + statistical_review (GPT).
Sends weekly intelligence brief via notifier.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[WEEKLY] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def run():
    _log("=== WEEKLY DEEP ANALYSIS ===")

    # Step 1: Pattern extraction (Opus)
    _log("Step 1: Pattern Extraction (Opus)...")
    try:
        import pattern_extractor
        patterns = pattern_extractor.run()
        _log(f"  Patterns: {'Found' if patterns else 'None (insufficient data or API)'}")
    except Exception as e:
        _log(f"  Pattern extraction failed: {e}")
        patterns = None

    # Step 2: Statistical review (GPT)
    _log("Step 2: Statistical Review (GPT)...")
    try:
        import statistical_review
        stats = statistical_review.run()
        _log(f"  Stats: {'Complete' if stats else 'None'}")
    except Exception as e:
        _log(f"  Statistical review failed: {e}")
        stats = None

    # Step 3: UCB1 source recalculation
    _log("Step 3: UCB1 Source Recalculation...")
    try:
        import ucb1_scorer
        ucb1_scorer.recalculate_all()
        _log("  UCB1 scores recalculated")
    except Exception as e:
        _log(f"  UCB1 recalc failed: {e}")

    # Step 4: Counterfactual analysis
    _log("Step 4: Counterfactual Analysis...")
    try:
        import counterfactual
        cf = counterfactual.analyze_all_trades()
        _log(f"  Counterfactuals: {cf.get('total_trades_analyzed', 0)} trades")
    except Exception as e:
        _log(f"  Counterfactual failed: {e}")
        cf = None

    # Step 5: Safety guardrails check (revert degraded strategies)
    _log("Step 5: Safety Guardrails Check...")
    try:
        import safety_guardrails
        strategies = ["meme-momentum", "early-launch", "sentiment-divergence",
                     "whale-following", "cex-listing-play"]
        for strat in strategies:
            reverts = safety_guardrails.check_revert_needed(strat)
            if reverts:
                _log(f"  REVERT: {strat} — {len(reverts)} parameters")
    except Exception as e:
        _log(f"  Guardrails check failed: {e}")

    # Step 6: Generate and send weekly brief
    _log("Step 6: Weekly Intelligence Brief...")
    brief = _build_brief(patterns, stats, cf)

    try:
        import notifier
        notifier.notify_weekly_brief({"summary": brief})
        _log("  Brief sent via Telegram")
    except Exception as e:
        _log(f"  Notification failed: {e}")

    _log("=== WEEKLY ANALYSIS COMPLETE ===")
    return brief


def _build_brief(patterns, stats, cf) -> str:
    """Build the weekly intelligence brief text."""
    lines = ["*Weekly Intelligence Brief*",
             f"Date: {_now().strftime('%Y-%m-%d')}",
             ""]

    if stats:
        r30 = stats.get("rolling_30d", {})
        lines.append(f"*30-Day Performance:*")
        lines.append(f"Trades: {r30.get('trades', 0)}")
        lines.append(f"Win Rate: {r30.get('win_rate', 0):.1%}")
        lines.append(f"Total Return: {r30.get('total_return_pct', 0):+.2f}%")
        lines.append(f"Sharpe: {r30.get('sharpe_ratio', 0):.2f}")
        lines.append(f"Max DD: {r30.get('max_drawdown_pct', 0):.1f}%")
        lines.append("")

        gpt = stats.get("gpt_validation", {})
        if gpt:
            lines.append(f"*GPT Verdict:* {gpt.get('verdict', 'N/A')}")
            lines.append(f"{gpt.get('summary', '')}")
            lines.append("")

    if patterns:
        analysis = patterns.get("analysis", {})
        wp = analysis.get("winning_patterns", [])
        recs = analysis.get("recommendations", [])
        if wp:
            lines.append(f"*Winning Patterns:* {len(wp)} found")
            for p in wp[:3]:
                lines.append(f"• {p.get('pattern', '?')}")
            lines.append("")
        if recs:
            lines.append(f"*Recommendations:* {len(recs)}")
            for r in recs[:3]:
                lines.append(f"• {r.get('action', '?')}")
            lines.append("")

    if cf:
        lines.append(f"*Edge Analysis:*")
        lines.append(f"Trades with significant edge: {cf.get('significant_edges', 0)}/{cf.get('total_trades_analyzed', 0)}")
        lines.append(f"Avg edge vs doing nothing: {cf.get('avg_edge_vs_nothing', 0):+.2f}%")

    return "\n".join(lines)


if __name__ == "__main__":
    run()
