#!/usr/bin/env python3
"""
RugCheck API Client — Sprint 3.6
Deterministic Python. No LLMs. No API key needed for read endpoints.
Solana token safety scoring — complements Birdeye security data.

RugCheck scoring: score_normalised 0-100 where LOWER = SAFER (it's a risk score).
We invert it so our rugcheck_score 0-100 = higher is safer (intuitive).
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
SIGNALS_DIR = BASE_DIR / "signals" / "rugcheck"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://api.rugcheck.xyz"

# Rate limiting — be conservative, no documented limits
MAX_CALLS_PER_MINUTE = 20
_call_timestamps: list[float] = []

# Circuit breaker
_consecutive_failures = 0
_circuit_open_until = 0.0
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN = 300


def _log(msg: str):
    print(f"[RUGCHECK] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
def _rate_limit():
    global _call_timestamps
    now = time.time()
    _call_timestamps = [t for t in _call_timestamps if now - t < 60]
    if len(_call_timestamps) >= MAX_CALLS_PER_MINUTE:
        sleep_for = 60 - (now - _call_timestamps[0]) + 1.0
        if sleep_for > 0:
            _log(f"Rate limit: sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
    _call_timestamps.append(time.time())


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
def _check_circuit():
    global _circuit_open_until
    if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        if time.time() < _circuit_open_until:
            remaining = int(_circuit_open_until - time.time())
            raise RuntimeError(f"Circuit breaker OPEN — {remaining}s remaining")
        _reset_circuit()


def _record_failure():
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures += 1
    if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        _circuit_open_until = time.time() + CIRCUIT_BREAKER_COOLDOWN
        _log(f"Circuit breaker OPENED — pausing API calls for {CIRCUIT_BREAKER_COOLDOWN}s")


def _reset_circuit():
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures = 0
    _circuit_open_until = 0.0


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _get(path: str, params: dict | None = None) -> dict | list:
    _check_circuit()
    _rate_limit()
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(url, params=params, timeout=10,
                            headers={"accept": "application/json"})
        if resp.status_code == 429:
            _log("Rate limited (429) — sleeping 5s and retrying once")
            time.sleep(5)
            resp = requests.get(url, params=params, timeout=10,
                                headers={"accept": "application/json"})
        resp.raise_for_status()
        _reset_circuit()
        return resp.json()
    except requests.exceptions.RequestException as e:
        _record_failure()
        raise RuntimeError(f"API error on {url}: {e}") from e


# ---------------------------------------------------------------------------
# Core API functions
# ---------------------------------------------------------------------------
def get_token_summary(mint_address: str) -> dict:
    """GET /v1/tokens/{mint}/report/summary
    Returns: score, score_normalised (0-100, lower=safer), risks[], lpLockedPct, etc.
    """
    return _get(f"/v1/tokens/{mint_address}/report/summary")


def get_token_report(mint_address: str) -> dict:
    """GET /v1/tokens/{mint}/report — full detailed report (heavy)."""
    return _get(f"/v1/tokens/{mint_address}/report")


def get_new_tokens() -> list:
    """GET /v1/stats/new_tokens — recently detected tokens."""
    data = _get("/v1/stats/new_tokens")
    return data if isinstance(data, list) else []


def get_trending() -> list:
    """GET /v1/stats/trending — most voted tokens in 24h."""
    data = _get("/v1/stats/trending")
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Convenience: unified safety assessment
# ---------------------------------------------------------------------------
def check_token_safety(mint_address: str) -> dict:
    """
    Call get_token_summary and return a unified safety assessment.
    rugcheck_score: 0-100 where HIGHER = SAFER (we invert the API's risk score).
    """
    try:
        summary = get_token_summary(mint_address)
    except Exception as e:
        return {
            "mint": mint_address,
            "rugcheck_score": None,
            "risk_level": "Unknown",
            "risks": [f"API error: {e}"],
            "lp_locked_pct": None,
            "token_type": None,
            "safe_to_trade": False,
            "details": {},
        }

    # score_normalised: 0-100 where LOWER = SAFER (risk score)
    raw_risk = summary.get("score_normalised") or summary.get("score", 0)
    # If score_normalised exists and is 0-100 range, use it
    norm = summary.get("score_normalised")
    if norm is not None and 0 <= norm <= 100:
        safety_score = 100 - norm  # invert: higher = safer
    else:
        # Fallback: cap raw score at 100 and invert
        safety_score = max(0, 100 - min(raw_risk, 100))

    lp_locked = summary.get("lpLockedPct") or 0
    token_type = summary.get("tokenType") or summary.get("tokenProgram") or ""

    # Extract risk descriptions
    risks_raw = summary.get("risks") or []
    risk_names = [r.get("name", "") for r in risks_raw]
    risk_descriptions = [
        f"{r.get('name', '')} ({r.get('level', '')}): {r.get('description', '')}"
        for r in risks_raw
    ]

    # Determine risk level
    if safety_score >= 70:
        risk_level = "Good"
    elif safety_score >= 40:
        risk_level = "Warning"
    elif safety_score >= 20:
        risk_level = "Danger"
    else:
        risk_level = "Critical"

    # Determine safe_to_trade
    safe = True

    if safety_score < 30:
        safe = False

    # Check for critical risks
    for r in risks_raw:
        name_lower = (r.get("name") or "").lower()
        level = (r.get("level") or "").lower()
        if level == "danger" and ("mint authority" in name_lower or "freeze authority" in name_lower):
            safe = False

    # LP lock check
    if lp_locked < 50 and safety_score < 50:
        safe = False

    return {
        "mint": mint_address,
        "rugcheck_score": safety_score,
        "risk_level": risk_level,
        "risks": risk_names,
        "risk_details": risk_descriptions,
        "lp_locked_pct": round(lp_locked, 2),
        "token_type": token_type,
        "safe_to_trade": safe,
        "details": summary,
    }


# ---------------------------------------------------------------------------
# Standalone mode
# ---------------------------------------------------------------------------
def run_scan():
    now = datetime.now(timezone.utc)
    ts_label = now.strftime("%Y-%m-%d_%H-%M")
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    _log(now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))

    # 1. Trending
    try:
        trending = get_trending()
        _log(f"Trending tokens: {len(trending)} found")
    except Exception as e:
        _log(f"ERROR fetching trending: {e}")
        trending = []

    # 2. New tokens
    try:
        new_tokens = get_new_tokens()
        _log(f"New tokens: {len(new_tokens)} found")
    except Exception as e:
        _log(f"ERROR fetching new tokens: {e}")
        new_tokens = []

    # 3. Safety check top 5 trending
    safety_results = []
    check_mints = [t.get("mint", "") for t in trending[:5] if t.get("mint")]
    _log(f"Safety checks on top {len(check_mints)} trending:")

    for i, mint in enumerate(check_mints, 1):
        result = check_token_safety(mint)
        safety_results.append(result)
        score = result["rugcheck_score"]
        level = result["risk_level"]
        lp = result["lp_locked_pct"]
        risks = result["risks"]
        safe = result["safe_to_trade"]

        score_str = f"{score}/100" if score is not None else "N/A"
        lp_str = f"LP locked {lp}%" if lp is not None else "LP N/A"
        risk_str = ", ".join(risks) if risks else "none"

        if level == "Good":
            flag = " ✅"
        elif level == "Warning":
            flag = " ⚠️"
        elif level == "Danger":
            flag = " ⛔"
        else:
            flag = " 💀"

        _log(f"  {i}. {mint[:8]}... — score {score_str} ({level}) | {lp_str} | Risks: {risk_str}{flag}")

    # 4. Save
    raw_output = {
        "timestamp": now.isoformat(),
        "trending": trending,
        "new_tokens": new_tokens[:20],
        "safety_checks": safety_results,
    }
    raw_path = SIGNALS_DIR / f"{ts_label}.json"
    raw_path.write_text(json.dumps(raw_output, indent=2, default=str))
    _log(f"Data saved to signals/rugcheck/{ts_label}.json")

    return safety_results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        run_scan()
    except Exception as e:
        _log(f"FATAL: {e}")
        sys.exit(1)
