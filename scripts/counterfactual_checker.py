#!/usr/bin/env python3
"""
Counterfactual Rejection Checker — checks what rejected signals did after rejection.

Runs every 6 hours. For each rejection older than 4 hours that hasn't been checked,
fetches current price and calculates what would have happened.

This tells us:
- If gates are too tight (rejecting winners)
- If gates work (rejecting losers)
- Which rejection reasons correlate with missed opportunities
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
CF_PATH = STATE_DIR / "counterfactual_rejections.json"
REPORT_PATH = BASE_DIR / "genius-memory" / "counterfactual_report.json"

sys.path.insert(0, str(SCRIPT_DIR))


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[CF-CHECK] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    import os
    os.replace(tmp, path)


def run():
    _log("=== COUNTERFACTUAL REJECTION CHECK ===")

    cf_data = _load_json(CF_PATH, {"rejections": []})
    rejections = cf_data.get("rejections", [])

    if not rejections:
        _log("No rejections to check")
        return

    now = _now()
    min_age_hours = 4  # Only check rejections at least 4h old
    checked_count = 0
    missed_winners = 0
    correct_rejections = 0

    try:
        from binance_client import get_price
    except ImportError:
        _log("binance_client not available — skipping")
        return

    for r in rejections:
        if r.get("checked"):
            continue

        # Parse rejection time
        try:
            rejected_at = datetime.fromisoformat(r["rejected_at"])
        except Exception:
            continue

        age_hours = (now - rejected_at).total_seconds() / 3600
        if age_hours < min_age_hours:
            continue

        # Get current price
        entry_price = r.get("price_at_rejection")
        if not entry_price or entry_price <= 0:
            r["checked"] = True
            r["counterfactual_pnl_pct"] = None
            r["verdict"] = "NO_PRICE"
            continue

        try:
            current = get_price(r["symbol"])
            if not current:
                continue
            current = float(current)
        except Exception:
            continue

        pnl_pct = ((current - entry_price) / entry_price) * 100
        r["price_24h_later"] = current
        r["counterfactual_pnl_pct"] = round(pnl_pct, 2)
        r["hours_since_rejection"] = round(age_hours, 1)
        r["checked"] = True

        if pnl_pct > 5:
            r["verdict"] = "MISSED_WINNER"
            missed_winners += 1
            _log(f"  ⚠️ MISSED: {r['token']} rejected at ${entry_price:.4f}, now ${current:.4f} (+{pnl_pct:.1f}%) — {r['rejection_reason']}")
        elif pnl_pct < -5:
            r["verdict"] = "CORRECT_REJECTION"
            correct_rejections += 1
            _log(f"  ✅ CORRECT: {r['token']} rejected at ${entry_price:.4f}, now ${current:.4f} ({pnl_pct:.1f}%)")
        else:
            r["verdict"] = "NEUTRAL"
            _log(f"  ➖ NEUTRAL: {r['token']} {pnl_pct:+.1f}%")

        checked_count += 1

    # Save updated rejections
    cf_data["rejections"] = rejections
    _save_json(CF_PATH, cf_data)

    # Generate report
    all_checked = [r for r in rejections if r.get("checked") and r.get("counterfactual_pnl_pct") is not None]
    if all_checked:
        verdicts = [r.get("verdict", "?") for r in all_checked]
        report = {
            "total_checked": len(all_checked),
            "missed_winners": verdicts.count("MISSED_WINNER"),
            "correct_rejections": verdicts.count("CORRECT_REJECTION"),
            "neutral": verdicts.count("NEUTRAL"),
            "gate_accuracy_pct": round(
                (verdicts.count("CORRECT_REJECTION") + verdicts.count("NEUTRAL")) / len(verdicts) * 100, 1
            ) if verdicts else 0,
            "avg_missed_pnl": round(
                sum(r["counterfactual_pnl_pct"] for r in all_checked if r.get("verdict") == "MISSED_WINNER") /
                max(1, verdicts.count("MISSED_WINNER")), 2
            ),
            "avg_correct_rejection_loss": round(
                sum(r["counterfactual_pnl_pct"] for r in all_checked if r.get("verdict") == "CORRECT_REJECTION") /
                max(1, verdicts.count("CORRECT_REJECTION")), 2
            ),
            "by_reason": {},
            "updated_at": now.isoformat(),
        }
        # Group by rejection reason
        for r in all_checked:
            reason = r.get("rejection_reason", "unknown")
            if reason not in report["by_reason"]:
                report["by_reason"][reason] = {"total": 0, "missed": 0, "correct": 0}
            report["by_reason"][reason]["total"] += 1
            if r.get("verdict") == "MISSED_WINNER":
                report["by_reason"][reason]["missed"] += 1
            elif r.get("verdict") == "CORRECT_REJECTION":
                report["by_reason"][reason]["correct"] += 1

        _save_json(REPORT_PATH, report)
        _log(f"Report: {report['total_checked']} checked, "
             f"{report['missed_winners']} missed winners, "
             f"{report['correct_rejections']} correct rejections, "
             f"gate accuracy {report['gate_accuracy_pct']}%")
    else:
        _log("No checked rejections with price data yet")

    _log(f"=== CHECK COMPLETE: {checked_count} new checks ===")


if __name__ == "__main__":
    run()
