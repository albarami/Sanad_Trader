#!/usr/bin/env python3
"""
Whale Tracker — Signal generator from coordinated whale wallet accumulation.
Uses existing helius_client.py for all Solana RPC calls.
"""
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

# Import existing Helius client — DO NOT write new RPC code
from helius_client import get_recent_transactions, get_token_metadata

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parents[1]))
CONFIG_FILE = BASE_DIR / "config" / "whale_wallets.json"
STATE_FILE = BASE_DIR / "state" / "whale_activity.json"
SIGNAL_DIR = BASE_DIR / "signals" / "onchain"
ALERT_FILE = BASE_DIR / "state" / "whale_distribution_alerts.json"
CRON_HEALTH = BASE_DIR / "state" / "cron_health.json"
LOG_FILE = BASE_DIR / "execution-logs" / "whale_tracker.log"

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

def _load_state() -> dict:
    """Load or initialize whale activity state."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def _save_state(state: dict):
    """Save whale activity state."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _update_cron_health(status: str = "ok"):
    """Update cron health timestamp."""
    health = {}
    if CRON_HEALTH.exists():
        with open(CRON_HEALTH) as f:
            health = json.load(f)
    
    health["whale_tracker"] = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "status": status
    }
    
    CRON_HEALTH.parent.mkdir(parents=True, exist_ok=True)
    with open(CRON_HEALTH, "w") as f:
        json.dump(health, f, indent=2)

def _parse_transaction(tx: dict, wallet_address: str) -> dict | None:
    """
    Parse transaction to detect BUY or SELL of a token.
    Returns: {"action": "BUY"|"SELL", "mint": str, "sol_amount": float, "timestamp": str}
    """
    # Simplified parser — looks for token transfers and SOL movements
    # In production, this would use Helius parsed transaction data
    try:
        tx_type = tx.get("type", "").upper()
        timestamp = tx.get("timestamp", datetime.now(timezone.utc).timestamp())
        
        # Convert timestamp to ISO format
        if isinstance(timestamp, (int, float)):
            ts = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        else:
            ts = timestamp
        
        # Helius provides parsed transaction types
        if "SWAP" in tx_type or "TRADE" in tx_type:
            # Check token changes
            token_transfers = tx.get("tokenTransfers", [])
            native_transfers = tx.get("nativeTransfers", [])
            
            for transfer in token_transfers:
                from_addr = transfer.get("fromUserAccount", "")
                to_addr = transfer.get("toUserAccount", "")
                mint = transfer.get("mint", "")
                amount = transfer.get("tokenAmount", 0)
                
                # If wallet received tokens → BUY
                if to_addr == wallet_address and amount > 0:
                    # Find corresponding SOL outflow
                    sol_spent = 0
                    for native in native_transfers:
                        if native.get("fromUserAccount") == wallet_address:
                            sol_spent = native.get("amount", 0) / 1e9  # lamports to SOL
                    
                    if sol_spent > 0.1:  # Minimum 0.1 SOL to count as signal
                        return {
                            "action": "BUY",
                            "mint": mint,
                            "sol_amount": sol_spent,
                            "timestamp": ts
                        }
                
                # If wallet sent tokens → SELL
                if from_addr == wallet_address and amount > 0:
                    return {
                        "action": "SELL",
                        "mint": mint,
                        "sol_amount": 0,  # Don't need SOL amount for sells
                        "timestamp": ts
                    }
        
        return None
    except Exception as e:
        _log(f"ERROR parsing transaction: {e}")
        return None

def _detect_accumulation(state: dict, config: dict) -> list[dict]:
    """
    Detect coordinated accumulation across wallets.
    Returns list of signals to generate.
    """
    signals = []
    now = datetime.now(timezone.utc)
    window = timedelta(hours=config.get("accumulation_window_hours", 6))
    min_wallets = config.get("min_accumulation_wallets", 3)
    min_score = config.get("min_weighted_score", 5.0)
    grade_weights = config.get("grade_weights", {"S": 3.0, "A": 2.0, "B": 1.0, "C": 0.5})
    
    # Group buys by mint
    mint_buys = defaultdict(list)
    
    for wallet_addr, activity in state.items():
        recent = [
            tx for tx in activity.get("transactions", [])
            if tx.get("action") == "BUY" and
            datetime.fromisoformat(tx["timestamp"]) > now - window
        ]
        for tx in recent:
            mint_buys[tx["mint"]].append({
                "wallet": wallet_addr,
                "name": activity.get("name", "Unknown"),
                "grade": activity.get("grade", "C"),
                "sol_amount": tx["sol_amount"],
                "timestamp": tx["timestamp"]
            })
    
    # Check accumulation threshold
    for mint, buys in mint_buys.items():
        unique_wallets = len(set(b["wallet"] for b in buys))
        if unique_wallets >= min_wallets:
            # Calculate weighted score
            weighted_score = sum(grade_weights.get(b["grade"], 0.5) for b in buys)
            total_sol = sum(b["sol_amount"] for b in buys)
            
            if weighted_score >= min_score:
                # Get token metadata
                metadata = get_token_metadata(mint) or {}
                symbol = metadata.get("symbol", mint[:8])
                
                # Calculate accumulation window
                timestamps = [datetime.fromisoformat(b["timestamp"]) for b in buys]
                window_hours = (max(timestamps) - min(timestamps)).total_seconds() / 3600
                
                signal = {
                    "token": symbol,
                    "symbol": f"{symbol}USDT",
                    "source": "whale_tracker",
                    "chain": "solana",
                    "direction": "LONG",
                    "signal_strength": min(95, int(weighted_score * 10)),
                    "score": min(95, int(weighted_score * 10)),
                    "thesis": f"{unique_wallets} tracked whales ({', '.join(b['name'] for b in buys[:3])}) accumulated ${symbol} in last {window_hours:.1f}h. Combined buy: {total_sol:.1f} SOL. Weighted score: {weighted_score:.1f}.",
                    "strategy_hint": "whale-following",
                    "token_address": mint,
                    "volume_24h": 0,
                    "price_change_1h": 0,
                    "liquidity_usd": 0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "whale_details": [
                        {
                            "name": b["name"],
                            "grade": b["grade"],
                            "buy_amount_sol": b["sol_amount"],
                            "timestamp": b["timestamp"]
                        }
                        for b in buys
                    ],
                    "metadata": {
                        "total_whales": unique_wallets,
                        "weighted_score": weighted_score,
                        "total_buy_sol": total_sol,
                        "accumulation_window_hours": window_hours,
                        "any_selling": False
                    }
                }
                signals.append(signal)
                _log(f"ACCUMULATION DETECTED: {symbol} ({mint[:8]}...) — {unique_wallets} whales, {weighted_score:.1f} score, {total_sol:.1f} SOL")
    
    return signals

def _detect_distribution(state: dict) -> list[dict]:
    """
    Detect when 2+ whales are selling same token (distribution warning).
    Returns list of alerts.
    """
    alerts = []
    now = datetime.now(timezone.utc)
    window = timedelta(hours=6)
    
    # Group sells by mint
    mint_sells = defaultdict(list)
    
    for wallet_addr, activity in state.items():
        recent = [
            tx for tx in activity.get("transactions", [])
            if tx.get("action") == "SELL" and
            datetime.fromisoformat(tx["timestamp"]) > now - window
        ]
        for tx in recent:
            mint_sells[tx["mint"]].append({
                "wallet": wallet_addr,
                "name": activity.get("name", "Unknown"),
                "timestamp": tx["timestamp"]
            })
    
    # Check distribution threshold (2+ whales selling)
    for mint, sells in mint_sells.items():
        unique_wallets = len(set(s["wallet"] for s in sells))
        if unique_wallets >= 2:
            alerts.append({
                "mint": mint,
                "wallets_selling": unique_wallets,
                "details": sells,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            _log(f"DISTRIBUTION WARNING: {mint[:8]}... — {unique_wallets} whales selling")
    
    return alerts

def _write_signal(signal: dict):
    """Write signal to signals/onchain/ directory."""
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"whale_tracker_{timestamp}.json"
    filepath = SIGNAL_DIR / filename
    
    with open(filepath, "w") as f:
        json.dump(signal, f, indent=2)
    
    _log(f"Signal written: {filename}")

def run_tracker(test_mode: bool = False):
    """Main tracker logic."""
    _log("=== WHALE TRACKER START ===")
    
    # Load config
    config = _load_config()
    if not config:
        _log("ERROR: Failed to load config")
        _update_cron_health("error")
        return
    
    # Load state
    state = _load_state()
    
    # In test mode, create mock signal
    if test_mode:
        _log("TEST MODE: Creating mock signal")
        mock_signal = {
            "token": "TEST",
            "symbol": "TESTUSDT",
            "source": "whale_tracker",
            "chain": "solana",
            "direction": "LONG",
            "signal_strength": 85,
            "score": 85,
            "thesis": "3 tracked whales (COOKER, EURIS, GOOD TRADER 7) accumulated $TEST in last 4h. Combined buy: 45 SOL. Weighted score: 8.0.",
            "strategy_hint": "whale-following",
            "token_address": "TEST1111111111111111111111111111111111111111",
            "volume_24h": 0,
            "price_change_1h": 0,
            "liquidity_usd": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "whale_details": [
                {"name": "COOKER", "grade": "S", "buy_amount_sol": 20, "timestamp": datetime.now(timezone.utc).isoformat()},
                {"name": "EURIS", "grade": "S", "buy_amount_sol": 15, "timestamp": datetime.now(timezone.utc).isoformat()},
                {"name": "GOOD TRADER 7", "grade": "A", "buy_amount_sol": 10, "timestamp": datetime.now(timezone.utc).isoformat()}
            ],
            "metadata": {
                "total_whales": 3,
                "weighted_score": 8.0,
                "total_buy_sol": 45,
                "accumulation_window_hours": 4,
                "any_selling": False
            }
        }
        _write_signal(mock_signal)
        _log(f"Mock signal created: TEST token, 3 whales, 8.0 weighted score")
        _update_cron_health("ok")
        _log("=== WHALE TRACKER END (TEST) ===")
        return
    
    # Track active wallets (grade B or higher)
    wallets = [w for w in config.get("wallets", []) if w["grade"] in ["S", "A", "B"]]
    _log(f"Tracking {len(wallets)} active wallets (S/A/B grade)")
    
    # Poll each wallet for recent transactions
    for wallet in wallets:
        address = wallet["address"]
        name = wallet["name"]
        grade = wallet["grade"]
        
        _log(f"Polling {name} ({grade}) — {address[:8]}...")
        
        # Get recent transactions via existing helius_client
        try:
            transactions = get_recent_transactions(address, limit=20)
            if not transactions:
                _log(f"  No transactions returned for {name}")
                continue
            
            # Initialize wallet state if new
            if address not in state:
                state[address] = {
                    "name": name,
                    "grade": grade,
                    "transactions": []
                }
            
            # Parse transactions
            new_txs = []
            for tx in transactions:
                parsed = _parse_transaction(tx, address)
                if parsed:
                    new_txs.append(parsed)
            
            # Add new transactions to state (avoid duplicates)
            existing_hashes = set()
            state[address]["transactions"] = [
                tx for tx in state[address].get("transactions", [])
                if datetime.fromisoformat(tx["timestamp"]) > datetime.now(timezone.utc) - timedelta(hours=6)
            ]
            state[address]["transactions"].extend(new_txs)
            
            _log(f"  {name}: {len(new_txs)} new transactions, {len(state[address]['transactions'])} in window")
            
        except Exception as e:
            _log(f"ERROR polling {name}: {e}")
            continue
    
    # Save updated state
    _save_state(state)
    
    # Detect accumulation patterns
    signals = _detect_accumulation(state, config)
    for signal in signals:
        _write_signal(signal)
    
    # Detect distribution warnings
    alerts = _detect_distribution(state)
    if alerts:
        ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERT_FILE, "w") as f:
            json.dump(alerts, f, indent=2)
    
    _log(f"Generated {len(signals)} signals, {len(alerts)} distribution alerts")
    _update_cron_health("ok")
    _log("=== WHALE TRACKER END ===")

if __name__ == "__main__":
    test_mode = "--test" in sys.argv
    try:
        run_tracker(test_mode=test_mode)
    except Exception as e:
        _log(f"FATAL ERROR: {e}")
        import traceback
        _log(traceback.format_exc())
        _update_cron_health("error")
        sys.exit(1)
