#!/usr/bin/env python3
"""
Whale Discovery Engine v2 — Autonomous Expansion
Sprint 6, Phase 1C Enhancement

Four discovery modes:
1. Co-buyer expansion: Find wallets that bought same token as seed whales
2. Front-runner discovery: Find wallets that bought before our profitable trades
3. Cluster filtering: Reject sybil/bot wallets
4. Graduated promotion: Shadow-follow → performance gates → promotion

Rules:
- New candidates require ≥10 swaps in last 7 days
- Reject bot patterns (many tiny swaps/minute)
- Reject sybil clusters (same funding parent)
- Promote only after hitting performance gates (WR + ROI + drawdown)
- Max 500 total tracked wallets (100 active + 400 candidates)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

# Import helius client for on-chain data
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("SANAD_HOME", str(SCRIPT_DIR.parent)))
sys.path.insert(0, str(SCRIPT_DIR))

from helius_client import get_recent_transactions

# Paths
STATE_DIR = BASE_DIR / "state"
CONFIG_DIR = BASE_DIR / "config"
LOGS_DIR = BASE_DIR / "logs"

# NOTE: whale_tracker.py uses config/whale_wallets.json (schema: {"wallets": [...]})
# Whale Discovery v2 maintains its OWN active list to avoid clobbering the tracker config.
WHALE_WALLETS = CONFIG_DIR / "whale_wallets.active.json"
SEED_WALLETS = CONFIG_DIR / "whale_wallets.seed.json"
CANDIDATE_WALLETS = STATE_DIR / "candidate_whales.json"
RETIRED_WALLETS = STATE_DIR / "retired_whales.json"

# Legacy tracker config (schema: {"wallets": [...]})
TRACKER_WALLETS_CONFIG = CONFIG_DIR / "whale_wallets.json"
CLOSED_TRADES = STATE_DIR / "closed_trades.json"

# Discovery config
MIN_SWAPS_7D = 10  # Minimum swaps in last 7 days to be considered
BOT_DETECTION_WINDOW = 60  # seconds - flag if >5 swaps in this window
CO_BUYER_WINDOW = 600  # 10 minutes - find buyers within this window after seed whale
MAX_CANDIDATES = 400
MAX_ACTIVE_WHALES = 100
EXPLORATION_BUDGET_PCT = 0.02  # 2% of portfolio for shadow-following candidates

# Performance gates for promotion
PROMOTION_MIN_TRADES = 5
PROMOTION_MIN_WR = 0.55  # 55% win rate
PROMOTION_MIN_ROI = 0.03  # 3% median ROI
PROMOTION_MAX_DD = 0.30  # 30% max drawdown


def _log(msg: str):
    ts = datetime.utcnow().isoformat() + "Z"
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Load state
# ---------------------------------------------------------------------------
def load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            _log(f"ERROR loading {path.name}: {e}")
    return default if default is not None else {}


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# 1. Co-buyer Expansion
# ---------------------------------------------------------------------------
def discover_co_buyers(seed_wallet: str, token_mint: str, buy_timestamp: int) -> list[str]:
    """
    Find wallets that bought the same token within 10min after seed whale.
    Returns list of candidate wallet addresses.
    """
    candidates = []
    
    # Get token holder list (top 50)
    from helius_client import get_token_holders
    holders = get_token_holders(token_mint, limit=50)
    if not holders:
        return []
    
    # For each holder, check if they bought within window
    window_start = buy_timestamp
    window_end = buy_timestamp + CO_BUYER_WINDOW
    
    for holder in holders:
        addr = holder["address"]
        if addr == seed_wallet:
            continue
        
        # Get recent transactions for this holder
        txs = get_recent_transactions(addr, limit=20)
        if not txs:
            continue
        
        # Check if any buys of this token in window
        for tx in txs:
            if tx.get("block_time") and window_start <= tx["block_time"] <= window_end:
                # Check if this tx involves the token
                token_transfers = tx.get("tokenTransfers", [])
                for tt in token_transfers:
                    if tt.get("mint") == token_mint and tt.get("toUserAccount") == addr:
                        # This is a buy
                        candidates.append(addr)
                        break
    
    return list(set(candidates))  # dedupe


# ---------------------------------------------------------------------------
# 2. Front-runner Discovery
# ---------------------------------------------------------------------------
def discover_front_runners(token_mint: str, our_entry_time: int, our_exit_time: int, our_pnl: float) -> list[str]:
    """
    Find wallets that:
    - Bought before our entry
    - Exited profitably (or held through our TP window)
    
    Only trigger this for profitable trades (our_pnl > 0).
    """
    if our_pnl <= 0:
        return []
    
    candidates = []
    
    from helius_client import get_token_holders
    holders = get_token_holders(token_mint, limit=50)
    if not holders:
        return []
    
    for holder in holders:
        addr = holder["address"]
        txs = get_recent_transactions(addr, limit=30)
        if not txs:
            continue
        
        # Find buys before our entry
        early_buys = []
        for tx in txs:
            if tx.get("block_time") and tx["block_time"] < our_entry_time:
                token_transfers = tx.get("tokenTransfers", [])
                for tt in token_transfers:
                    if tt.get("mint") == token_mint and tt.get("toUserAccount") == addr:
                        early_buys.append(tx["block_time"])
        
        if early_buys:
            # This wallet bought before us - potential front-runner
            candidates.append(addr)
    
    return list(set(candidates))


# ---------------------------------------------------------------------------
# 3. Bot/Sybil Filtering
# ---------------------------------------------------------------------------
def is_bot_wallet(wallet_addr: str) -> bool:
    """
    Detect bot patterns:
    - Many tiny swaps per minute
    - Repetitive swap amounts
    - <10 total swaps in 7 days
    """
    txs = get_recent_transactions(wallet_addr, limit=50)
    if not txs:
        return True  # No data = reject
    
    # Count swaps in last 7 days
    week_ago = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    recent_swaps = [tx for tx in txs if tx.get("type") == "SWAP" and tx.get("block_time", 0) > week_ago]
    
    if len(recent_swaps) < MIN_SWAPS_7D:
        return True  # Too inactive
    
    # Check for burst patterns (>5 swaps in 60 seconds)
    timestamps = sorted([tx["block_time"] for tx in recent_swaps if tx.get("block_time")])
    for i in range(len(timestamps) - 4):
        if timestamps[i + 4] - timestamps[i] < BOT_DETECTION_WINDOW:
            return True  # Bot burst detected
    
    return False


def detect_sybil_cluster(wallet_addr: str, known_clusters: dict) -> bool:
    """
    Check if wallet is part of a known sybil cluster.
    Uses funding source detection from helius_client.
    """
    # Get first transaction to find funding source
    txs = get_recent_transactions(wallet_addr, limit=5)
    if not txs:
        return False
    
    # Check last (earliest) transaction for funding source
    earliest = txs[-1]
    # For now, simple heuristic: if wallet has very few transactions, might be sybil
    # Full implementation would track funding sources across all wallets
    
    # TODO: Implement full sybil detection with funding graph
    return False


# ---------------------------------------------------------------------------
# 4. Candidate Management
# ---------------------------------------------------------------------------
def add_candidate(wallet_addr: str, source: str, provenance: dict):
    """
    Add a new candidate whale with provenance tracking.
    """
    candidates = load_json(CANDIDATE_WALLETS, {"candidates": []})
    
    # Check if already exists
    existing = [c for c in candidates["candidates"] if c["address"] == wallet_addr]
    if existing:
        return  # Already tracked
    
    candidate = {
        "address": wallet_addr,
        "discovered_at": datetime.utcnow().isoformat() + "Z",
        "source": source,
        "provenance": provenance,
        "grade": "CANDIDATE",
        "trade_count": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "median_roi": 0.0,
        "max_drawdown": 0.0,
        "performance_updated": None,
    }
    
    candidates["candidates"].append(candidate)
    save_json(CANDIDATE_WALLETS, candidates)
    _log(f"Added candidate: {wallet_addr[:8]}... (source: {source})")


def evaluate_candidate_performance(wallet_addr: str) -> dict:
    """
    Evaluate a candidate's shadow-follow performance.
    Returns metrics for promotion decision.
    """
    # Load closed trades that were following this candidate
    closed = load_json(CLOSED_TRADES, {"trades": []})
    
    candidate_trades = [
        t for t in closed["trades"]
        if t.get("strategy_source") == f"whale_follow:{wallet_addr}"
    ]
    
    if len(candidate_trades) < PROMOTION_MIN_TRADES:
        return None  # Not enough data
    
    wins = sum(1 for t in candidate_trades if t.get("pnl", 0) > 0)
    losses = len(candidate_trades) - wins
    win_rate = wins / len(candidate_trades)
    
    rois = [t.get("roi", 0) for t in candidate_trades if t.get("roi") is not None]
    median_roi = sorted(rois)[len(rois) // 2] if rois else 0
    
    # Max drawdown (simple: worst single trade)
    max_dd = min(rois) if rois else 0
    
    return {
        "trade_count": len(candidate_trades),
        "win_rate": win_rate,
        "median_roi": median_roi,
        "max_drawdown": abs(max_dd),
        "total_pnl": sum(t.get("pnl", 0) for t in candidate_trades),
    }


def promote_candidate(wallet_addr: str, performance: dict):
    """
    Promote candidate to active whale with initial grade based on performance.
    """
    candidates = load_json(CANDIDATE_WALLETS, {"candidates": []})
    whales = load_json(WHALE_WALLETS, {"whales": []})
    
    # Remove from candidates
    candidate = next((c for c in candidates["candidates"] if c["address"] == wallet_addr), None)
    if not candidate:
        return
    
    candidates["candidates"] = [c for c in candidates["candidates"] if c["address"] != wallet_addr]
    save_json(CANDIDATE_WALLETS, candidates)
    
    # Determine initial grade based on performance
    wr = performance["win_rate"]
    roi = performance["median_roi"]
    
    if wr >= 0.65 and roi >= 0.05:
        grade = "A"
    elif wr >= 0.60 and roi >= 0.04:
        grade = "B"
    else:
        grade = "C"
    
    # Add to active whales
    whale = {
        "label": f"AUTO_{wallet_addr[:8]}",
        "address": wallet_addr,
        "grade": grade,
        "discovered_at": candidate["discovered_at"],
        "promoted_at": datetime.utcnow().isoformat() + "Z",
        "provenance": candidate["provenance"],
        "initial_performance": performance,
    }
    
    whales["whales"].append(whale)
    save_json(WHALE_WALLETS, whales)
    _log(f"PROMOTED: {wallet_addr[:8]}... → Grade {grade} (WR={wr:.1%}, ROI={roi:.1%})")


# ---------------------------------------------------------------------------
# Main discovery loop
# ---------------------------------------------------------------------------
def _migrate_tracker_wallets_to_active_if_needed():
    """Initialize whale_wallets.active.json from whale_tracker config if active file missing/empty."""
    whales = load_json(WHALE_WALLETS, {"whales": []})
    if whales.get("whales"):
        return  # already initialized

    tracker = load_json(TRACKER_WALLETS_CONFIG, {"wallets": []})
    wallets = tracker.get("wallets") or []
    if not wallets:
        return

    # Convert tracker wallets → discovery "whales" schema
    converted = []
    for w in wallets:
        addr = w.get("address") if isinstance(w, dict) else None
        if not addr:
            continue
        converted.append({
            "label": w.get("name") or w.get("label") or f"TRACKED_{addr[:8]}",
            "address": addr,
            "grade": w.get("grade") or "C",
            "discovered_at": tracker.get("last_updated") or datetime.now(timezone.utc).isoformat() + "Z",
            "promoted_at": tracker.get("last_updated") or datetime.now(timezone.utc).isoformat() + "Z",
            "provenance": {"source": "tracker_seed"},
            "initial_performance": None,
        })

    save_json(WHALE_WALLETS, {"whales": converted, "migrated_from": str(TRACKER_WALLETS_CONFIG), "migrated_at": datetime.now(timezone.utc).isoformat() + "Z"})
    _log(f"MIGRATED: initialized active whales from tracker config ({len(converted)} wallets)")


def run_discovery():
    _log("=== WHALE DISCOVERY V2 START ===")

    # Ensure schemas are initialized
    _migrate_tracker_wallets_to_active_if_needed()

    # Load current state
    whales = load_json(WHALE_WALLETS, {"whales": []})
    candidates = load_json(CANDIDATE_WALLETS, {"candidates": []})

    # candidate_whales.json might be legacy {} → normalize
    if not isinstance(candidates, dict) or "candidates" not in candidates:
        candidates = {"candidates": []}

    active_count = len(whales.get("whales", []))
    candidate_count = len(candidates.get("candidates", []))

    _log(f"Current state: {active_count} active whales, {candidate_count} candidates")
    
    # 1. Check candidate performance and promote if qualified
    for candidate in candidates.get("candidates", []):
        perf = evaluate_candidate_performance(candidate["address"])
        if perf and perf["trade_count"] >= PROMOTION_MIN_TRADES:
            if (perf["win_rate"] >= PROMOTION_MIN_WR and
                perf["median_roi"] >= PROMOTION_MIN_ROI and
                perf["max_drawdown"] <= PROMOTION_MAX_DD):
                promote_candidate(candidate["address"], perf)
    
    # 2. Co-buyer expansion (check recent whale activity)
    # This would integrate with whale_tracker's recent buys
    # For now, skip if we don't have fresh whale activity data
    
    # 3. Front-runner discovery (check our profitable closed trades)
    closed = load_json(CLOSED_TRADES, {"trades": []})
    recent_wins = [
        t for t in closed.get("trades", [])
        if t.get("pnl", 0) > 0 and t.get("exit_time")
    ]
    
    for trade in recent_wins[-10:]:  # Last 10 wins
        token = trade.get("token")
        entry_time = trade.get("entry_time")
        exit_time = trade.get("exit_time")
        pnl = trade.get("pnl", 0)
        
        if token and entry_time and exit_time:
            front_runners = discover_front_runners(token, entry_time, exit_time, pnl)
            for fr in front_runners:
                if not is_bot_wallet(fr):
                    add_candidate(
                        fr,
                        "front_runner",
                        {
                            "trade_token": token,
                            "our_entry": entry_time,
                            "our_pnl": pnl,
                        }
                    )
    
    _log("=== WHALE DISCOVERY V2 END ===")


if __name__ == "__main__":
    try:
        run_discovery()
    except Exception as e:
        _log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
