#!/usr/bin/env python3
"""
Tradeability Scorer — Second gate after Sanad.
Sanad asks "is info reliable?" — this asks "is there exploitable price move?"

100% deterministic Python. NO LLMs.
Scores 0-100, minimum 55 to proceed.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
POSITIONS_FILE = BASE_DIR / "state" / "positions.json"

def score_tradeability(signal: dict) -> int:
    """
    Score signal tradeability across 6 components:
    1. Momentum (0-25)
    2. Volume (0-20)
    3. Liquidity (0-20)
    4. Timing (0-15)
    5. Catalyst (0-10)
    6. Anti-Crowding (0-10)
    
    Total: 0-100, minimum 55 to trade.
    """
    score = 0
    components = {}
    
    # ===== 1. MOMENTUM (0-25) =====
    momentum_score = 0
    
    # For majors with indicators
    indicators = signal.get("indicators", {})
    if indicators:
        rsi = indicators.get("rsi", 50)
        macd_hist = indicators.get("macd_hist", 0)
        
        # RSI distance from extremes (closer to 0 or 100 = stronger)
        rsi_strength = max(50 - abs(rsi - 50), 0) / 50  # 0-1 scale
        momentum_score += rsi_strength * 15  # Max 15 from RSI
        
        # MACD histogram magnitude
        if abs(macd_hist) > 100:
            momentum_score += 10
        elif abs(macd_hist) > 50:
            momentum_score += 5
    
    # For tokens without indicators (CEX majors, Solana)
    else:
        price_1h = signal.get("price_change_1h", 0)
        price_24h = signal.get("price_change_24h", 0)
        
        # Strong moves in either direction = tradeable (mean reversion or momentum)
        if abs(price_1h) > 5:
            momentum_score += 10
        elif abs(price_1h) > 2:
            momentum_score += 5
            
        if abs(price_24h) > 10:
            momentum_score += 10
        elif abs(price_24h) > 5:
            momentum_score += 5
        
        # Bonus for acceleration (1h > 24h means speeding up)
        if price_1h and price_24h and abs(price_1h) > abs(price_24h / 24):
            momentum_score += 5
    
    momentum_score = min(25, int(momentum_score))
    components["momentum"] = momentum_score
    score += momentum_score
    
    # ===== 2. VOLUME (0-20) =====
    volume_score = 0
    
    # Check volume ratio from indicators or use absolute thresholds
    volume_ratio = indicators.get("volume_ratio", 0)
    vol_24h = signal.get("volume_24h", 0)
    
    if volume_ratio > 3:
        volume_score = 20
    elif volume_ratio > 2:
        volume_score = 15
    elif volume_ratio > 1.5:
        volume_score = 10
    # Absolute volume thresholds for majors (no baseline available)
    elif vol_24h > 1_000_000_000:  # > $1B (top majors)
        volume_score = 20
    elif vol_24h > 100_000_000:  # > $100M (mid majors)
        volume_score = 15
    elif vol_24h > 10_000_000:  # > $10M (small caps)
        volume_score = 10
    elif vol_24h > 1_000_000:  # > $1M (micro)
        volume_score = 5
    else:
        volume_score = 0
    
    components["volume"] = volume_score
    score += volume_score
    
    # ===== 3. LIQUIDITY (0-20) =====
    liquidity_score = 0
    
    chain = signal.get("chain", "").lower()
    if chain == "binance":
        # Binance majors = deep books, always max score
        liquidity_score = 20
    else:
        # Solana tokens use liquidity_usd
        liq = signal.get("liquidity_usd", 0)
        if liq >= 500_000:
            liquidity_score = 20
        elif liq >= 100_000:
            liquidity_score = 10
        elif liq >= 50_000:
            liquidity_score = 5
        else:
            liquidity_score = 0
    
    components["liquidity"] = liquidity_score
    score += liquidity_score
    
    # ===== 4. TIMING (0-15) =====
    timing_score = 0
    
    try:
        signal_ts = signal.get("timestamp", "")
        if signal_ts:
            signal_time = datetime.fromisoformat(signal_ts.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - signal_time).total_seconds() / 60
            
            if age_minutes < 5:
                timing_score = 15
            elif age_minutes < 15:
                timing_score = 10
            elif age_minutes < 30:
                timing_score = 5
            else:
                timing_score = 0
    except:
        timing_score = 5  # Default if timestamp parsing fails
    
    components["timing"] = timing_score
    score += timing_score
    
    # ===== 5. CATALYST (0-10) =====
    catalyst_score = 0
    
    source = signal.get("source", "").lower()
    if "whale" in source:
        catalyst_score = 10
    elif "pumpfun" in source:
        catalyst_score = 8
    elif "majors" in source:
        catalyst_score = 7
    elif "birdeye" in source:
        catalyst_score = 4
    elif "coingecko" in source:
        catalyst_score = 3
    elif "dexscreener" in source:
        catalyst_score = 1
    else:
        catalyst_score = 2  # Unknown source
    
    components["catalyst"] = catalyst_score
    score += catalyst_score
    
    # ===== 6. ANTI-CROWDING (0-10) =====
    crowding_score = 10  # Start at max, deduct for crowding
    
    token = signal.get("token", "")
    
    # Check if already in positions
    try:
        with open(POSITIONS_FILE) as f:
            positions_data = json.load(f)
            positions = positions_data.get("positions", positions_data)
            
            for pos in positions:
                if pos.get("status") == "OPEN" and pos.get("token") == token:
                    crowding_score = 0  # Already holding, max crowding penalty
                    break
    except:
        pass  # If can't read positions, assume not crowded
    
    # TODO: Check for multiple signals on same token from different sources in last hour
    # This would require reading signal history (signal_window.json or similar)
    # For now, just use the position check
    
    components["crowding"] = crowding_score
    score += crowding_score
    
    # ===== FINAL SCORE =====
    total_score = min(100, score)
    
    return total_score

def explain_score(signal: dict) -> dict:
    """
    Return detailed breakdown of tradeability score.
    Useful for debugging and transparency.
    """
    components = {}
    total = 0
    
    # Re-calculate each component (duplicates score_tradeability logic)
    # In production, would refactor to avoid duplication
    
    # Simplified version just calls score_tradeability
    # and returns the total
    total = score_tradeability(signal)
    
    return {
        "total_score": total,
        "threshold": 55,
        "tradeable": total >= 55,
        "signal": {
            "token": signal.get("token"),
            "source": signal.get("source"),
            "chain": signal.get("chain")
        }
    }

if __name__ == "__main__":
    # Test cases
    print("=== TRADEABILITY SCORER TEST ===\n")
    
    # Test 1: Whale signal (should be high)
    whale_signal = {
        "source": "whale_tracker",
        "token": "TEST",
        "chain": "solana",
        "price_change_1h": 15,
        "volume_24h": 500000,
        "liquidity_usd": 300000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    whale_score = score_tradeability(whale_signal)
    print(f"Test 1 - Whale signal: {whale_score}/100 {'✅ PASS' if whale_score >= 70 else '❌ FAIL'}")
    print(f"  Expected: 70+, Got: {whale_score}\n")
    
    # Test 2: Stale trending signal (should be low)
    from datetime import timedelta
    old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    stale_signal = {
        "source": "coingecko",
        "token": "TEST",
        "chain": "solana",
        "price_change_1h": 2,
        "volume_24h": 100000,
        "liquidity_usd": 40000,
        "timestamp": old_time,
    }
    stale_score = score_tradeability(stale_signal)
    print(f"Test 2 - Stale signal: {stale_score}/100 {'✅ PASS' if stale_score < 55 else '❌ FAIL'}")
    print(f"  Expected: <55, Got: {stale_score}\n")
    
    # Test 3: Fresh majors signal with strong indicators
    majors_signal = {
        "source": "majors_scanner",
        "token": "BTC",
        "chain": "binance",
        "symbol": "BTCUSDT",
        "volume_24h": 50_000_000_000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "indicators": {
            "rsi": 28,
            "macd_hist": -150,
            "volume_ratio": 2.5,
            "current_price": 95000,
            "bb_lower": 94000
        }
    }
    majors_score = score_tradeability(majors_signal)
    print(f"Test 3 - Majors signal: {majors_score}/100 {'✅ PASS' if majors_score >= 60 else '❌ FAIL'}")
    print(f"  Expected: 60+, Got: {majors_score}\n")
    
    print("=== TEST COMPLETE ===")
