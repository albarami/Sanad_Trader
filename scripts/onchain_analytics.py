#!/usr/bin/env python3
"""
On-Chain Analytics — Sprint 3.8.3
Deterministic Python. No LLMs.

Tracks whale movements, exchange flows, and accumulation patterns using FREE APIs:
Helius (Solana), Blockchain.com (BTC), Whale Alert.

Runs every 15 minutes via cron.
Outputs signals to signals/onchain/

Sources:
- Helius: Solana whale wallet monitoring (already configured)
- Blockchain.com: BTC exchange inflows/outflows (free, no key)
- Whale Alert: Large transactions (free tier, no key for basic)
"""
import json
import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
CONFIG_ENV = BASE_DIR / "config" / ".env"
SIGNALS_DIR = BASE_DIR / "signals" / "onchain"
STATE_PATH = BASE_DIR / "state" / "onchain_analytics_state.json"

# Thresholds
BTC_LARGE_TX_USD = 10_000_000      # $10M+ BTC movement = noteworthy
EXCHANGE_FLOW_SIGNAL_PCT = 5        # 5%+ change in exchange reserves = signal
WHALE_ALERT_MIN_USD = 5_000_000     # $5M minimum for Whale Alert


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"[ONCHAIN] {ts} {msg}", flush=True)


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


def _load_helius_key():
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            if line.strip().startswith("HELIUS_API_KEY="):
                return line.strip().split("=", 1)[1].strip().strip('"')
    return os.environ.get("HELIUS_API_KEY", "")


# ─────────────────────────────────────────────────────
# Source 1: Blockchain.com — BTC Exchange Flows (FREE)
# ─────────────────────────────────────────────────────

def get_btc_exchange_data() -> dict | None:
    """Get BTC exchange-related metrics from Blockchain.com free API."""
    try:
        metrics = {}

        # Mempool size (pending transactions — network congestion indicator)
        resp = requests.get("https://api.blockchain.info/charts/mempool-size?timespan=1days&format=json", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            values = data.get("values", [])
            if values:
                latest = values[-1].get("y", 0)
                prev = values[-2].get("y", 0) if len(values) > 1 else latest
                metrics["mempool_bytes"] = latest
                metrics["mempool_change_pct"] = ((latest - prev) / prev * 100) if prev > 0 else 0
        time.sleep(0.5)

        # Hash rate (miner health)
        resp = requests.get("https://api.blockchain.info/charts/hash-rate?timespan=7days&format=json", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            values = data.get("values", [])
            if len(values) >= 2:
                latest = values[-1].get("y", 0)
                week_ago = values[0].get("y", 0)
                metrics["hash_rate_th"] = latest
                metrics["hash_rate_7d_change_pct"] = ((latest - week_ago) / week_ago * 100) if week_ago > 0 else 0
        time.sleep(0.5)

        # Estimated transaction volume USD
        resp = requests.get("https://api.blockchain.info/charts/estimated-transaction-volume-usd?timespan=2days&format=json", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            values = data.get("values", [])
            if len(values) >= 2:
                latest = values[-1].get("y", 0)
                prev = values[-2].get("y", 0) if len(values) > 1 else latest
                metrics["tx_volume_usd"] = latest
                metrics["tx_volume_change_pct"] = ((latest - prev) / prev * 100) if prev > 0 else 0

        if metrics:
            _log(f"  BTC: mempool={metrics.get('mempool_bytes', '?')} hashrate_7d={metrics.get('hash_rate_7d_change_pct', 0):.1f}%")
            return metrics
        return None

    except Exception as e:
        _log(f"  BTC data error: {e}")
        return None


# ─────────────────────────────────────────────────────
# Source 2: Whale Alert (FREE basic — no key needed)
# ─────────────────────────────────────────────────────

def get_whale_alerts() -> list:
    """Check recent large crypto transactions via Whale Alert public feed.
    Free tier: limited to public recent transactions page scraping.
    For real-time: would need API key ($9/month).
    Fallback: Use Blockchain.com large transaction detection.
    """
    alerts = []

    # Blockchain.com large BTC transactions (free)
    try:
        resp = requests.get(
            "https://blockchain.info/unconfirmed-transactions?format=json",
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            txs = data.get("txs", [])

            for tx in txs[:50]:  # Check top 50
                total_output = sum(o.get("value", 0) for o in tx.get("out", []))
                total_btc = total_output / 1e8
                # Rough USD estimate
                btc_price = 68000  # Will be replaced with live price
                total_usd = total_btc * btc_price

                if total_usd >= WHALE_ALERT_MIN_USD:
                    alerts.append({
                        "chain": "bitcoin",
                        "amount_btc": round(total_btc, 4),
                        "amount_usd": round(total_usd),
                        "tx_hash": tx.get("hash", "")[:16] + "...",
                        "inputs": len(tx.get("inputs", [])),
                        "outputs": len(tx.get("out", [])),
                    })

            if alerts:
                _log(f"  Whale alerts: {len(alerts)} large BTC transactions (>${WHALE_ALERT_MIN_USD/1e6:.0f}M)")

    except Exception as e:
        _log(f"  Whale alert error: {e}")

    return alerts


# ─────────────────────────────────────────────────────
# Source 3: Helius — Solana Whale Monitoring
# ─────────────────────────────────────────────────────

def check_sol_whale_activity(api_key: str) -> dict | None:
    """Monitor SOL network activity via Helius."""
    if not api_key:
        return None

    try:
        # Get recent priority fees (network demand indicator)
        resp = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={api_key}",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getRecentPrioritizationFees",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            fees = data.get("result", [])
            if fees:
                avg_fee = sum(f.get("prioritizationFee", 0) for f in fees) / len(fees)
                max_fee = max(f.get("prioritizationFee", 0) for f in fees)
                _log(f"  SOL: avg_priority_fee={avg_fee:.0f} max={max_fee:.0f} ({len(fees)} slots)")
                return {
                    "avg_priority_fee": avg_fee,
                    "max_priority_fee": max_fee,
                    "sample_slots": len(fees),
                    "network_demand": "HIGH" if avg_fee > 10000 else "NORMAL" if avg_fee > 1000 else "LOW",
                }
    except Exception as e:
        _log(f"  SOL whale check error: {e}")

    return None


# ─────────────────────────────────────────────────────
# Signal Generation
# ─────────────────────────────────────────────────────

def _generate_signals(btc_data: dict, whale_alerts: list, sol_data: dict, state: dict) -> list:
    """Generate signals from on-chain data."""
    now = _now()
    signals = []
    cooldowns = state.get("cooldowns", {})

    # Signal 1: BTC transaction volume spike
    if btc_data and abs(btc_data.get("tx_volume_change_pct", 0)) > 20:
        change = btc_data["tx_volume_change_pct"]
        if "btc_volume" not in cooldowns or cooldowns["btc_volume"] < (now - timedelta(hours=2)).isoformat():
            direction = "surge" if change > 0 else "drop"
            signal = {
                "token": "BTC",
                "source": "onchain_analytics",
                "source_detail": f"BTC transaction volume {direction} ({change:+.1f}%)",
                "thesis": f"BTC on-chain transaction volume {direction}d {abs(change):.1f}% in 24h. Large volume changes often precede price moves.",
                "signal_score": min(50 + abs(change) / 2, 80),
                "onchain_data": btc_data,
                "timestamp": now.isoformat(),
            }
            SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
            _save_json(SIGNALS_DIR / f"{now.strftime('%Y%m%d_%H%M')}_btc_volume.json", signal)
            signals.append(signal)
            cooldowns["btc_volume"] = now.isoformat()
            _log(f"  SIGNAL: BTC volume {direction} {abs(change):.1f}%")

    # Signal 2: Large whale transactions
    if len(whale_alerts) >= 3:
        total_usd = sum(a["amount_usd"] for a in whale_alerts)
        if "whale_movement" not in cooldowns or cooldowns["whale_movement"] < (now - timedelta(hours=1)).isoformat():
            signal = {
                "token": "BTC",
                "source": "onchain_analytics",
                "source_detail": f"{len(whale_alerts)} whale transactions (${total_usd/1e6:.0f}M total)",
                "thesis": f"Cluster of {len(whale_alerts)} large BTC transactions detected totaling ${total_usd/1e6:.0f}M. Whale clusters can signal impending volatility.",
                "signal_score": 60,
                "whale_data": whale_alerts[:5],
                "timestamp": now.isoformat(),
            }
            _save_json(SIGNALS_DIR / f"{now.strftime('%Y%m%d_%H%M')}_whale_cluster.json", signal)
            signals.append(signal)
            cooldowns["whale_movement"] = now.isoformat()
            _log(f"  SIGNAL: {len(whale_alerts)} whale txs, ${total_usd/1e6:.0f}M total")

    # Signal 3: SOL network demand spike
    if sol_data and sol_data.get("network_demand") == "HIGH":
        if "sol_demand" not in cooldowns or cooldowns["sol_demand"] < (now - timedelta(hours=1)).isoformat():
            signal = {
                "token": "SOL",
                "source": "onchain_analytics",
                "source_detail": f"High Solana network demand (avg fee={sol_data['avg_priority_fee']:.0f})",
                "thesis": "Solana network priority fees elevated — indicates high demand, likely meme coin activity or DeFi surge.",
                "signal_score": 55,
                "sol_data": sol_data,
                "timestamp": now.isoformat(),
            }
            _save_json(SIGNALS_DIR / f"{now.strftime('%Y%m%d_%H%M')}_sol_demand.json", signal)
            signals.append(signal)
            cooldowns["sol_demand"] = now.isoformat()
            _log(f"  SIGNAL: SOL high network demand")

    state["cooldowns"] = cooldowns
    return signals


# ─────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────

def run():
    now = _now()
    _log("=== ON-CHAIN ANALYTICS SCAN ===")

    state = _load_json(STATE_PATH, {
        "last_run": None,
        "cooldowns": {},
        "total_signals": 0,
    })

    helius_key = _load_helius_key()

    # Source 1: BTC exchange/network data
    btc_data = get_btc_exchange_data()

    # Source 2: Whale alerts
    whale_alerts = get_whale_alerts()

    # Source 3: SOL network activity
    sol_data = check_sol_whale_activity(helius_key)

    # Generate signals
    signals = _generate_signals(btc_data or {}, whale_alerts, sol_data or {}, state)

    # Update state
    state["last_run"] = now.isoformat()
    state["total_signals"] = state.get("total_signals", 0) + len(signals)
    state["last_btc_data"] = btc_data
    state["last_sol_data"] = sol_data
    state["whale_alerts_count"] = len(whale_alerts)
    _save_json(STATE_PATH, state)

    # Write heartbeat file so watchdog knows we ran (even if 0 signals)
    heartbeat_file = SIGNALS_DIR / "_heartbeat.json"
    _save_json(heartbeat_file, {
        "last_run": _now().isoformat(),
        "signals_generated": len(signals),
        "btc_checked": btc_data is not None,
        "sol_checked": sol_data is not None,
        "whale_alerts": len(whale_alerts)
    })

    _log(f"=== SCAN COMPLETE: {len(signals)} signals ===")
    return signals


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        _log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
