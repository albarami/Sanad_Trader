#!/usr/bin/env python3
"""
Paper Execution Module - Exchange-agnostic paper trading

Handles paper trade execution WITHOUT requiring live exchange connectivity.
Used by sanad_pipeline Stage 7 when portfolio.mode == "paper".

Key principles:
1. Never block on exchange API failures
2. Use prices from decision packet (already validated in earlier stages)
3. Support both CEX and DEX tokens
4. Deterministic fills with realistic slippage simulation
"""

import json
import time
import random
from datetime import datetime, timezone
from pathlib import Path

def execute_paper_trade(
    token: str,
    symbol: str,
    side: str,
    quantity: float,
    decision_price: float,
    venue: str = "CEX",
    exchange: str = "binance",
    liquidity_usd: float = None
) -> dict:
    """
    Execute a paper trade without touching any exchange API.
    
    Args:
        token: Token symbol (e.g., 'BTC', 'BP')
        symbol: Trading pair (e.g., 'BTCUSDT', 'BP/USDT')
        side: 'BUY' or 'SELL' 
        quantity: Amount to trade
        decision_price: Price from decision packet (already validated)
        venue: 'CEX' or 'DEX'
        exchange: Target exchange name (for logging only)
        liquidity_usd: Liquidity for slippage calc (optional)
        
    Returns:
        dict with order result or error
    """
    
    if not decision_price or decision_price <= 0:
        return {
            "success": False,
            "error": "Invalid decision_price",
            "detail": f"price={decision_price}"
        }
    
    # Simulate realistic slippage based on venue and liquidity
    if venue == "DEX":
        # DEX: higher slippage, depends on liquidity
        if liquidity_usd and liquidity_usd > 0:
            # Trade size as % of liquidity
            trade_size_usd = quantity * decision_price
            impact_pct = min(trade_size_usd / liquidity_usd, 0.05)  # Cap at 5%
            slippage_pct = impact_pct * random.uniform(0.8, 1.2)
        else:
            # Unknown liquidity: assume moderate slippage
            slippage_pct = random.uniform(0.001, 0.005)  # 0.1%-0.5%
    else:
        # CEX: lower slippage
        slippage_pct = random.uniform(0.0001, 0.001)  # 0.01%-0.1%
    
    # Apply slippage
    if side.upper() == "BUY":
        fill_price = decision_price * (1 + slippage_pct)
    else:
        fill_price = decision_price * (1 - slippage_pct)
    
    # Simulate trading fee (0.1% standard)
    fee_rate = 0.001
    fee_usd = fill_price * quantity * fee_rate
    
    # Generate order result
    order_id = f"PAPER-{int(time.time()*1000)}"
    
    order_result = {
        "success": True,
        "orderId": order_id,
        "symbol": symbol.upper(),
        "side": side.upper(),
        "type": "MARKET",
        "quantity": quantity,
        "price": fill_price,
        "fee_usd": fee_usd,
        "fee_rate": fee_rate,
        "status": "FILLED",
        "venue": venue,
        "exchange": exchange,
        "decision_price": decision_price,
        "slippage_pct": slippage_pct * 100,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }
    
    # Log to paper fills file
    try:
        log_dir = Path(__file__).resolve().parent.parent / "execution-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "paper_fills.jsonl"
        
        with open(log_file, "a") as f:
            f.write(json.dumps(order_result) + "\n")
    except Exception as e:
        # Non-fatal: log write failed but order succeeded
        print(f"[PAPER] Warning: Could not write to log: {e}")
    
    return order_result


def get_execution_parameters(decision_record: dict) -> dict:
    """
    Extract execution parameters from decision record.
    
    Returns dict with:
        - venue: 'CEX' or 'DEX'
        - exchange: exchange name
        - price: validated price
        - liquidity_usd: optional liquidity for slippage
    """
    
    # Determine venue from token profile
    token_profile = decision_record.get("token_profile", {})
    chain = token_profile.get("chain", "").lower()
    dex_only = token_profile.get("dex_only", False)
    cex_names = token_profile.get("cex_names", [])
    
    # Detect DEX token (note: cex_names is misnomer, includes DEXes like raydium)
    dex_exchanges = {"raydium", "orca", "jupiter", "uniswap", "pancakeswap", "sushiswap"}
    has_dex_only = any(ex.lower() in dex_exchanges for ex in cex_names) and len(cex_names) == len([ex for ex in cex_names if ex.lower() in dex_exchanges])
    
    if dex_only or has_dex_only or (chain in ("solana", "ethereum", "base") and not cex_names):
        venue = "DEX"
        # Use first DEX exchange from cex_names
        exchange = cex_names[0] if cex_names else "raydium"
    else:
        venue = "CEX"
        # Try routing, fallback to binance
        exchange = "binance"  # Will be enhanced by exchange_router
    
    # Get price from decision record (already validated in Stage 6/7)
    # Priority: execution.price > strategy.current_price > signal.price
    price = None
    
    if decision_record.get("execution", {}).get("current_price"):
        price = decision_record["execution"]["current_price"]
    elif decision_record.get("strategy", {}).get("current_price"):
        price = decision_record["strategy"]["current_price"]
    elif decision_record.get("signal", {}).get("price"):
        price = decision_record["signal"]["price"]
    
    # Get liquidity for slippage calc
    liquidity_usd = token_profile.get("liquidity_usd")
    
    return {
        "venue": venue,
        "exchange": exchange,
        "price": price,
        "liquidity_usd": liquidity_usd,
    }


if __name__ == "__main__":
    # Test execution
    print("Testing paper execution...")
    
    # Test CEX token
    result = execute_paper_trade(
        token="BTC",
        symbol="BTCUSDT",
        side="BUY",
        quantity=0.001,
        decision_price=67000.0,
        venue="CEX",
        exchange="binance"
    )
    print(f"CEX test: {result}")
    
    # Test DEX token
    result = execute_paper_trade(
        token="BP",
        symbol="BP/USDT",
        side="BUY",
        quantity=100.0,
        decision_price=0.0057,
        venue="DEX",
        exchange="raydium",
        liquidity_usd=550000
    )
    print(f"DEX test: {result}")
