#!/usr/bin/env python3
"""
UCB1 Source Scorer — Sprint 5.2
Deterministic Python. No LLMs.

Implements UCB1 (Upper Confidence Bound 1) algorithm for adaptive source grading.
Each signal source gets a reliability score based on its track record.

UCB1 Formula:
  score = win_rate + sqrt(2 * ln(total_signals_all_sources) / signals_this_source)

The exploration bonus (second term) ensures new/untested sources get a fair trial
before being penalized. As data accumulates, the score converges toward the true win rate.

Cold start: sources with <5 signals get neutral score 50 (Grade C equivalent).

This maps directly to the Sanad Verifier's Jarh wa Ta'dil grading.

Score → Grade mapping:
  >80: Grade A (Thiqah — Fully Trusted)
  60-80: Grade B (Saduq — Mostly Reliable)
  40-60: Grade C (Maqbul — Acceptable)
  20-40: Grade D (Da'if — Weak)
  <20: Grade F (Matruk — Rejected)

Used by:
- Sanad Verifier Step 2: source grading
- post_trade_analyzer.py: update scores after trade closes
- weekly_analysis.py: source performance review
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent  # /data/.openclaw/workspace/trading
SOURCE_DIR = BASE_DIR / "genius-memory" / "source-accuracy"

# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────
COLD_START_THRESHOLD = 5    # Minimum signals before UCB1 kicks in
COLD_START_SCORE = 50       # Neutral score for new sources
UCB1_SCALE = 100            # Scale UCB1 to 0-100 range
EXPLORATION_CONSTANT = 2.0  # sqrt(2) is classic UCB1, can tune


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[UCB1] {ts} {msg}", flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────
# Source File Management
# ─────────────────────────────────────────────────────────
def _source_path(source_name: str) -> Path:
    """Get the path for a source's accuracy file."""
    # Sanitize source name for filesystem
    safe_name = (
        source_name.lower()
        .replace(" ", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace(".", "_")
    )
    # Truncate if too long
    if len(safe_name) > 80:
        safe_name = safe_name[:80]
    return SOURCE_DIR / f"{safe_name}.json"


def _load_source(source_name: str) -> dict:
    """Load a source's accuracy data, or create a new record."""
    path = _source_path(source_name)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _new_source_record(source_name)


def _new_source_record(source_name: str) -> dict:
    """Create a blank source record."""
    return {
        "source_name": source_name,
        "total_signals": 0,
        "trades_executed": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_usd": 0.0,
        "total_pnl_pct": 0.0,
        "avg_pnl_pct": 0.0,
        "win_rate": 0.0,
        "ucb1_score": COLD_START_SCORE,
        "grade": "C",
        "last_signal_at": None,
        "last_trade_at": None,
        "last_win_at": None,
        "last_recalc_at": None,
        "created_at": _now_iso(),
        "history": [],  # Last 20 trade outcomes for review
    }


def _save_source(source_name: str, data: dict):
    """Save a source's accuracy data atomically."""
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    path = _source_path(source_name)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        _log(f"ERROR saving {path}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _load_all_sources() -> dict[str, dict]:
    """Load all source accuracy files."""
    sources = {}
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    for f in SOURCE_DIR.glob("*.json"):
        try:
            with open(f, "r") as fh:
                data = json.load(fh)
            name = data.get("source_name", f.stem)
            sources[name] = data
        except (json.JSONDecodeError, KeyError):
            continue
    return sources


# ─────────────────────────────────────────────────────────
# UCB1 Calculation
# ─────────────────────────────────────────────────────────
def _calc_ucb1(
    wins: int,
    total_trades: int,
    total_all_sources: int,
    exploration: float = EXPLORATION_CONSTANT,
) -> float:
    """Calculate UCB1 score (0-100 scale).

    UCB1 = win_rate + sqrt(exploration * ln(total_all) / trades_this_source)

    Scaled to 0-100 where:
    - Pure win_rate contributes 0-100
    - Exploration bonus adds extra for under-explored sources
    - Capped at 100
    """
    if total_trades <= 0:
        return COLD_START_SCORE

    win_rate = wins / total_trades

    # Exploration bonus
    if total_all_sources <= 0:
        total_all_sources = total_trades

    exploration_bonus = math.sqrt(
        exploration * math.log(max(total_all_sources, 1)) / total_trades
    )

    # Scale: win_rate is 0-1, exploration_bonus can be >1 for low-sample sources
    # Convert to 0-100 scale
    raw = (win_rate + exploration_bonus) * UCB1_SCALE

    # Clamp to 0-100
    return round(min(max(raw, 0), 100), 2)


def _score_to_grade(score: float) -> str:
    """Map UCB1 score to Sanad grading system."""
    if score > 80:
        return "A"   # Thiqah — Fully Trusted
    elif score > 60:
        return "B"   # Saduq — Mostly Reliable
    elif score > 40:
        return "C"   # Maqbul — Acceptable
    elif score > 20:
        return "D"   # Da'if — Weak
    else:
        return "F"   # Matruk — Rejected


# ─────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────
def get_source_score(source_name: str) -> dict:
    """Get current UCB1 score and grade for a source.

    Returns: {"score": float, "grade": str, "total_signals": int, "win_rate": float}
    """
    data = _load_source(source_name)

    if data["trades_executed"] < COLD_START_THRESHOLD:
        return {
            "score": COLD_START_SCORE,
            "grade": "C",
            "total_signals": data["total_signals"],
            "trades_executed": data["trades_executed"],
            "win_rate": data["win_rate"],
            "cold_start": True,
        }

    return {
        "score": data["ucb1_score"],
        "grade": data["grade"],
        "total_signals": data["total_signals"],
        "trades_executed": data["trades_executed"],
        "win_rate": data["win_rate"],
        "cold_start": False,
    }


def record_signal(source_name: str):
    """Record that a signal was received from this source.
    Called when a signal enters the pipeline (before trade decision).
    """
    data = _load_source(source_name)
    data["total_signals"] += 1
    data["last_signal_at"] = _now_iso()
    _save_source(source_name, data)
    _log(f"Signal recorded: {source_name} (total: {data['total_signals']})")


def record_trade_outcome(
    source_name: str,
    is_win: bool,
    pnl_usd: float,
    pnl_pct: float,
    token: str = "",
    trade_id: str = "",
):
    """Record a trade outcome and recalculate UCB1 score.
    Called by post_trade_analyzer.py when a trade closes.
    """
    data = _load_source(source_name)

    # Update counts
    data["trades_executed"] += 1
    if is_win:
        data["wins"] += 1
        data["last_win_at"] = _now_iso()
    else:
        data["losses"] += 1

    data["total_pnl_usd"] = round(data["total_pnl_usd"] + pnl_usd, 4)
    data["total_pnl_pct"] = round(data["total_pnl_pct"] + pnl_pct, 6)

    # Recalculate win rate
    if data["trades_executed"] > 0:
        data["win_rate"] = round(data["wins"] / data["trades_executed"], 4)
        data["avg_pnl_pct"] = round(data["total_pnl_pct"] / data["trades_executed"], 6)

    # Add to history (keep last 20)
    data["history"].append({
        "timestamp": _now_iso(),
        "token": token,
        "trade_id": trade_id,
        "is_win": is_win,
        "pnl_usd": round(pnl_usd, 4),
        "pnl_pct": round(pnl_pct, 6),
    })
    data["history"] = data["history"][-20:]

    data["last_trade_at"] = _now_iso()

    # Recalculate UCB1 (need total across all sources)
    all_sources = _load_all_sources()
    total_all = sum(s.get("trades_executed", 0) for s in all_sources.values())
    # Include current update
    total_all = max(total_all, data["trades_executed"])

    if data["trades_executed"] >= COLD_START_THRESHOLD:
        data["ucb1_score"] = _calc_ucb1(
            data["wins"],
            data["trades_executed"],
            total_all,
        )
        data["grade"] = _score_to_grade(data["ucb1_score"])
    else:
        data["ucb1_score"] = COLD_START_SCORE
        data["grade"] = "C"

    data["last_recalc_at"] = _now_iso()

    _save_source(source_name, data)

    _log(
        f"Trade recorded: {source_name} | "
        f"{'WIN' if is_win else 'LOSS'} {pnl_pct:+.2%} | "
        f"UCB1={data['ucb1_score']:.1f} Grade={data['grade']} | "
        f"W/L={data['wins']}/{data['losses']} ({data['win_rate']:.0%})"
    )


def recalculate_all():
    """Recalculate UCB1 scores for ALL sources.
    Run this weekly or when the formula/constants change.
    """
    all_sources = _load_all_sources()
    if not all_sources:
        _log("No source data to recalculate")
        return {}

    total_all = sum(s.get("trades_executed", 0) for s in all_sources.values())
    _log(f"Recalculating {len(all_sources)} sources (total trades: {total_all})")

    results = {}

    for name, data in all_sources.items():
        trades = data.get("trades_executed", 0)
        wins = data.get("wins", 0)

        if trades >= COLD_START_THRESHOLD:
            old_score = data.get("ucb1_score", COLD_START_SCORE)
            new_score = _calc_ucb1(wins, trades, total_all)
            new_grade = _score_to_grade(new_score)

            data["ucb1_score"] = new_score
            data["grade"] = new_grade
            data["last_recalc_at"] = _now_iso()
            _save_source(name, data)

            change = new_score - old_score
            _log(
                f"  {name}: {old_score:.1f} → {new_score:.1f} "
                f"({change:+.1f}) Grade={new_grade}"
            )
        else:
            _log(f"  {name}: cold start ({trades}/{COLD_START_THRESHOLD} trades)")

        results[name] = {
            "score": data.get("ucb1_score", COLD_START_SCORE),
            "grade": data.get("grade", "C"),
            "trades": trades,
            "wins": wins,
            "win_rate": data.get("win_rate", 0),
        }

    return results


def get_all_scores() -> dict[str, dict]:
    """Get scores for all sources. Returns dict of source_name → score info."""
    all_sources = _load_all_sources()
    return {
        name: {
            "score": data.get("ucb1_score", COLD_START_SCORE),
            "grade": data.get("grade", "C"),
            "total_signals": data.get("total_signals", 0),
            "trades_executed": data.get("trades_executed", 0),
            "win_rate": data.get("win_rate", 0),
            "avg_pnl_pct": data.get("avg_pnl_pct", 0),
            "cold_start": data.get("trades_executed", 0) < COLD_START_THRESHOLD,
        }
        for name, data in all_sources.items()
    }


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "recalc":
        _log("=== FULL RECALCULATION ===")
        results = recalculate_all()
        if results:
            print(f"\n{'Source':<30} {'Score':>6} {'Grade':>6} {'W/L':>8} {'WR':>6}")
            print("-" * 60)
            for name, info in sorted(results.items(), key=lambda x: x[1]["score"], reverse=True):
                wr = f"{info['win_rate']:.0%}" if info["trades"] > 0 else "N/A"
                wl = f"{info['wins']}/{info['trades'] - info['wins']}"
                print(f"{name:<30} {info['score']:>6.1f} {info['grade']:>6} {wl:>8} {wr:>6}")

    elif len(sys.argv) > 1:
        source = " ".join(sys.argv[1:])
        info = get_source_score(source)
        print(f"\n  Source: {source}")
        print(f"UCB1 Score: {info['score']:.1f}")
        print(f"Grade: {info['grade']}")
        print(f"Signals: {info['total_signals']}")
        print(f"Trades: {info['trades_executed']}")
        print(f"Win Rate: {info['win_rate']:.0%}" if info["trades_executed"] > 0 else "Win Rate: N/A")
        print(f"Cold Start: {'Yes' if info['cold_start'] else 'No'}")

    else:
        _log("=== ALL SOURCE SCORES ===")
        scores = get_all_scores()
        if scores:
            print(f"\n{'Source':<30} {'Score':>6} {'Grade':>6} {'Trades':>7} {'WR':>6} {'Cold':>5}")
            print("-" * 65)
            for name, info in sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True):
                wr = f"{info['win_rate']:.0%}" if info["trades_executed"] > 0 else "N/A"
                cold = "Yes" if info["cold_start"] else "No"
                print(f"{name:<30} {info['score']:>6.1f} {info['grade']:>6} {info['trades_executed']:>7} {wr:>6} {cold:>5}")
        else:
            _log("No source data yet. Scores will populate as trades close.")
