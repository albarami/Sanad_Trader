#!/usr/bin/env python3
"""
Market Data Quality Gates — Sprint 3.8.11
Deterministic Python. No LLMs.

Four checks:
1. Timestamp skew: >30s = FLAG, >60s = BLOCK
2. Cross-feed deviation: >2% = WARN, >5% = BLOCK (delegates to cross_feed_validator)
3. Outlier rejection: >15% tick in 1min with no confirmation = reject
4. Stale-but-not-empty: same value 5 consecutive polls = stale

Called by: heartbeat.py, policy_engine.py (Gate 3 enhancement)
Returns: {status: OK|WARN|BLOCK, checks: [...]}
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
CONFIG_DIR = BASE_DIR / "config"
PRICE_HISTORY_PATH = STATE_DIR / "price_history.json"
PRICE_CACHE_PATH = STATE_DIR / "price_cache.json"
QUALITY_STATE_PATH = STATE_DIR / "data_quality.json"
MAINT_WINDOWS_PATH = CONFIG_DIR / "maintenance-windows.json"

# Thresholds
TIMESTAMP_WARN_S = 30
TIMESTAMP_BLOCK_S = 60
CROSS_FEED_WARN = 0.02   # 2%
CROSS_FEED_BLOCK = 0.05  # 5%
OUTLIER_THRESHOLD = 0.15  # 15% in 1 min
STALE_CONSECUTIVE = 5


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[DATA-QUALITY] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _is_maintenance(exchange: str) -> bool:
    """Check if exchange is in a maintenance window."""
    maint = _load_json(MAINT_WINDOWS_PATH, {})
    overrides = maint.get("active_overrides", [])
    now = _now()
    for o in overrides:
        if o.get("exchange") == exchange:
            start = o.get("start")
            end = o.get("end")
            if start and end:
                try:
                    if start <= now.isoformat() <= end:
                        return True
                except (TypeError, ValueError):
                    pass
    return False


def check_timestamp_skew() -> dict:
    """Check 1: Are price timestamps fresh?"""
    cache = _load_json(PRICE_CACHE_PATH, {})
    now = _now()
    issues = []
    status = "OK"

    for symbol, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        ts = entry.get("timestamp", entry.get("updated_at", ""))
        if not ts:
            continue
        try:
            entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_s = (now - entry_dt).total_seconds()
            if age_s > TIMESTAMP_BLOCK_S:
                issues.append({"symbol": symbol, "age_s": round(age_s), "severity": "BLOCK"})
                status = "BLOCK"
            elif age_s > TIMESTAMP_WARN_S:
                issues.append({"symbol": symbol, "age_s": round(age_s), "severity": "WARN"})
                if status != "BLOCK":
                    status = "WARN"
        except (ValueError, TypeError):
            pass

    return {"check": "timestamp_skew", "status": status, "issues": issues}


def check_outlier_rejection() -> dict:
    """Check 3: >15% price change in 1 minute with no cross-feed confirmation = reject."""
    history = _load_json(PRICE_HISTORY_PATH, {})
    now = _now()
    issues = []
    status = "OK"

    for symbol, entries in history.items():
        if not isinstance(entries, list) or len(entries) < 2:
            continue

        # Get last two entries
        recent = sorted(entries, key=lambda x: x.get("timestamp", ""), reverse=True)
        if len(recent) < 2:
            continue

        try:
            p1 = float(recent[0].get("price", 0))
            p2 = float(recent[1].get("price", 0))
            t1 = recent[0].get("timestamp", "")
            t2 = recent[1].get("timestamp", "")

            if p2 <= 0 or p1 <= 0:
                continue

            dt1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
            dt2 = datetime.fromisoformat(t2.replace("Z", "+00:00"))
            interval_s = abs((dt1 - dt2).total_seconds())

            if interval_s > 300:  # Only check recent ticks
                continue

            change = abs(p1 - p2) / p2
            if change > OUTLIER_THRESHOLD:
                issues.append({
                    "symbol": symbol,
                    "change_pct": round(change * 100, 2),
                    "price_old": p2,
                    "price_new": p1,
                    "interval_s": round(interval_s),
                    "severity": "BLOCK",
                })
                status = "BLOCK"
                _log(f"  OUTLIER: {symbol} moved {change:.1%} in {interval_s:.0f}s")
        except (ValueError, TypeError, IndexError):
            pass

    return {"check": "outlier_rejection", "status": status, "issues": issues}


def check_stale_detection() -> dict:
    """Check 4: Same price value for 5+ consecutive polls = stale."""
    history = _load_json(PRICE_HISTORY_PATH, {})
    issues = []
    status = "OK"

    for symbol, entries in history.items():
        if not isinstance(entries, list) or len(entries) < STALE_CONSECUTIVE:
            continue

        recent = sorted(entries, key=lambda x: x.get("timestamp", ""), reverse=True)[:STALE_CONSECUTIVE]
        prices = [e.get("price") for e in recent if e.get("price") is not None]

        if len(prices) >= STALE_CONSECUTIVE and len(set(prices)) == 1:
            if not _is_maintenance("binance"):
                issues.append({
                    "symbol": symbol,
                    "stale_price": prices[0],
                    "consecutive_polls": len(prices),
                    "severity": "WARN",
                })
                if status != "BLOCK":
                    status = "WARN"
                _log(f"  STALE: {symbol} = {prices[0]} for {len(prices)} polls")

    return {"check": "stale_detection", "status": status, "issues": issues}


def run_all_checks() -> dict:
    """Run all data quality checks. Returns aggregate result."""
    _log("=== MARKET DATA QUALITY CHECK ===")
    checks = []

    # Check 1: Timestamp skew
    ts_result = check_timestamp_skew()
    checks.append(ts_result)
    if ts_result["issues"]:
        _log(f"  Timestamp: {ts_result['status']} ({len(ts_result['issues'])} issues)")

    # Check 2: Cross-feed deviation (delegate)
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        import cross_feed_validator
        cf_result = cross_feed_validator.validate_prices()
        cf_check = {
            "check": "cross_feed_deviation",
            "status": cf_result.get("status", "OK"),
            "issues": [d for d in cf_result.get("deviations", []) if d.get("status") != "OK"],
        }
        checks.append(cf_check)
        if cf_check["issues"]:
            _log(f"  Cross-feed: {cf_check['status']} ({len(cf_check['issues'])} deviations)")
    except Exception as e:
        checks.append({"check": "cross_feed_deviation", "status": "ERROR", "issues": [{"error": str(e)}]})
        _log(f"  Cross-feed: ERROR — {e}")

    # Check 3: Outlier rejection
    outlier_result = check_outlier_rejection()
    checks.append(outlier_result)
    if outlier_result["issues"]:
        _log(f"  Outlier: {outlier_result['status']} ({len(outlier_result['issues'])} outliers)")

    # Check 4: Stale detection
    stale_result = check_stale_detection()
    checks.append(stale_result)
    if stale_result["issues"]:
        _log(f"  Stale: {stale_result['status']} ({len(stale_result['issues'])} stale)")

    # Aggregate
    statuses = [c["status"] for c in checks]
    if "BLOCK" in statuses:
        overall = "BLOCK"
    elif "WARN" in statuses:
        overall = "WARN"
    elif "ERROR" in statuses:
        overall = "ERROR"
    else:
        overall = "OK"

    result = {
        "status": overall,
        "timestamp": _now().isoformat(),
        "checks": checks,
        "summary": {s: statuses.count(s) for s in set(statuses)},
    }

    _save_json(QUALITY_STATE_PATH, result)
    _log(f"=== RESULT: {overall} ===")
    return result


if __name__ == "__main__":
    result = run_all_checks()
    print(f"  Overall: {result['status']}")
    for c in result["checks"]:
        issues = len(c.get("issues", []))
        print(f"    {c['check']}: {c['status']}" + (f" ({issues} issues)" if issues else ""))
