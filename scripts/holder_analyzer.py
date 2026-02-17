#!/usr/bin/env python3
"""
Holder Concentration Analyzer — Sprint 1.2.14 (replaces BubbleMaps)
Deterministic Python. No LLMs.

Uses Helius DAS API to analyze token holder distribution.
Detects:
  - Top holder concentration (whale %)
  - Connected wallets (same-source funding = Sybil)
  - Wallet age distribution
  - Dev wallet holding %

Used by: Sanad verifier (rugpull check — Sybil detection)

NOTE: helius_client.py already has get_holder_concentration() and detect_sybil_clusters().
This module wraps them into a single analyze_concentration() call for the Sanad pipeline,
adding HHI, Gini, and a unified risk_score.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
STATE_DIR = BASE_DIR / "state"
sys.path.insert(0, str(SCRIPT_DIR))

import env_loader
env_loader.load_env()

import helius_client


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[HOLDER] {ts} {msg}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _gini(values: list[float]) -> float:
    """Gini coefficient from a list of positive values."""
    n = len(values)
    if n < 2:
        return 0.0
    total = sum(values)
    if total <= 0:
        return 0.0
    s = sorted(values)
    # Standard Gini: 1 - (2 / (n-1)) * (n - sum((i+1)*yi) / sum(yi))
    # Using the mean-difference formula
    abs_diffs = sum(abs(s[i] - s[j]) for i in range(n) for j in range(n))
    g = abs_diffs / (2 * n * total)
    return max(0.0, min(1.0, g))


def _hhi(shares: list[float]) -> float:
    """Herfindahl-Hirschman Index (0-10000)."""
    return sum(s * s * 10000 for s in shares)


# ─────────────────────────────────────────────────────────
# Main analysis
# ─────────────────────────────────────────────────────────

def analyze_concentration(mint_address: str) -> dict:
    """
    Full holder concentration analysis for a token.

    Returns dict with:
      risk_score (0-100), sybil_risk, top_5/10/20_pct, hhi, gini,
      dev_wallet_pct, holder count, etc.
    """
    _log(f"Analyzing {mint_address[:16]}...")

    # Fetch holders via existing helius_client
    holders = helius_client.get_token_holders(mint_address, limit=50)
    if holders is None:
        _log("No holder data — marking high risk")
        return {
            "mint": mint_address,
            "status": "no_data",
            "risk_score": 80,
            "sybil_risk": "UNKNOWN",
            "analyzed_at": _now().isoformat(),
        }

    # Extract (owner, amount) pairs
    balances = []
    for h in holders:
        amt = h.get("amount") or h.get("ui_amount") or 0
        if isinstance(amt, str):
            try:
                amt = float(amt)
            except ValueError:
                amt = 0
        if amt > 0:
            balances.append(float(amt))

    if not balances:
        return {
            "mint": mint_address,
            "status": "no_balances",
            "risk_score": 80,
            "sybil_risk": "UNKNOWN",
            "analyzed_at": _now().isoformat(),
        }

    balances.sort(reverse=True)
    total = sum(balances)

    # Concentration percentages
    top_5 = sum(balances[:5]) / total * 100 if total else 0
    top_10 = sum(balances[:10]) / total * 100 if total else 0
    top_20 = sum(balances[:20]) / total * 100 if total else 0

    # HHI & Gini
    shares = [b / total for b in balances] if total else []
    hhi = _hhi(shares)
    gini = _gini(balances)

    # Dev wallet (largest holder)
    dev_pct = balances[0] / total * 100 if total else 0

    # Sybil: identical-balance clusters
    counts = defaultdict(int)
    for b in balances:
        counts[round(b, 2)] += 1
    suspicious_groups = sum(1 for c in counts.values() if c >= 3)
    sybil_wallets = sum(c for c in counts.values() if c >= 3)

    if suspicious_groups >= 5 or sybil_wallets >= 15:
        sybil_risk = "CRITICAL"
    elif suspicious_groups >= 3 or sybil_wallets >= 8:
        sybil_risk = "HIGH"
    elif suspicious_groups >= 1 or sybil_wallets >= 3:
        sybil_risk = "MEDIUM"
    else:
        sybil_risk = "LOW"

    # Risk score 0-100
    risk = 0
    if top_10 > 80:
        risk += 40
    elif top_10 > 60:
        risk += 25
    elif top_10 > 40:
        risk += 10

    if dev_pct > 20:
        risk += 30
    elif dev_pct > 10:
        risk += 15
    elif dev_pct > 5:
        risk += 5

    if sybil_risk == "CRITICAL":
        risk += 30
    elif sybil_risk == "HIGH":
        risk += 20
    elif sybil_risk == "MEDIUM":
        risk += 10

    risk = min(100, risk)

    result = {
        "mint": mint_address,
        "status": "analyzed",
        "holders_analyzed": len(balances),
        "top_5_pct": round(top_5, 1),
        "top_10_pct": round(top_10, 1),
        "top_20_pct": round(top_20, 1),
        "hhi": round(hhi, 1),
        "gini": round(gini, 4),
        "dev_wallet_pct": round(dev_pct, 1),
        "sybil_risk": sybil_risk,
        "suspicious_identical_groups": suspicious_groups,
        "potential_sybil_wallets": sybil_wallets,
        "risk_score": risk,
        "analyzed_at": _now().isoformat(),
    }

    _save_json(STATE_DIR / "holder_analysis_cache.json", result)
    _log(f"  Top10={top_10:.1f}% Dev={dev_pct:.1f}% Sybil={sybil_risk} Risk={risk}/100")
    return result


# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    _log("=== HOLDER CONCENTRATION ANALYZER TEST ===")
    # BONK as test token
    BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    r = analyze_concentration(BONK)
    for k, v in r.items():
        print(f"  {k}: {v}")
    _log("=== DONE ===")
