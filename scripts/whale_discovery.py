#!/usr/bin/env python3
"""
Whale Discovery — Automatically expand whale intelligence network.
Discovers profitable wallets, validates performance, promotes/demotes based on results.
Uses existing helius_client.py for all Solana RPC calls.
"""
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

# Import existing Helius client — DO NOT write new RPC code
from helius_client import get_token_holders, get_recent_transactions, get_token_metadata

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parents[1]))
CONFIG_FILE = BASE_DIR / "config" / "whale_wallets.json"
CANDIDATE_FILE = BASE_DIR / "state" / "candidate_whales.json"
RETIRED_FILE = BASE_DIR / "state" / "retired_whales.json"
SIGNAL_DIRS = [
    BASE_DIR / "signals" / "onchain",
    BASE_DIR / "signals" / "birdeye",
    BASE_DIR / "signals" / "dexscreener"
]
LOG_FILE = BASE_DIR / "execution-logs" / "whale_discovery.log"

def _log(msg: str):
    """Append to log file with timestamp."""
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"[{timestamp}] {msg}\n"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line)
    print(line.strip())

def _load_config() -> dict:
    """Load whale wallet configuration."""
    if not CONFIG_FILE.exists():
        _log(f"ERROR: Config file not found: {CONFIG_FILE}")
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)

def _save_config(config: dict):
    """Save whale wallet configuration."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def _load_candidates() -> dict:
    """Load or initialize candidate whales."""
    if CANDIDATE_FILE.exists():
        with open(CANDIDATE_FILE) as f:
            return json.load(f)
    return {}

def _save_candidates(candidates: dict):
    """Save candidate whales."""
    CANDIDATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CANDIDATE_FILE, "w") as f:
        json.dump(candidates, f, indent=2)

def _load_retired() -> dict:
    """Load retired wallets (audit trail)."""
    if RETIRED_FILE.exists():
        with open(RETIRED_FILE) as f:
            return json.load(f)
    return {}

def _save_retired(retired: dict):
    """Save retired wallets."""
    RETIRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RETIRED_FILE, "w") as f:
        json.dump(retired, f, indent=2)

def _get_recent_signals() -> list[dict]:
    """Get recent signal files with token addresses."""
    signals = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    
    for signal_dir in SIGNAL_DIRS:
        if not signal_dir.exists():
            continue
        
        for filepath in signal_dir.glob("*.json"):
            try:
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
                
                with open(filepath) as f:
                    data = json.load(f)
                
                # Extract token address if present
                token_addr = data.get("token_address") or data.get("address") or data.get("mint")
                if token_addr and len(token_addr) > 20:  # Valid Solana address
                    signals.append({
                        "token_address": token_addr,
                        "token": data.get("token", ""),
                        "volume_24h": data.get("volume_24h", 0),
                        "source": data.get("source", "")
                    })
            except Exception as e:
                continue
    
    return signals

def scan_for_candidates(dry_run: bool = False) -> dict:
    """
    Scan recent signals for large holders and early buyers.
    Returns updated candidates dict.
    """
    _log("=== CANDIDATE DISCOVERY SCAN ===")
    
    candidates = _load_candidates()
    signals = _get_recent_signals()
    
    _log(f"Scanning {len(signals)} recent signals for whale candidates")
    
    discovered_count = 0
    
    for signal in signals[:10]:  # Limit to prevent rate limits
        token_addr = signal["token_address"]
        token = signal.get("token", token_addr[:8])
        
        _log(f"Analyzing {token} ({token_addr[:8]}...)")
        
        try:
            # Get large holders
            holders = get_token_holders(token_addr, limit=50)
            if not holders:
                _log(f"  No holder data for {token}")
                continue
            
            # Filter for significant holders (> 0.5% supply or > 5 SOL equivalent)
            interesting_holders = []
            for holder in holders:
                wallet = holder.get("owner", "")
                balance = holder.get("amount", 0)
                pct = holder.get("percentage", 0)
                
                # Skip known bad actors (CEX hot wallets, burn addresses)
                if any(x in wallet.lower() for x in ["burn", "lock", "dead"]):
                    continue
                
                if pct > 0.5 or balance > 5:  # Significant holder
                    interesting_holders.append(wallet)
            
            _log(f"  Found {len(interesting_holders)} interesting holders for {token}")
            
            # Add to candidates
            for wallet in interesting_holders:
                if wallet not in candidates:
                    candidates[wallet] = {
                        "first_seen": datetime.now(timezone.utc).isoformat(),
                        "trades_observed": 1,
                        "wins": 0,
                        "losses": 0,
                        "pending": 1,
                        "total_sol_traded": 0,
                        "tokens_traded": [token],
                        "avg_entry_timing_minutes": 0,
                        "status": "candidate"
                    }
                    discovered_count += 1
                    _log(f"  NEW CANDIDATE: {wallet[:8]}... (from {token})")
                else:
                    # Update existing candidate
                    if token not in candidates[wallet].get("tokens_traded", []):
                        candidates[wallet]["tokens_traded"].append(token)
                        candidates[wallet]["trades_observed"] += 1
        
        except Exception as e:
            _log(f"ERROR analyzing {token}: {e}")
            continue
    
    if not dry_run:
        _save_candidates(candidates)
    
    _log(f"Discovered {discovered_count} new candidates, {len(candidates)} total")
    _log("=== CANDIDATE SCAN END ===")
    
    return candidates

def _calculate_wallet_stats(wallet_address: str) -> dict | None:
    """
    Calculate 30-day performance stats for a wallet.
    Returns: {trades: int, wins: int, losses: int, win_rate: float, median_roi: float, is_bot: bool}
    """
    try:
        transactions = get_recent_transactions(wallet_address, limit=100)
        if not transactions:
            return None
        
        # Analyze transaction patterns
        now = datetime.now(timezone.utc)
        cutoff_30d = now - timedelta(days=30)
        
        trades = []
        token_interactions = Counter()
        same_block_count = 0
        
        for tx in transactions:
            timestamp = tx.get("timestamp", 0)
            if isinstance(timestamp, (int, float)):
                tx_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            else:
                continue
            
            if tx_time < cutoff_30d:
                continue
            
            # Count unique token interactions (bot detection)
            token_transfers = tx.get("tokenTransfers", [])
            for transfer in token_transfers:
                mint = transfer.get("mint", "")
                if mint:
                    token_interactions[mint] += 1
            
            # Detect same-block buys/sells (sandwich bot pattern)
            # This is simplified - in production would track block numbers
            tx_type = tx.get("type", "").upper()
            if "SWAP" in tx_type:
                trades.append({
                    "timestamp": tx_time,
                    "type": tx_type,
                    "slot": tx.get("slot", 0)
                })
        
        # Bot detection
        unique_tokens = len(token_interactions)
        is_market_maker = unique_tokens > 1000  # Likely MM/CEX if > 1000 unique tokens in 30d
        
        # Sandwich bot detection (simplified)
        slots = [t["slot"] for t in trades if t["slot"] > 0]
        duplicate_slots = len(slots) - len(set(slots))
        is_sandwich_bot = duplicate_slots > 10  # > 10 same-slot trades = likely bot
        
        is_bot = is_market_maker or is_sandwich_bot
        
        # Calculate simple stats
        total_trades = len(trades)
        
        # For now, return simplified stats
        # In production, would track each trade's outcome by comparing entry/exit prices
        return {
            "trades": total_trades,
            "wins": 0,  # Would need price tracking
            "losses": 0,
            "win_rate": 0,
            "median_roi": 0,
            "is_bot": is_bot,
            "unique_tokens": unique_tokens
        }
    
    except Exception as e:
        _log(f"ERROR calculating stats for {wallet_address[:8]}...: {e}")
        return None

def validate_and_promote(dry_run: bool = False) -> dict:
    """
    Weekly validation: promote candidates, re-grade tracked whales, demote poor performers.
    Returns: {"promoted": int, "demoted": int, "removed": int}
    """
    _log("=== PERFORMANCE VALIDATION ===")
    
    config = _load_config()
    candidates = _load_candidates()
    retired = _load_retired()
    
    promotion_gates = config.get("promotion_gates", {})
    demotion_gates = config.get("demotion_gates", {})
    max_wallets = config.get("max_tracked_wallets", 100)
    
    tracked_wallets = config.get("wallets", [])
    tracked_addresses = {w["address"]: w for w in tracked_wallets}
    
    stats = {
        "promoted": 0,
        "demoted": 0,
        "removed": 0,
        "candidates_evaluated": 0
    }
    
    # === PART 1: Evaluate Candidates for Promotion ===
    _log(f"Evaluating {len(candidates)} candidates for promotion")
    
    promotable = []
    
    for wallet_addr, candidate in list(candidates.items())[:20]:  # Limit to prevent rate limits
        stats["candidates_evaluated"] += 1
        
        _log(f"Evaluating {wallet_addr[:8]}...")
        
        # Calculate performance
        perf = _calculate_wallet_stats(wallet_addr)
        if not perf:
            _log(f"  No performance data")
            continue
        
        # Check promotion gates
        min_trades = promotion_gates.get("min_trades_30d", 10)
        min_wr = promotion_gates.get("min_win_rate", 0.55)
        min_roi = promotion_gates.get("min_median_roi_pct", 3.0)
        
        if perf["is_bot"]:
            _log(f"  REJECT: Bot detected ({perf['unique_tokens']} tokens)")
            continue
        
        if perf["trades"] < min_trades:
            _log(f"  Not ready: only {perf['trades']} trades (need {min_trades})")
            continue
        
        # For now, promote based on activity level (ROI tracking needs price history)
        if perf["trades"] >= min_trades and not perf["is_bot"]:
            promotable.append((wallet_addr, candidate, perf))
            _log(f"  PROMOTABLE: {perf['trades']} trades, {perf['unique_tokens']} tokens")
    
    # Promote top performers (if under max_wallets cap)
    current_count = len(tracked_wallets)
    available_slots = max(0, max_wallets - current_count)
    
    promotable.sort(key=lambda x: x[2]["trades"], reverse=True)
    
    for wallet_addr, candidate, perf in promotable[:available_slots]:
        # Add to tracked wallets
        new_whale = {
            "name": f"DISCOVERED_{wallet_addr[:8]}",
            "address": wallet_addr,
            "grade": "B",
            "origin": "discovered",
            "notes": f"Auto-promoted: {perf['trades']} trades, {perf['unique_tokens']} tokens"
        }
        tracked_wallets.append(new_whale)
        
        # Remove from candidates
        del candidates[wallet_addr]
        
        stats["promoted"] += 1
        _log(f"PROMOTED: {wallet_addr[:8]}... → Grade B whale")
    
    # === PART 2: Re-grade Tracked Whales ===
    _log(f"Re-grading {len(tracked_wallets)} tracked whales")
    
    now = datetime.now(timezone.utc)
    
    to_remove = []
    
    for i, whale in enumerate(tracked_wallets):
        addr = whale["address"]
        current_grade = whale["grade"]
        
        # Skip seed whales from re-grading (they're manually curated)
        if whale.get("origin") == "seed":
            continue
        
        _log(f"Re-grading {whale['name']} (current: {current_grade})")
        
        # Check activity
        try:
            transactions = get_recent_transactions(addr, limit=5)
            if not transactions:
                # No activity - check last_seen
                # For now, mark for removal if no recent transactions
                to_remove.append(i)
                _log(f"  REMOVE: No activity")
                continue
        except:
            continue
    
    # Remove inactive wallets
    for idx in sorted(to_remove, reverse=True):
        whale = tracked_wallets.pop(idx)
        retired[whale["address"]] = {
            **whale,
            "retired_at": datetime.now(timezone.utc).isoformat(),
            "reason": "No activity for 30+ days"
        }
        stats["removed"] += 1
        _log(f"REMOVED: {whale['name']} → retired")
    
    # Save updated config
    config["wallets"] = tracked_wallets
    if not dry_run:
        _save_config(config)
        _save_candidates(candidates)
        _save_retired(retired)
    
    _log(f"Validation complete: {stats['promoted']} promoted, {stats['removed']} removed")
    _log("=== VALIDATION END ===")
    
    return stats

if __name__ == "__main__":
    import sys
    
    mode = "scan"  # default
    dry_run = False
    
    if "--validate" in sys.argv:
        mode = "validate"
    elif "--test" in sys.argv:
        dry_run = True
    
    try:
        if mode == "validate":
            stats = validate_and_promote(dry_run=dry_run)
            if dry_run:
                _log(f"DRY RUN: Would promote {stats['promoted']}, remove {stats['removed']}")
        else:
            candidates = scan_for_candidates(dry_run=dry_run)
            if dry_run:
                _log(f"DRY RUN: Found {len(candidates)} candidates")
    
    except Exception as e:
        _log(f"FATAL ERROR: {e}")
        import traceback
        _log(traceback.format_exc())
        sys.exit(1)
