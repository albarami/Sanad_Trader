#!/usr/bin/env python3
"""
Birdeye Smart Money Token List Scanner
Fetches tokens that proven profitable traders are actively buying.
This is whale-following without the complexity of tracking individual wallets.
"""

import os
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# --- CONFIG ---
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY")
BASE_URL = "https://public-api.birdeye.so"

# Smart Money API params
# trader_style: all | risk_averse | risk_balancers | trenchers
# sort_by: smart_traders_no | net_flow | market_cap
SMART_MONEY_ENDPOINT = f"{BASE_URL}/smart-money/v1/token/list"
SMART_MONEY_PARAMS = "interval=1d&trader_style=all&sort_by=smart_traders_no&sort_type=desc&offset=0&limit=50"

BASE_DIR = Path(os.environ.get("SANAD_HOME", Path(__file__).resolve().parent.parent))
STATE_DIR = BASE_DIR / "state"
SIGNAL_QUEUE = STATE_DIR / "signal_queue.json"
SMART_MONEY_STATE = STATE_DIR / "smart_money_state.json"

# --- HELPERS ---
def _log(msg):
    ts = datetime.utcnow().isoformat() + "Z"
    print(f"[SMART-MONEY] {ts} {msg}")


def _load_state():
    if SMART_MONEY_STATE.exists():
        try:
            return json.load(open(SMART_MONEY_STATE))
        except Exception:
            return {}
    return {}


def _save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(SMART_MONEY_STATE, "w") as f:
        json.dump(state, f, indent=2)


def _load_signal_queue():
    if SIGNAL_QUEUE.exists():
        try:
            return json.load(open(SIGNAL_QUEUE))
        except Exception:
            return []
    return []


def _save_signal_queue(signals):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_QUEUE, "w") as f:
        json.dump(signals, f, indent=2)


def fetch_smart_money_tokens():
    """
    Fetch tokens from Birdeye Smart Money Token List.
    Returns list of tokens with smart money activity.
    """
    if not BIRDEYE_API_KEY:
        _log("No BIRDEYE_API_KEY found — skipping")
        return []
    
    url = f"{SMART_MONEY_ENDPOINT}?{SMART_MONEY_PARAMS}"
    headers = {
        "X-API-KEY": BIRDEYE_API_KEY,
        "x-chain": "solana",
        "accept": "application/json"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            
            if result.get("success"):
                items = result.get("data", [])  # API returns data as array directly
                _log(f"Fetched {len(items)} smart money tokens")
                return items
            else:
                _log(f"API returned success=false: {result.get('message', 'unknown')}")
                return []
                
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        
        if e.code == 403:
            _log(f"HTTP 403: Smart Money API requires premium tier or is not yet available")
            _log(f"Error body: {body}")
        else:
            _log(f"HTTP {e.code}: {e.reason}")
        
        return []
    except Exception as e:
        _log(f"Fetch error: {e}")
        return []


def parse_smart_money_signal(item):
    """
    Parse a smart money token item into a signal.
    API response format:
    - token: token address
    - symbol: token symbol
    - name: token name
    - smart_traders_no: number of smart money wallets
    - trader_style: "trenchers" | "risk_averse" | "risk_balancers"
    - price: current price
    - volume_usd: 24h volume
    - price_change_percent: 24h price change %
    - net_flow: net USD flow from smart money
    - liquidity: pool liquidity
    - market_cap: market cap
    """
    address = item.get("token", "")
    symbol = item.get("symbol", "").upper()
    name = item.get("name", "")
    
    smart_money_count = item.get("smart_traders_no", 0)
    trader_style = item.get("trader_style", "unknown")
    
    price = item.get("price", 0)
    volume_24h = item.get("volume_usd", 0)
    price_change_24h = item.get("price_change_percent", 0)
    net_flow = item.get("net_flow", 0)
    liquidity = item.get("liquidity", 0)
    market_cap = item.get("market_cap", 0)
    
    # Build thesis
    thesis_parts = [
        f"{symbol} tracked by {smart_money_count} smart money trader(s).",
    ]
    
    if trader_style and trader_style != "unknown":
        style_map = {
            "trenchers": "aggressive momentum chasers",
            "risk_averse": "conservative accumulators",
            "risk_balancers": "balanced portfolio builders"
        }
        thesis_parts.append(f"Style: {style_map.get(trader_style, trader_style)}.")
    
    if net_flow and net_flow > 0:
        flow_str = f"${net_flow/1e6:.1f}M" if abs(net_flow) >= 1e6 else f"${net_flow/1e3:.0f}K"
        thesis_parts.append(f"Net inflow: {flow_str}.")
    
    if price_change_24h:
        thesis_parts.append(f"{price_change_24h:+.1f}% 24h.")
    
    if volume_24h:
        vol_str = f"${volume_24h/1e6:.1f}M" if volume_24h >= 1e6 else f"${volume_24h/1e3:.0f}K"
        thesis_parts.append(f"Volume {vol_str}.")
    
    if market_cap:
        mc_str = f"${market_cap/1e6:.1f}M" if market_cap >= 1e6 else f"${market_cap/1e3:.0f}K"
        thesis_parts.append(f"Market cap {mc_str}.")
    
    thesis = " ".join(thesis_parts)
    
    signal = {
        "token": symbol,
        "source": "birdeye_smart_money",
        "signal_type": "SMART_MONEY_ACCUMULATION",
        "thesis": thesis,
        "contract_address": address,
        "token_address": address,
        "address": address,
        "chain": "solana",
        "venue": "DEX",
        "exchange": "raydium",  # Most Solana tokens
        "smart_money_signal": True,
        "smart_money_count": smart_money_count,
        "trader_style": trader_style,
        "net_flow": net_flow,
        "price": price,
        "volume_24h": volume_24h,
        "price_change_24h_pct": price_change_24h,
        "liquidity": liquidity,
        "market_cap": market_cap,
        "_origin": "smart_money",
        "_timestamp": datetime.utcnow().isoformat() + "Z"
    }
    
    return signal


def run():
    """Main scanner loop."""
    _log("=== SMART MONEY SCANNER START ===")
    
    # Load state
    state = _load_state()
    last_seen = state.get("last_seen", {})
    
    # Fetch smart money tokens
    items = fetch_smart_money_tokens()
    
    if not items:
        _log("No smart money tokens fetched")
        return
    
    # Load current signal queue
    queue = _load_signal_queue()
    
    # Process each token
    new_signals = 0
    updated_signals = 0
    
    for item in items:
        address = item.get("address", "")
        symbol = item.get("symbol", "").upper()
        
        if not address or not symbol:
            continue
        
        # Parse signal
        signal = parse_smart_money_signal(item)
        
        # Check if we've seen this token recently (< 30 min)
        last_seen_ts = last_seen.get(address, 0)
        now = time.time()
        
        if now - last_seen_ts < 1800:  # 30 minutes
            updated_signals += 1
            continue
        
        # New smart money signal
        queue.append(signal)
        last_seen[address] = now
        new_signals += 1
        
        _log(f"NEW: {symbol} ({signal['smart_money_count']} traders, style={signal['trader_style']})")
    
    # Save updated queue and state
    if new_signals > 0:
        _save_signal_queue(queue)
    
    state["last_seen"] = last_seen
    state["last_run"] = datetime.utcnow().isoformat() + "Z"
    state["tokens_fetched"] = len(items)
    state["new_signals"] = new_signals
    _save_state(state)
    
    _log(f"=== COMPLETE: {new_signals} new, {updated_signals} recent ===")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        _log("Interrupted")
    except Exception as e:
        _log(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
