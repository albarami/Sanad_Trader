#!/usr/bin/env python3
"""
Helius RPC Client — Sprint 4, Item #16
Deterministic Python. No LLMs.
Uses mainnet.helius-rpc.com RPC endpoint (api.helius.dev DNS blocked in sandbox).

Provides: holder concentration, Sybil detection, token metadata, tx simulation.
"""

import json
import sys
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
CONFIG_ENV = BASE_DIR / "config" / ".env"

# Load API key
HELIUS_API_KEY = ""
if CONFIG_ENV.exists():
    for line in CONFIG_ENV.read_text().splitlines():
        if line.startswith("HELIUS_API_KEY="):
            HELIUS_API_KEY = line.split("=", 1)[1].strip()

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# Rate limiting: max 10 req/s (Helius free tier)
MAX_CALLS_PER_SECOND = 10
_call_timestamps: list[float] = []

# Circuit breaker
_consecutive_failures = 0
_circuit_open_until = 0.0
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN = 300

_rpc_id = 0


def _log(msg: str):
    print(f"[HELIUS] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
def _rate_limit():
    global _call_timestamps
    now = time.time()
    _call_timestamps = [t for t in _call_timestamps if now - t < 1.0]
    if len(_call_timestamps) >= MAX_CALLS_PER_SECOND:
        sleep_for = 1.0 - (now - _call_timestamps[0]) + 0.05
        if sleep_for > 0:
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
        _log(f"Circuit breaker OPENED — pausing for {CIRCUIT_BREAKER_COOLDOWN}s")


def _reset_circuit():
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures = 0
    _circuit_open_until = 0.0


# ---------------------------------------------------------------------------
# RPC helper
# ---------------------------------------------------------------------------
def _rpc(method: str, params=None) -> dict | list | None:
    global _rpc_id
    _check_circuit()
    _rate_limit()
    _rpc_id += 1

    payload = {
        "jsonrpc": "2.0",
        "id": _rpc_id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params

    try:
        resp = requests.post(RPC_URL, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            _log(f"RPC error ({method}): {data['error']}")
            _record_failure()
            return None

        _reset_circuit()
        return data.get("result")
    except requests.exceptions.RequestException as e:
        _log(f"RPC request failed ({method}): {e}")
        _record_failure()
        return None


# ---------------------------------------------------------------------------
# 1. get_token_holders
# ---------------------------------------------------------------------------
def get_token_holders(token_mint: str, limit: int = 50) -> list | None:
    """
    Get largest token holders using getTokenLargestAccounts (returns top 20)
    plus getTokenAccountsByOwner for deeper analysis if needed.
    """
    result = _rpc("getTokenLargestAccounts", [token_mint])
    if not result:
        return None

    accounts = result.get("value", [])
    if not accounts:
        return []

    # Get total supply for percentage calc
    supply_result = _rpc("getTokenSupply", [token_mint])
    total_supply = 0
    if supply_result:
        total_supply = float(supply_result.get("value", {}).get("uiAmount", 0) or 0)

    holders = []
    for acc in accounts[:limit]:
        balance = float(acc.get("uiAmount", 0) or 0)
        pct = (balance / total_supply * 100) if total_supply > 0 else 0
        holders.append({
            "address": acc["address"],
            "balance": balance,
            "percentage_of_supply": round(pct, 4),
        })

    holders.sort(key=lambda x: x["balance"], reverse=True)
    return holders


# ---------------------------------------------------------------------------
# 2. get_holder_concentration
# ---------------------------------------------------------------------------
def get_holder_concentration(token_mint: str, holders: list | None = None) -> dict | None:
    """Analyze holder concentration risk from top holders."""
    if holders is None:
        holders = get_token_holders(token_mint)
    if holders is None:
        return None

    total_pcts = [h["percentage_of_supply"] for h in holders]
    top_10_pct = sum(total_pcts[:10])
    top_20_pct = sum(total_pcts[:20])
    top_50_pct = sum(total_pcts[:50])
    largest = total_pcts[0] if total_pcts else 0

    if top_10_pct > 80:
        risk = "CRITICAL"
    elif top_10_pct > 50:
        risk = "HIGH"
    elif top_10_pct > 30:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    return {
        "top_10_pct": round(top_10_pct, 2),
        "top_20_pct": round(top_20_pct, 2),
        "top_50_pct": round(top_50_pct, 2),
        "holder_count": len(holders),
        "largest_holder_pct": round(largest, 2),
        "concentration_risk": risk,
    }


# ---------------------------------------------------------------------------
# 3. detect_sybil_clusters
# ---------------------------------------------------------------------------
def detect_sybil_clusters(token_mint: str, top_n: int = 30) -> dict | None:
    """
    Detect Sybil clusters among top holders.
    Checks: shared funding source + coordinated buy timing.
    """
    holders = get_token_holders(token_mint, limit=top_n)
    if holders is None:
        return None

    # For each holder, find their first transaction (funding source)
    funding_sources: dict[str, str] = {}  # holder_addr → parent_addr
    first_buy_times: dict[str, int] = {}  # holder_addr → timestamp

    for h in holders[:top_n]:
        addr = h["address"]
        # Get first few signatures for this account
        sigs = _rpc("getSignaturesForAddress", [addr, {"limit": 5}])
        if not sigs:
            continue

        # The last signature in the list is the earliest
        earliest = sigs[-1] if sigs else None
        if earliest and earliest.get("blockTime"):
            first_buy_times[addr] = earliest["blockTime"]

        # Try to get the earliest transaction to find funding source
        if earliest and earliest.get("signature"):
            tx = _rpc("getTransaction", [earliest["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if tx:
                # Look for the fee payer (likely the funding source)
                try:
                    account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    if account_keys:
                        # Fee payer is first account key
                        fee_payer = account_keys[0]
                        if isinstance(fee_payer, dict):
                            fee_payer = fee_payer.get("pubkey", "")
                        if fee_payer and fee_payer != addr:
                            funding_sources[addr] = fee_payer
                except (KeyError, IndexError, TypeError):
                    pass

    # Group by funding source
    parent_to_children: dict[str, list[str]] = defaultdict(list)
    for child, parent in funding_sources.items():
        parent_to_children[parent].append(child)

    # Sybil clusters: parent with 3+ children
    clusters = []
    for parent, children in parent_to_children.items():
        if len(children) >= 3:
            clusters.append({
                "parent_wallet": parent,
                "child_count": len(children),
                "addresses": children,
            })

    # Coordinated timing: 5+ holders bought within 30 minutes
    coordinated_buys = 0
    timestamps = sorted(first_buy_times.values())
    if len(timestamps) >= 5:
        for i in range(len(timestamps) - 4):
            window = timestamps[i + 4] - timestamps[i]
            if window <= 1800:  # 30 minutes
                coordinated_buys = max(coordinated_buys, 5)
                # Count how many are in this window
                count = sum(1 for t in timestamps if timestamps[i] <= t <= timestamps[i] + 1800)
                coordinated_buys = max(coordinated_buys, count)

    # Risk assessment
    if clusters and max(c["child_count"] for c in clusters) >= 5:
        sybil_risk = "HIGH"
    elif clusters or coordinated_buys >= 5:
        sybil_risk = "MEDIUM"
    else:
        sybil_risk = "LOW"

    largest_cluster = max((c["child_count"] for c in clusters), default=0)

    evidence_parts = []
    if clusters:
        evidence_parts.append(f"{len(clusters)} cluster(s) found, largest has {largest_cluster} wallets")
    if coordinated_buys:
        evidence_parts.append(f"{coordinated_buys} holders bought within 30min window")
    if not evidence_parts:
        evidence_parts.append("No clusters or coordinated timing detected")

    return {
        "sybil_risk": sybil_risk,
        "clusters_found": len(clusters),
        "largest_cluster_size": largest_cluster,
        "cluster_details": clusters,
        "coordinated_buys": coordinated_buys,
        "evidence": "; ".join(evidence_parts),
        "holders_analyzed": len(holders[:top_n]),
        "funding_sources_found": len(funding_sources),
    }


# ---------------------------------------------------------------------------
# 4. get_token_metadata
# ---------------------------------------------------------------------------
def get_token_metadata(token_mint: str) -> dict | None:
    """Get token metadata via DAS getAsset."""
    result = _rpc("getAsset", {"id": token_mint})
    if not result:
        return None

    content = result.get("content", {})
    metadata = content.get("metadata", {})
    token_info = result.get("token_info", {})

    # Calculate token age
    # Try to get creation time from authorities or ownership
    created_at = result.get("created_at")
    age_hours = None
    if created_at:
        try:
            # created_at might be a timestamp
            ct = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - ct).total_seconds() / 3600
        except (ValueError, TypeError):
            pass

    supply_raw = token_info.get("supply", 0)
    decimals = token_info.get("decimals", 0)
    supply_ui = supply_raw / (10 ** decimals) if decimals else supply_raw

    return {
        "name": metadata.get("name", "Unknown"),
        "symbol": metadata.get("symbol", "Unknown"),
        "decimals": decimals,
        "supply": supply_ui,
        "supply_raw": supply_raw,
        "is_mutable": result.get("mutable", None),
        "creator": result.get("authorities", [{}])[0].get("address", "Unknown") if result.get("authorities") else "Unknown",
        "token_standard": metadata.get("token_standard", "Unknown"),
        "token_age_hours": round(age_hours, 1) if age_hours else None,
    }


# ---------------------------------------------------------------------------
# 5. simulate_transaction
# ---------------------------------------------------------------------------
def simulate_transaction(encoded_transaction: str) -> dict | None:
    """Simulate a transaction via RPC for pre-flight checks (Gate 8)."""
    result = _rpc("simulateTransaction", [encoded_transaction, {"encoding": "base64"}])
    if result is None:
        return {"success": False, "error": "RPC call failed", "logs": []}

    err = result.get("value", {}).get("err")
    logs = result.get("value", {}).get("logs", [])

    return {
        "success": err is None,
        "error": str(err) if err else None,
        "logs": logs,
    }


# ---------------------------------------------------------------------------
# 6. get_recent_transactions
# ---------------------------------------------------------------------------
def get_recent_transactions(address: str, limit: int = 20) -> list | None:
    """Get recent transactions for an address."""
    sigs = _rpc("getSignaturesForAddress", [address, {"limit": limit}])
    if sigs is None:
        return None

    transactions = []
    # Only fetch details for first 10 to avoid rate limits
    for sig_info in sigs[:min(limit, 10)]:
        sig = sig_info.get("signature")
        block_time = sig_info.get("blockTime")
        ts = datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat() if block_time else None

        transactions.append({
            "signature": sig,
            "timestamp": ts,
            "block_time": block_time,
            "slot": sig_info.get("slot"),
            "err": sig_info.get("err"),
            "memo": sig_info.get("memo"),
        })

    return transactions


# ---------------------------------------------------------------------------
# Standalone report
# ---------------------------------------------------------------------------
def run_report(token_mint: str):
    now = datetime.now(timezone.utc)
    _log(now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    _log(f"Full report for: {token_mint}")
    _log("")

    # 1. Token metadata
    _log("── Token Metadata ──")
    meta = get_token_metadata(token_mint)
    if meta:
        _log(f"  Name: {meta['name']}")
        _log(f"  Symbol: {meta['symbol']}")
        _log(f"  Decimals: {meta['decimals']}")
        _log(f"  Supply: {meta['supply']:,.0f}")
        _log(f"  Mutable: {meta['is_mutable']}")
        _log(f"  Creator: {meta['creator']}")
        _log(f"  Standard: {meta['token_standard']}")
        if meta['token_age_hours']:
            _log(f"  Age: {meta['token_age_hours']:.1f} hours")
    else:
        _log("  ERROR: Could not fetch metadata")
    _log("")

    # 2. Holder concentration (pre-fetch holders to avoid duplicate RPC calls)
    _log("── Holder Concentration ──")
    holders = get_token_holders(token_mint)
    conc = get_holder_concentration(token_mint, holders=holders) if holders else None
    if conc:
        risk_icon = {"LOW": "✅", "MEDIUM": "⚠️", "HIGH": "⛔", "CRITICAL": "💀"}.get(conc["concentration_risk"], "?")
        _log(f"  Top 10 holders: {conc['top_10_pct']:.1f}%")
        _log(f"  Top 20 holders: {conc['top_20_pct']:.1f}%")
        _log(f"  Largest holder: {conc['largest_holder_pct']:.1f}%")
        _log(f"  Holders analyzed: {conc['holder_count']}")
        _log(f"  Concentration risk: {conc['concentration_risk']} {risk_icon}")
    else:
        _log("  ERROR: Could not fetch holder data")
    _log("")

    # 3. Top 10 holders (reuse from above)
    _log("── Top 10 Holders ──")
    if holders:
        for i, h in enumerate(holders[:10], 1):
            addr_short = h["address"][:8] + "..." + h["address"][-4:]
            _log(f"  {i:>2}. {addr_short} — {h['balance']:>18,.2f} ({h['percentage_of_supply']:.2f}%)")
    else:
        _log("  ERROR: Could not fetch holders")
    _log("")

    # 4. Sybil detection
    _log("── Sybil Detection ──")
    _log("  Analyzing top 15 holders (reduced for rate limits)...")
    sybil = detect_sybil_clusters(token_mint, top_n=15)
    if sybil:
        risk_icon = {"LOW": "✅", "MEDIUM": "⚠️", "HIGH": "⛔"}.get(sybil["sybil_risk"], "?")
        _log(f"  Sybil Risk: {sybil['sybil_risk']} {risk_icon}")
        _log(f"  Clusters found: {sybil['clusters_found']}")
        _log(f"  Largest cluster: {sybil['largest_cluster_size']} wallets")
        _log(f"  Coordinated buys: {sybil['coordinated_buys']}")
        _log(f"  Holders analyzed: {sybil['holders_analyzed']}")
        _log(f"  Funding sources traced: {sybil['funding_sources_found']}")
        _log(f"  Evidence: {sybil['evidence']}")
        if sybil["cluster_details"]:
            for c in sybil["cluster_details"]:
                parent_short = c["parent_wallet"][:8] + "..." + c["parent_wallet"][-4:]
                _log(f"    Cluster: {parent_short} → {c['child_count']} children")
    else:
        _log("  ERROR: Could not run Sybil detection")

    _log("")
    _log("Report complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Default: BONK
        mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        _log(f"No address provided, using BONK: {mint}")
    else:
        mint = sys.argv[1]

    try:
        run_report(mint)
    except Exception as e:
        _log(f"FATAL: {e}")
        sys.exit(1)
