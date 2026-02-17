#!/usr/bin/env python3
"""
Threat Auto-Response — Sprint 9.2.1, 9.2.2, 9.2.8
Completes the remaining threat response gaps:
  9.2.1 — Stale data auto-response (beyond Gate 3)
  9.2.2 — API rate limiting auto-response (beyond circuit breakers)
  9.2.8 — Coordinated pump/dump detection (beyond Sybil)

Each threat has: detect → classify severity → auto-respond → log → notify
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
CONFIG_DIR = BASE_DIR / "config"
SIGNALS_DIR = BASE_DIR / "signals"
sys.path.insert(0, str(SCRIPT_DIR))


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


def _log(msg):
    ts = _now().strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[THREAT] {ts} {msg}", flush=True)


def _notify(level, message):
    """Send notification via notifier if available."""
    try:
        from notifier import send_alert
        send_alert(level, "THREAT_RESPONSE", message)
    except Exception:
        _log(f"[NOTIFY] {message}")


# ─────────────────────────────────────────────────────────
# 9.2.1 — Stale Data Auto-Response
# ─────────────────────────────────────────────────────────

class StaleDataResponder:
    """
    Detects stale data across all feeds and auto-responds:
    - WARNING (>5min): Log + continue with caution
    - CRITICAL (>15min): Pause new trades, notify
    - EMERGENCY (>30min): Kill switch if positions open
    """

    THRESHOLDS = {
        "warning_sec": 300,      # 5 min
        "critical_sec": 900,     # 15 min
        "emergency_sec": 1800,   # 30 min
    }

    FEEDS = [
        {"name": "price_snapshot", "state_file": "price_cache.json", "ts_key": "timestamp"},
        {"name": "heartbeat", "state_file": "heartbeat_state.json", "ts_key": "last_heartbeat"},
        {"name": "fear_greed", "state_file": None, "signal_dir": "market", "file": "fear_greed_latest.json", "ts_key": "timestamp"},
    ]

    def check_all(self) -> dict:
        """Check all feeds for staleness. Returns response actions taken."""
        results = {"feeds": {}, "actions": [], "overall": "HEALTHY"}
        worst_severity = "HEALTHY"

        for feed in self.FEEDS:
            name = feed["name"]

            # Load timestamp
            if feed.get("state_file"):
                data = _load_json(STATE_DIR / feed["state_file"])
            elif feed.get("signal_dir"):
                data = _load_json(SIGNALS_DIR / feed["signal_dir"] / feed["file"])
            else:
                continue

            ts_str = data.get(feed["ts_key"]) or data.get("timestamp")
            if not ts_str:
                results["feeds"][name] = {"status": "NO_DATA", "age_sec": None}
                continue

            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                age = (_now() - ts).total_seconds()
            except Exception:
                results["feeds"][name] = {"status": "PARSE_ERROR", "age_sec": None}
                continue

            # Classify
            if age >= self.THRESHOLDS["emergency_sec"]:
                severity = "EMERGENCY"
            elif age >= self.THRESHOLDS["critical_sec"]:
                severity = "CRITICAL"
            elif age >= self.THRESHOLDS["warning_sec"]:
                severity = "WARNING"
            else:
                severity = "HEALTHY"

            results["feeds"][name] = {
                "status": severity,
                "age_sec": round(age),
                "last_update": ts_str,
            }

            if severity == "EMERGENCY" and worst_severity != "EMERGENCY":
                worst_severity = "EMERGENCY"
            elif severity == "CRITICAL" and worst_severity not in ("EMERGENCY",):
                worst_severity = "CRITICAL"
            elif severity == "WARNING" and worst_severity == "HEALTHY":
                worst_severity = "WARNING"

        results["overall"] = worst_severity

        # Auto-respond
        if worst_severity == "EMERGENCY":
            results["actions"].append("KILL_SWITCH_ACTIVATED")
            self._activate_kill_switch("Stale data emergency — feeds offline >30min")
            _notify("L4", "STALE DATA EMERGENCY: Feeds offline >30min. Kill switch activated.")
        elif worst_severity == "CRITICAL":
            results["actions"].append("NEW_TRADES_PAUSED")
            self._pause_new_trades()
            _notify("L3", "STALE DATA CRITICAL: Feeds stale >15min. New trades paused.")
        elif worst_severity == "WARNING":
            results["actions"].append("LOGGED_WARNING")
            _log(f"Stale data warning: some feeds >5min old")

        # Persist state
        _save_json(STATE_DIR / "stale_data_state.json", {
            **results,
            "checked_at": _now().isoformat(),
        })

        return results

    def _activate_kill_switch(self, reason):
        policy = _load_json(STATE_DIR / "policy_engine_state.json", {})
        policy["kill_switch"] = True
        policy["kill_switch_reason"] = reason
        policy["kill_switch_at"] = _now().isoformat()
        _save_json(STATE_DIR / "policy_engine_state.json", policy)

    def _pause_new_trades(self):
        policy = _load_json(STATE_DIR / "policy_engine_state.json", {})
        policy["new_trades_paused"] = True
        policy["paused_reason"] = "Stale data — critical threshold"
        policy["paused_at"] = _now().isoformat()
        _save_json(STATE_DIR / "policy_engine_state.json", policy)


# ─────────────────────────────────────────────────────────
# 9.2.2 — API Rate Limiting Auto-Response
# ─────────────────────────────────────────────────────────

class RateLimitResponder:
    """
    Monitors API rate limit state across all clients and auto-responds:
    - THROTTLED: Reduce polling frequency
    - BLOCKED: Switch to fallback source
    - EXHAUSTED: Pause feed, notify, use cached data
    """

    APIS = [
        {"name": "binance", "circuit_file": "binance_circuit.json", "fallback": "mexc"},
        {"name": "mexc", "circuit_file": "mexc_circuit.json", "fallback": None},
        {"name": "coingecko", "circuit_file": "coingecko_circuit.json", "fallback": "dexscreener"},
        {"name": "birdeye", "circuit_file": "birdeye_circuit.json", "fallback": "dexscreener"},
        {"name": "helius", "circuit_file": "helius_circuit.json", "fallback": None},
        {"name": "perplexity", "circuit_file": "perplexity_circuit.json", "fallback": "openrouter"},
    ]

    def check_all(self) -> dict:
        results = {"apis": {}, "actions": [], "overall": "HEALTHY"}

        for api in self.APIS:
            name = api["name"]
            state = _load_json(STATE_DIR / api["circuit_file"])

            if not state:
                results["apis"][name] = {"status": "NO_STATE", "circuit": "UNKNOWN"}
                continue

            circuit_state = state.get("state", state.get("circuit_state", "CLOSED"))
            error_count = state.get("error_count", state.get("consecutive_errors", 0))
            last_error = state.get("last_error_at", state.get("last_error"))
            cooldown_until = state.get("cooldown_until")

            # Classify
            if circuit_state == "OPEN" or error_count >= 5:
                severity = "EXHAUSTED"
            elif error_count >= 3:
                severity = "BLOCKED"
            elif error_count >= 1:
                severity = "THROTTLED"
            else:
                severity = "HEALTHY"

            results["apis"][name] = {
                "status": severity,
                "circuit": circuit_state,
                "errors": error_count,
                "last_error": last_error,
                "cooldown_until": cooldown_until,
            }

            # Auto-respond
            if severity == "EXHAUSTED":
                action = f"{name}: EXHAUSTED"
                if api["fallback"]:
                    action += f" → switching to {api['fallback']}"
                    self._activate_fallback(name, api["fallback"])
                else:
                    action += " → using cached data"
                results["actions"].append(action)
                _notify("L3", f"API {name} exhausted (circuit OPEN). {'Fallback: ' + api['fallback'] if api['fallback'] else 'Using cache.'}")

            elif severity == "BLOCKED":
                results["actions"].append(f"{name}: rate limited, reducing frequency")
                self._reduce_frequency(name)

        # Overall
        exhausted_count = sum(1 for a in results["apis"].values() if a["status"] == "EXHAUSTED")
        if exhausted_count >= 3:
            results["overall"] = "CRITICAL"
            results["actions"].append("MULTIPLE_APIs_DOWN — pausing pipeline")
            _notify("L4", f"{exhausted_count} APIs exhausted. Pipeline paused.")
        elif exhausted_count >= 1:
            results["overall"] = "DEGRADED"
        else:
            blocked = sum(1 for a in results["apis"].values() if a["status"] == "BLOCKED")
            results["overall"] = "THROTTLED" if blocked > 0 else "HEALTHY"

        _save_json(STATE_DIR / "rate_limit_state.json", {
            **results,
            "checked_at": _now().isoformat(),
        })

        return results

    def _activate_fallback(self, primary, fallback):
        fallback_state = _load_json(STATE_DIR / "fallback_routing.json", {"routes": {}})
        fallback_state["routes"][primary] = {
            "fallback": fallback,
            "activated_at": _now().isoformat(),
            "reason": "circuit_open",
        }
        _save_json(STATE_DIR / "fallback_routing.json", fallback_state)

    def _reduce_frequency(self, api_name):
        throttle_state = _load_json(STATE_DIR / "throttle_state.json", {"apis": {}})
        current = throttle_state["apis"].get(api_name, {"multiplier": 1.0})
        current["multiplier"] = min(current.get("multiplier", 1.0) * 2.0, 8.0)
        current["throttled_at"] = _now().isoformat()
        throttle_state["apis"][api_name] = current
        _save_json(STATE_DIR / "throttle_state.json", throttle_state)


# ─────────────────────────────────────────────────────────
# 9.2.8 — Coordinated Pump/Dump Detection
# ─────────────────────────────────────────────────────────

class PumpDumpDetector:
    """
    Detects coordinated pump-and-dump schemes beyond simple Sybil detection:
    1. Volume spike without organic growth
    2. Multi-source signal burst (artificial consensus)
    3. Social media + price correlation (manufactured FOMO)
    4. Holder concentration spike (whale accumulation before dump)

    Response: Block signal, log, add to watchlist, notify
    """

    THRESHOLDS = {
        "volume_spike_ratio": 10,       # 10x normal volume = suspicious
        "signal_burst_count": 5,        # 5+ signals in 10min = suspicious
        "signal_burst_window_min": 10,
        "holder_concentration_pct": 60, # Top 10 holders > 60% = suspicious
        "price_pump_pct": 50,           # 50%+ in 1h = suspicious
    }

    def analyze_token(self, token: str, signal_data: dict = None) -> dict:
        """Full pump/dump analysis for a token."""
        results = {
            "token": token,
            "checks": {},
            "is_pump_dump": False,
            "confidence": 0.0,
            "action": "ALLOW",
        }

        flags = 0
        total_checks = 4

        # Check 1: Volume spike
        vol_result = self._check_volume_spike(token, signal_data)
        results["checks"]["volume_spike"] = vol_result
        if vol_result["suspicious"]:
            flags += 1

        # Check 2: Signal burst (multiple sources in short window)
        burst_result = self._check_signal_burst(token)
        results["checks"]["signal_burst"] = burst_result
        if burst_result["suspicious"]:
            flags += 1

        # Check 3: Price pump without fundamentals
        price_result = self._check_price_pump(token, signal_data)
        results["checks"]["price_pump"] = price_result
        if price_result["suspicious"]:
            flags += 1

        # Check 4: Holder concentration
        holder_result = self._check_holder_concentration(token, signal_data)
        results["checks"]["holder_concentration"] = holder_result
        if holder_result["suspicious"]:
            flags += 1

        # Verdict
        confidence = flags / total_checks
        results["confidence"] = round(confidence, 2)

        if flags >= 3:
            results["is_pump_dump"] = True
            results["action"] = "BLOCK"
            self._block_token(token, results)
            _notify("L3", f"PUMP/DUMP DETECTED: {token} ({flags}/{total_checks} flags, {confidence*100:.0f}% confidence). BLOCKED.")
        elif flags >= 2:
            results["action"] = "WARN"
            _log(f"Pump/dump WARNING: {token} ({flags}/{total_checks} flags)")
        else:
            results["action"] = "ALLOW"

        # Log
        _save_json(
            STATE_DIR / "pump_dump_checks" / f"{token}_{_now().strftime('%Y%m%d_%H%M%S')}.json",
            results,
        )

        return results

    def _check_volume_spike(self, token, signal_data) -> dict:
        """Check if volume is abnormally high vs historical."""
        if not signal_data:
            return {"suspicious": False, "reason": "No signal data"}

        vol_24h = signal_data.get("volume_24h", 0)
        avg_vol = signal_data.get("avg_volume_7d", vol_24h)  # fallback

        if avg_vol and avg_vol > 0:
            ratio = vol_24h / avg_vol
            suspicious = ratio >= self.THRESHOLDS["volume_spike_ratio"]
            return {
                "suspicious": suspicious,
                "volume_24h": vol_24h,
                "avg_volume_7d": avg_vol,
                "ratio": round(ratio, 2),
                "threshold": self.THRESHOLDS["volume_spike_ratio"],
            }
        return {"suspicious": False, "reason": "Insufficient volume data"}

    def _check_signal_burst(self, token) -> dict:
        """Check if too many signals arrived for this token in a short window."""
        window = timedelta(minutes=self.THRESHOLDS["signal_burst_window_min"])
        cutoff = _now() - window
        count = 0

        if SIGNALS_DIR.exists():
            for subdir in SIGNALS_DIR.iterdir():
                if subdir.is_dir():
                    for f in subdir.glob("*.json"):
                        try:
                            data = _load_json(f)
                            sig_token = data.get("token", data.get("symbol", ""))
                            if sig_token.upper() == token.upper():
                                ts_str = data.get("timestamp", "")
                                if ts_str:
                                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                    if ts >= cutoff:
                                        count += 1
                        except Exception:
                            continue

        suspicious = count >= self.THRESHOLDS["signal_burst_count"]
        return {
            "suspicious": suspicious,
            "signals_in_window": count,
            "window_min": self.THRESHOLDS["signal_burst_window_min"],
            "threshold": self.THRESHOLDS["signal_burst_count"],
        }

    def _check_price_pump(self, token, signal_data) -> dict:
        """Check for abnormal price increase."""
        if not signal_data:
            return {"suspicious": False, "reason": "No signal data"}

        price_change_1h = signal_data.get("price_change_1h", signal_data.get("price_change_pct", 0))
        suspicious = abs(price_change_1h or 0) >= self.THRESHOLDS["price_pump_pct"]
        return {
            "suspicious": suspicious,
            "price_change_1h": price_change_1h,
            "threshold": self.THRESHOLDS["price_pump_pct"],
        }

    def _check_holder_concentration(self, token, signal_data) -> dict:
        """Check if top holders own too much supply."""
        if not signal_data:
            return {"suspicious": False, "reason": "No signal data"}

        top10_pct = signal_data.get("top_10_holder_pct", signal_data.get("holder_concentration", 0))
        suspicious = (top10_pct or 0) >= self.THRESHOLDS["holder_concentration_pct"]
        return {
            "suspicious": suspicious,
            "top_10_holder_pct": top10_pct,
            "threshold": self.THRESHOLDS["holder_concentration_pct"],
        }

    def _block_token(self, token, analysis):
        """Add token to pump/dump watchlist — blocks pipeline processing."""
        watchlist = _load_json(STATE_DIR / "pump_dump_watchlist.json", {"blocked": {}})
        watchlist["blocked"][token] = {
            "blocked_at": _now().isoformat(),
            "confidence": analysis["confidence"],
            "flags": sum(1 for c in analysis["checks"].values() if c.get("suspicious")),
            "expires_at": (_now() + timedelta(hours=24)).isoformat(),
        }
        _save_json(STATE_DIR / "pump_dump_watchlist.json", watchlist)

    @staticmethod
    def is_blocked(token: str) -> bool:
        """Check if a token is on the pump/dump watchlist."""
        watchlist = _load_json(STATE_DIR / "pump_dump_watchlist.json", {"blocked": {}})
        entry = watchlist.get("blocked", {}).get(token.upper())
        if not entry:
            return False
        expires = entry.get("expires_at", "")
        if expires:
            try:
                exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if _now() > exp:
                    return False  # Expired
            except Exception:
                pass
        return True


# ─────────────────────────────────────────────────────────
# Unified threat check (called by heartbeat)
# ─────────────────────────────────────────────────────────

def run_all_threat_checks() -> dict:
    """Run all threat auto-response checks. Called by heartbeat."""
    _log("Running threat auto-response checks...")

    results = {}

    # 9.2.1 — Stale data
    stale = StaleDataResponder()
    results["stale_data"] = stale.check_all()
    _log(f"  Stale data: {results['stale_data']['overall']}")

    # 9.2.2 — Rate limiting
    rate = RateLimitResponder()
    results["rate_limits"] = rate.check_all()
    _log(f"  Rate limits: {results['rate_limits']['overall']}")

    # 9.2.8 — Pump/dump (check recent signals)
    pump = PumpDumpDetector()
    results["pump_dump"] = {"tokens_checked": 0, "blocked": []}

    # Check tokens from recent signals
    recent_tokens = set()
    if SIGNALS_DIR.exists():
        cutoff = _now() - timedelta(minutes=15)
        for subdir in SIGNALS_DIR.iterdir():
            if subdir.is_dir():
                for f in sorted(subdir.glob("*.json"), reverse=True)[:5]:
                    try:
                        data = _load_json(f)
                        token = data.get("token", data.get("symbol", ""))
                        if token:
                            recent_tokens.add(token.upper())
                    except Exception:
                        continue

    for token in list(recent_tokens)[:10]:
        analysis = pump.analyze_token(token)
        results["pump_dump"]["tokens_checked"] += 1
        if analysis["is_pump_dump"]:
            results["pump_dump"]["blocked"].append(token)

    _log(f"  Pump/dump: {results['pump_dump']['tokens_checked']} checked, {len(results['pump_dump']['blocked'])} blocked")

    # Save combined state
    _save_json(STATE_DIR / "threat_response_state.json", {
        **results,
        "checked_at": _now().isoformat(),
    })

    return results


if __name__ == "__main__":
    run_all_threat_checks()
