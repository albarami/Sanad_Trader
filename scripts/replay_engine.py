#!/usr/bin/env python3
"""
Replay Engine — Sprint 10.1
Deterministic Python. No LLMs (unless replaying full pipeline).

Replays historical signals through the pipeline to backtest strategies.

Three modes:
1. FAST — Signal → deterministic checks only (no LLM calls)
2. FULL — Signal → complete pipeline (LLM calls, slow, expensive)
3. SHADOW — Record live signals, replay later for comparison

Usage:
    python3 replay_engine.py --mode fast --source signals/
    python3 replay_engine.py --mode fast --file replay_set.json
    python3 replay_engine.py --generate --count 30
"""

import json
import os
import sys
import copy
import time
import hashlib
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
SIGNALS_DIR = BASE_DIR / "signals"
REPLAY_DIR = BASE_DIR / "replay"
REPORTS_DIR = BASE_DIR / "reports" / "replay"
sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[REPLAY] {ts} {msg}", flush=True)


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
# Signal Collection
# ─────────────────────────────────────────────────────────

def collect_signals_from_disk(days: int = 30) -> list:
    """Collect all saved signals from signals/ directory."""
    signals = []
    cutoff = _now() - timedelta(days=days)

    if not SIGNALS_DIR.exists():
        _log("No signals directory found")
        return signals

    for subdir in SIGNALS_DIR.iterdir():
        if not subdir.is_dir():
            continue
        for f in subdir.glob("*.json"):
            data = _load_json(f)
            if not data:
                continue

            # Parse timestamp
            ts_str = data.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            except Exception:
                pass

            data["_source_file"] = str(f)
            data["_source_dir"] = subdir.name
            signals.append(data)

    signals.sort(key=lambda s: s.get("timestamp", ""))
    _log(f"Collected {len(signals)} signals from disk (last {days} days)")
    return signals


def generate_synthetic_signals(count: int = 20) -> list:
    """Generate synthetic signals for testing replay."""
    tokens = [
        {"token": "BONK", "chain": "solana", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"},
        {"token": "WIF", "chain": "solana", "address": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"},
        {"token": "PEPE", "chain": "ethereum", "address": "0x6982508145454Ce325dDbE47a25d4ec3d2311933"},
        {"token": "DOGE", "chain": "binance", "address": "DOGEUSDT"},
        {"token": "SOL", "chain": "solana", "address": "So11111111111111111111111111111111111111112"},
        {"token": "FLOKI", "chain": "binance", "address": "FLOKIUSDT"},
        {"token": "SHIB", "chain": "ethereum", "address": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE"},
        {"token": "MYRO", "chain": "solana", "address": "HhJpBhRRn4g56VsyLuT8DL5Bv31HkXqsrahTTUCZeZg4"},
    ]
    sources = ["coingecko", "dexscreener", "telegram_sniffer", "meme_radar", "fear_greed"]
    directions = ["LONG", "SHORT"]

    signals = []
    base_time = _now() - timedelta(days=7)

    for i in range(count):
        tok = random.choice(tokens)
        source = random.choice(sources)
        direction = random.choice(directions)
        hours_offset = random.uniform(0, 168)  # Up to 7 days

        # Simulate realistic metrics
        volume = random.uniform(10000, 50_000_000)
        mcap = random.uniform(100000, 5_000_000_000)
        price_change = random.uniform(-30, 80)

        signal = {
            "token": tok["token"],
            "source": source,
            "direction": direction,
            "chain": tok["chain"],
            "token_address": tok["address"],
            "thesis": f"{'Bullish' if direction == 'LONG' else 'Bearish'} signal on {tok['token']} — "
                      f"volume spike {volume/1e6:.1f}M, price {'+' if price_change > 0 else ''}{price_change:.1f}%",
            "volume_24h": round(volume, 2),
            "market_cap": round(mcap, 2),
            "price_change_24h": round(price_change, 2),
            "timestamp": (base_time + timedelta(hours=hours_offset)).isoformat(),
            "score": random.randint(30, 95),
            "_synthetic": True,
        }
        signals.append(signal)

    signals.sort(key=lambda s: s.get("timestamp", ""))
    _log(f"Generated {count} synthetic signals")
    return signals


# ─────────────────────────────────────────────────────────
# Fast Replay — Deterministic checks only
# ─────────────────────────────────────────────────────────

def replay_fast(signals: list) -> dict:
    """
    Replay signals through deterministic checks only.
    No LLM calls — tests data quality gates, blacklist, basic filters.
    """
    _log(f"FAST REPLAY: {len(signals)} signals")

    results = {
        "mode": "fast",
        "total_signals": len(signals),
        "passed_stage1": 0,
        "blocked_stage1": 0,
        "blocked_reasons": {},
        "by_source": {},
        "by_token": {},
        "by_direction": {},
        "signals_processed": [],
        "started_at": _now().isoformat(),
    }

    for i, signal in enumerate(signals):
        token = signal.get("token", "UNKNOWN")
        source = signal.get("source", "unknown")
        direction = signal.get("direction", "UNKNOWN")

        outcome = _fast_check(signal)

        # Track results
        result = {
            "index": i,
            "token": token,
            "source": source,
            "direction": direction,
            "passed": outcome["passed"],
            "block_reasons": outcome.get("reasons", []),
            "checks": outcome.get("checks", {}),
        }
        results["signals_processed"].append(result)

        if outcome["passed"]:
            results["passed_stage1"] += 1
        else:
            results["blocked_stage1"] += 1
            for reason in outcome.get("reasons", []):
                results["blocked_reasons"][reason] = results["blocked_reasons"].get(reason, 0) + 1

        # By source
        if source not in results["by_source"]:
            results["by_source"][source] = {"total": 0, "passed": 0, "blocked": 0}
        results["by_source"][source]["total"] += 1
        results["by_source"][source]["passed" if outcome["passed"] else "blocked"] += 1

        # By token
        if token not in results["by_token"]:
            results["by_token"][token] = {"total": 0, "passed": 0, "blocked": 0}
        results["by_token"][token]["total"] += 1
        results["by_token"][token]["passed" if outcome["passed"] else "blocked"] += 1

        # By direction
        if direction not in results["by_direction"]:
            results["by_direction"][direction] = {"total": 0, "passed": 0, "blocked": 0}
        results["by_direction"][direction]["total"] += 1
        results["by_direction"][direction]["passed" if outcome["passed"] else "blocked"] += 1

    results["pass_rate"] = round(results["passed_stage1"] / max(results["total_signals"], 1), 4)
    results["completed_at"] = _now().isoformat()

    return results


def _fast_check(signal: dict) -> dict:
    """Run all deterministic checks on a signal."""
    reasons = []
    checks = {}

    token = signal.get("token", "")
    source = signal.get("source", "")
    direction = signal.get("direction", "")
    address = signal.get("token_address", "")
    chain = signal.get("chain", "")

    # Check 1: Required fields
    if not token or not source or not direction:
        reasons.append("missing_required_fields")
    checks["required_fields"] = len(reasons) == 0

    # Check 2: Negative/impossible values
    price = signal.get("price", 0)
    mcap = signal.get("market_cap", 0)
    volume = signal.get("volume_24h", 0)

    if price is not None and price < 0:
        reasons.append("negative_price")
    if mcap is not None and mcap < 0:
        reasons.append("negative_market_cap")
    checks["value_sanity"] = "negative_price" not in reasons and "negative_market_cap" not in reasons

    # Check 3: Stale signal (>24h)
    ts = signal.get("timestamp")
    if ts:
        try:
            sig_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_hours = (_now() - sig_time).total_seconds() / 3600
            if age_hours > 24:
                reasons.append("stale_signal")
            checks["freshness_hours"] = round(age_hours, 1)
        except Exception:
            checks["freshness_hours"] = "parse_error"

    # Check 4: Blacklist check
    if address:
        try:
            from rugpull_scanner import is_blacklisted
            if is_blacklisted(address):
                reasons.append("blacklisted_token")
        except ImportError:
            pass
    checks["blacklist"] = "blacklisted_token" not in reasons

    # Check 5: Volume/mcap sanity
    if volume and mcap and mcap > 0:
        vol_mcap_ratio = volume / mcap
        if vol_mcap_ratio > 10000:
            reasons.append("impossible_volume_mcap_ratio")
        checks["vol_mcap_ratio"] = round(vol_mcap_ratio, 2)

    # Check 6: Prompt injection in thesis
    thesis = signal.get("thesis", "")
    if thesis:
        try:
            from red_team import _detect_prompt_injection
            if _detect_prompt_injection(thesis):
                reasons.append("prompt_injection_detected")
        except ImportError:
            pass
    checks["prompt_injection"] = "prompt_injection_detected" not in reasons

    # Check 7: Price change sanity
    pct = signal.get("price_change_24h", 0)
    if pct is not None and abs(pct) > 200:
        reasons.append("extreme_price_change")
    checks["price_change_sanity"] = "extreme_price_change" not in reasons

    return {
        "passed": len(reasons) == 0,
        "reasons": reasons,
        "checks": checks,
    }


# ─────────────────────────────────────────────────────────
# Shadow Recording
# ─────────────────────────────────────────────────────────

def record_shadow_signal(signal: dict, pipeline_result: dict):
    """Record a live signal + pipeline result for later replay comparison."""
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)

    record = {
        "signal": signal,
        "pipeline_result": {
            "trust_score": pipeline_result.get("trust_score"),
            "recommendation": pipeline_result.get("recommendation"),
            "judge_verdict": pipeline_result.get("judge_verdict"),
        },
        "recorded_at": _now().isoformat(),
    }

    filename = f"shadow_{_now().strftime('%Y%m%d_%H%M%S')}_{signal.get('token', 'UNK')}.json"
    _save_json(REPLAY_DIR / filename, record)


def load_shadow_signals() -> list:
    """Load all shadow-recorded signals for replay."""
    if not REPLAY_DIR.exists():
        return []

    shadows = []
    for f in sorted(REPLAY_DIR.glob("shadow_*.json")):
        data = _load_json(f)
        if data:
            shadows.append(data)

    _log(f"Loaded {len(shadows)} shadow recordings")
    return shadows


# ─────────────────────────────────────────────────────────
# Report Generation
# ─────────────────────────────────────────────────────────

def generate_report(results: dict) -> dict:
    """Generate a human-readable replay report."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "title": f"Replay Report — {results['mode'].upper()} mode",
        "generated_at": _now().isoformat(),
        "summary": {
            "total_signals": results["total_signals"],
            "passed": results["passed_stage1"],
            "blocked": results["blocked_stage1"],
            "pass_rate": f"{results['pass_rate'] * 100:.1f}%",
        },
        "block_reasons": results.get("blocked_reasons", {}),
        "by_source": results.get("by_source", {}),
        "by_token": results.get("by_token", {}),
        "by_direction": results.get("by_direction", {}),
    }

    filename = f"replay_{_now().strftime('%Y%m%d_%H%M%S')}.json"
    _save_json(REPORTS_DIR / filename, report)
    _save_json(REPORTS_DIR / "latest.json", report)
    _log(f"Report saved: {filename}")

    return report


# ─────────────────────────────────────────────────────────
# Console API endpoint data
# ─────────────────────────────────────────────────────────

def get_replay_summary() -> dict:
    """Get latest replay results for console API."""
    return _load_json(REPORTS_DIR / "latest.json", {"note": "No replay run yet"})


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sanad Trader Replay Engine")
    parser.add_argument("--mode", type=str, default="fast", choices=["fast", "full", "shadow"])
    parser.add_argument("--source", type=str, help="Directory of signal files")
    parser.add_argument("--file", type=str, help="Single replay set JSON file")
    parser.add_argument("--generate", action="store_true", help="Generate synthetic signals")
    parser.add_argument("--count", type=int, default=20, help="Number of synthetic signals")
    parser.add_argument("--days", type=int, default=30, help="Days of history to collect")
    args = parser.parse_args()

    if args.generate:
        signals = generate_synthetic_signals(args.count)
    elif args.file:
        signals = _load_json(args.file, [])
    else:
        signals = collect_signals_from_disk(args.days)

    if not signals:
        _log("No signals to replay — generating synthetic set")
        signals = generate_synthetic_signals(20)

    if args.mode == "fast":
        results = replay_fast(signals)
        report = generate_report(results)

        print(f"\n{'='*50}")
        print(f"REPLAY RESULTS — FAST MODE")
        print(f"{'='*50}")
        print(f"  Total signals:   {results['total_signals']}")
        print(f"  Passed Stage 1:  {results['passed_stage1']}")
        print(f"  Blocked Stage 1: {results['blocked_stage1']}")
        print(f"  Pass rate:       {results['pass_rate']*100:.1f}%")
        if results["blocked_reasons"]:
            print(f"  Block reasons:")
            for reason, count in sorted(results["blocked_reasons"].items(), key=lambda x: -x[1]):
                print(f"    {reason}: {count}")
        if results["by_source"]:
            print(f"  By source:")
            for src, stats in results["by_source"].items():
                rate = stats["passed"] / max(stats["total"], 1) * 100
                print(f"    {src}: {stats['passed']}/{stats['total']} ({rate:.0f}%)")
        print(f"{'='*50}")

    elif args.mode == "shadow":
        shadows = load_shadow_signals()
        if shadows:
            signals = [s["signal"] for s in shadows]
            results = replay_fast(signals)
            generate_report(results)
        else:
            _log("No shadow recordings found")
